"""Run ONE AppWorld task through a minimal Raven AgentLoop, then grade.

Cross-venv design (see tool.py): AppWorld (pydantic v1) runs as an HTTP
``environment`` server; this runner (Raven, pydantic v2) never imports
appworld — it drives the task purely over HTTP:

  POST {env}/initialize {task_id}      -> instruction + supervisor (loads world)
  agent's `execute` tool -> POST {env}/execute {task_id, code}
  POST {env}/evaluate {task_id}        -> TestTracker dict (success = pass@1)
  POST {env}/task_completed, /close

One env server holds one world at a time, so the batch runner gives each
concurrent task its own server/port (--env-url). The harness is plain vanilla:
the agent gets only the `execute` tool and the base AppWorld prompt.

Usage::

    python -m raven.benchmarks.appworld.agent_cli \
        --task-id 82e2fac_1 --env-url http://127.0.0.1:8100 \
        --config <subject_cfg.json> --out result.json [--experiment vanilla]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import requests

APPWORLD_PROMPT = """You are an autonomous agent that completes a digital task for your supervisor by writing and running Python code.

You act ONLY through the `execute` tool: you write Python, it runs in a stateful REPL (variables, imports and logins persist across calls, like a Jupyter notebook), and you get back stdout. You MUST print() anything you want to observe.

How to work:
- See the apps:            print(apis.api_docs.show_app_descriptions())
- List an app's APIs:      print(apis.api_docs.show_api_descriptions(app_name='spotify'))
- Read one API's doc:      print(apis.api_docs.show_api_doc(app_name='spotify', api_name='login'))
- Your supervisor's data:  apis.supervisor.show_profile(), show_account_passwords(), show_addresses(), show_payment_cards()
- Most APIs need an access_token. Log in first, e.g.:
      pwds = apis.supervisor.show_account_passwords()
      token = apis.spotify.login(username=..., password=...)["access_token"]
- Go step by step: inspect first, then act, then verify before finalizing.

When the task is complete, call exactly once:
- A question  ->  apis.supervisor.complete_task(answer=<your answer>)
- An action   ->  apis.supervisor.complete_task()

Your supervisor: {supervisor}

Your task:
{instruction}
"""


def _build_agent(args):
    from raven.agent.loop import AgentLoop
    from raven.cli._helpers import load_runtime_config, make_provider
    from raven.config.raven import load_raven_config
    from raven.session.manager import SessionManager

    from raven.benchmarks.appworld.tool import AppWorldExecuteTool

    config = load_runtime_config(args.config, args.workspace)
    ec_config = load_raven_config()
    provider = make_provider(config)

    # AppWorld is pure API/code: disable every default tool, give only `execute`.
    disabled = [
        "read_file", "write_file", "edit_file", "list_dir",
        "exec", "web_search", "web_fetch", "message", "spawn", "cron",
    ]
    agent = AgentLoop(
        provider=provider,
        workspace=config.workspace_path,
        model=args.model or config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=SessionManager(config.workspace_path),
        context_config=ec_config.context,
        hooks=None,
        disabled_tools=disabled,
    )
    exec_tool = AppWorldExecuteTool(args.env_url, args.task_id)
    agent.tools.register(exec_tool)
    return agent, exec_tool


def _post(env_url: str, path: str, body: dict, timeout: float = 120.0) -> dict:
    r = requests.post(f"{env_url.rstrip('/')}{path}", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json().get("output", {})


# When the LLM endpoint drops/times-out/rate-limits, the agent loop catches the
# provider error and returns it as the final content (finish_reason=="error"),
# so it never raises up to the HTTP layer and would otherwise be graded as a
# normal wrong answer. That is infra, not the agent's fault -- detect it and
# raise so the run is recorded as infra_error and excluded from scoring. The
# signatures are short one-liners; the length guard avoids flagging a long
# genuine answer that merely mentions one of these words.
_LLM_TRANSPORT_SIGNS = (
    "error: connection error",
    "sorry, i encountered an error calling the ai model",
    "apiconnectionerror", "apitimeouterror", "api timeout error",
    "ratelimiterror", "rate limit",
    "internal server error", "service unavailable",
    "502 bad gateway", "503 service", "bad gateway",
    # litellm renders provider failures without the spaced/colon forms above
    # (observed leaking through as INCOMPLETE: "Error calling LLM:
    # litellm.InternalServerError: OpenAIException - Connection error.")
    "error calling llm", "connection error", "internalservererror",
)


def _is_llm_transport_error(resp: str | None) -> bool:
    if not resp:
        return False
    s = resp.strip().lower()
    if len(s) > 300:
        return False
    return any(sig in s for sig in _LLM_TRANSPORT_SIGNS)


def _endpoint_dead(config_path: str) -> bool:
    """True only when the subject endpoint is transport-unreachable.

    Disambiguates an EMPTY final response: a dead endpoint (DNS gone,
    connection refused) yields empty completions on every task, which would
    otherwise be graded INCOMPLETE — real-looking fails that poison whole
    evals (observed: a mid-run endpoint death scored 270/270 INCOMPLETE).
    But an empty response with a HEALTHY endpoint is the agent's own W1
    stall and must stay a legit fail, so only a transport-level probe
    failure counts as dead; any HTTP status means alive.
    """
    try:
        cfg = json.load(open(config_path))
        defaults = cfg.get("agents", {}).get("defaults", {})
        base = (cfg.get("providers", {}).get(defaults.get("provider")) or {}).get("api_base")
        if not base:
            return False
        requests.get(base.rstrip("/") + "/models", timeout=10)
        return False
    except requests.RequestException:
        return True
    except Exception:
        return False


async def _run(args) -> dict:
    t0 = time.time()
    result: dict = {"task_id": args.task_id, "experiment": args.experiment}
    try:
        init = _post(args.env_url, "/initialize",
                     # unique experiment_name per attempt -> isolated experiments/outputs/<...>/tasks/<task>/
                     # dir, so concurrent K-trials of the same task never collide on model_hashes.json (Errno 22).
                     {"task_id": args.task_id, "experiment_name": (args.session or args.experiment)})
        prompt = APPWORLD_PROMPT.format(
            supervisor=init.get("supervisor"), instruction=init.get("instruction"))
        agent, exec_tool = _build_agent(args)
        skey = args.session or args.task_id
        try:
            # Raven's AgentLoop is spine-driven (no process_direct): drive one headless
            # turn through run_turn with no-op emit/drain and capture the final reply via
            # text_sink. AppWorld success is judged by the env oracle (/evaluate), not this
            # text; it is kept only for transport-error detection and the result record.
            from raven.spine import ChatType, Origin, Source, TurnRequest

            async def _emit(_event):
                return None

            def _drain():
                return []

            sink: dict = {}
            req = TurnRequest(
                origin=Origin.USER,
                source=Source(channel="cli", chat_id="direct",
                              sender_id="user", chat_type=ChatType.DM),
                # Raven's SessionManager splits the conversation key on ':' into
                # <channel>/<chat_id>.jsonl. Prefix a fixed channel so the per-
                # attempt transcript lands at a clean flat path the evolver reads:
                # ws/sessions/appworld/<tid>_<exp>_k<k>.jsonl.
                text=prompt, conversation=f"appworld:{skey}",
            )
            await agent.run_turn(req, _emit, _drain, stream=False, text_sink=sink)
            response = sink.get("text") or ""
        finally:
            await agent.close_executor()
            client = getattr(getattr(agent, "provider", None), "_client", None)
            closer = getattr(client, "close", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass
        if _is_llm_transport_error(response):
            raise RuntimeError(f"llm_transport_error: {response.strip()[:160]}")
        if not response.strip() and _endpoint_dead(args.config):
            raise RuntimeError(
                "llm_transport_error: empty response and subject endpoint unreachable")
        ev = _post(args.env_url, "/evaluate",
                   {"task_id": args.task_id, "suppress_errors": True})
        done = _post(args.env_url, "/task_completed", {"task_id": args.task_id})
        # Capture the full evaluation oracle (passes/failures) for the evolver's
        # diagnose step; derive pass_count when the env omits it. This is the only
        # addition over the original vanilla adapter -- pure instrumentation, no behaviour change.
        _passes = ev.get("passes") or []
        _failures = ev.get("failures") or []
        result.update(
            success=bool(ev.get("success")),
            num_tests=ev.get("num_tests"),
            pass_count=ev.get("pass_count") if ev.get("pass_count") is not None else len(_passes),
            evaluation={"passes": _passes, "failures": _failures},
            task_completed=bool(done) if not isinstance(done, dict) else done,
            response=(response or "")[:2000],
            elapsed_s=round(time.time() - t0, 1),
        )
    except BaseException as e:
        import traceback
        result.update(success=False, infra_error=f"{type(e).__name__}: {e}",
                      traceback=traceback.format_exc()[-2000:],
                      elapsed_s=round(time.time() - t0, 1))
    finally:
        try:
            _post(args.env_url, "/close", {"task_id": args.task_id})
        except Exception:
            pass
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="appworld-agent")
    p.add_argument("--task-id", required=True)
    p.add_argument("--env-url", required=True, help="AppWorld environment server base URL.")
    p.add_argument("--config", required=True, help="Raven runtime config JSON.")
    p.add_argument("--out", required=True, help="Where to write the result JSON.")
    p.add_argument("--workspace",
                   default=os.path.expanduser("~/workspace/appworld-run/ws"))
    p.add_argument("--model", default=None)
    p.add_argument("--experiment", default="vanilla")
    p.add_argument("--session", default=None,
                   help="Session key (jsonl stem). Default task_id; pass a per-attempt "
                        "key to retain all K trajectories instead of overwriting.")
    args = p.parse_args(argv)

    import contextlib
    try:
        import litellm
        litellm.suppress_debug_info = True
    except Exception:
        pass

    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(_run(args))

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    sys.stderr.write(f"[appworld] {args.task_id} success={result.get('success')} "
                     f"infra={result.get('infra_error')} t={result.get('elapsed_s')}s\n")
    return 0


if __name__ == "__main__":
    import os
    os._exit(main())
