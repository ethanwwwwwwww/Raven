"""The single agent tool for AppWorld: execute Python in the AppWorld REPL.

AppWorld is pinned to pydantic v1 (via sqlmodel) while Raven needs pydantic
v2, so the two cannot share a venv. We therefore run AppWorld as its own HTTP
``environment`` server (``appworld serve environment``, in the AppWorld venv) and
talk to it over HTTP from here — this module never imports ``appworld``.

This tool routes the agent's code to ``POST {env_url}/execute`` (the stateful
in-server world REPL): variables, imports and logins persist across calls. One
env server holds ONE world at a time, so the batch runner gives each concurrent
task its own server/port.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import requests

from raven.agent.tools.base import Tool

# R6 evolved RUNTIME patch (env-gated EXEC_RECOVER, default off). Fires ONLY when an
# execute call errors — so it never touches the prompt or the successful path (the
# regression trap that sank every prompt/knowledge patch). Targets W3 (API-signature
# fumbling): appends a targeted recovery hint to a failed execution's output.
_ERR_MARKERS = ("Execution failed", "Traceback", "is not allowed", "KeyError",
                "TypeError", "got an unexpected keyword", "missing", "AttributeError")
_RECOVER_HINT = (
    "\n\n[recovery hint] That execution errored. Before retrying: "
    "(a) call apis.api_docs.show_api_doc(app_name=..., api_name=...) to confirm the EXACT "
    "parameter names/types; (b) os.listdir/glob/open are blocked — use apis.file_system.*; "
    "(c) most APIs need access_token= from apis.<app>.login(...)['access_token']. "
    "Fix the call based on the error above, then re-run."
)


_BLOCKED_HINT = (
    "\n\n[recovery hint] You used a module blocked in this sandbox (os.listdir/glob/open are "
    "not allowed). Use the file_system app instead: apis.file_system.show_directory("
    "access_token=..., directory_path=...) and apis.file_system.show_file(access_token=..., "
    "file_path=...). Re-run with those."
)


def _maybe_recover(output: str) -> str:
    if not isinstance(output, str):
        return output
    # R7 NARROW activation: only on the unambiguous "blocked module" error — rare, clearly
    # fixable, and (unlike all-errors) shouldn't touch the fragile tasks that error-but-recover.
    if os.environ.get("EXEC_RECOVER_NARROW") and "is not allowed" in output:
        return output + _BLOCKED_HINT
    if os.environ.get("EXEC_RECOVER") and any(mk in output for mk in _ERR_MARKERS):
        return output + _RECOVER_HINT
    return output


# R8 evolved RUNTIME patch (env-gated LOOP_BREAKER, default off). Distinct from VF (engagement)
# and EXEC_RECOVER (per-error hint): tracks CONSECUTIVE errored execute calls and, on the Nth in
# a row, injects a meta-level redirect to break unproductive flailing (the b0a8eae 40-iteration
# wall that raising max_iterations did not fix). Fires once per streak, then resets.
_LOOP_BREAKER_THRESHOLD = 3
_LOOP_BREAKER_MSG = (
    "\n\n[loop-breaker] You have errored on several consecutive steps — you are likely stuck "
    "repeating a failing approach. STOP and reset: in ONE execute call, print() the concrete "
    "data you have ALREADY gathered successfully; re-read the task and list what is still "
    "missing; then pick a DIFFERENT api/approach than the one that keeps failing. Do NOT repeat "
    "the last failing call."
)


# R9 evolved RUNTIME patch (env-gated ZERO_CHALLENGE, default off). Diagnosed from VF failure
# trajectories: the largest answer-type failure cluster submits a suspicious ZERO/EMPTY answer
# ($0, "no transactions", "none") — almost always because the agent used the real-world date
# instead of the world clock and filtered everything out. This intercepts the FIRST such
# complete_task (before it finalizes), returns a self-check challenge, and lets the retry pass.
# Runtime lever (tool-level intercept), distinct from DATETIME_ANCHOR (prompt injection).
_SUSPICIOUS_ANSWER = re.compile(
    r"\$0(\.0+)?\b"                                    # a dollar-zero amount ($0, $0.00)
    r"|answer\s*=\s*[\"']?\$?0(\.0+)?[\"']?\s*\)"      # answer=0 / "0" / "$0.00"
    r"|answer\s*=\s*[\"']\s*[\"']"                     # answer=""  (empty)
    r"|there (are|were) no\b"                          # "there are no ..."
    r"|no (transactions|bills|records|results|data|songs|items|matches)\b",
    re.I,
)
_ZERO_CHALLENGE_MSG = (
    "[verify before finalizing] You are about to submit a ZERO / EMPTY / 'no results' answer, "
    "which is frequently WRONG here due to a DATE mistake: the environment's current date is NOT "
    "the real-world today. Before finalizing: (1) print the environment/world current date and "
    "re-check every 'this year / this month / since / recent' filter against THAT date, not "
    "today's real date; (2) confirm you actually retrieved the data and it is genuinely empty, "
    "not excluded by a wrong date or parameter. If after re-checking it is truly zero/empty, call "
    "complete_task again and it will go through."
)


class AppWorldExecuteTool(Tool):
    def __init__(self, env_url: str, task_id: str, timeout: float = 180.0) -> None:
        self._env_url = env_url.rstrip("/")
        self._task_id = task_id
        self._timeout = timeout
        self._consec_err = 0
        self._zero_challenged = False
        self.saw_complete_task = False

    @property
    def name(self) -> str:
        return "execute"

    @property
    def description(self) -> str:
        return (
            "Execute Python code in the AppWorld environment and return its stdout. "
            "Call app APIs via apis.<app>.<api>(...). State (variables, imports, logins) "
            "persists across calls like a Jupyter notebook, so build up the solution "
            "incrementally. You MUST print() anything you want to see — only stdout is "
            "returned. Discover APIs with apis.api_docs.show_api_descriptions(app_name=...) "
            "and apis.api_docs.show_api_doc(app_name=..., api_name=...). When the task is "
            "done call apis.supervisor.complete_task(answer=...) exactly once."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to run in the stateful AppWorld REPL.",
                }
            },
            "required": ["code"],
        }

    def _post_execute(self, code: str) -> str:
        if "complete_task" in code:
            self.saw_complete_task = True
        if (os.environ.get("ZERO_CHALLENGE") and not self._zero_challenged
                and "complete_task" in code and _SUSPICIOUS_ANSWER.search(code)):
            self._zero_challenged = True
            return _ZERO_CHALLENGE_MSG
        resp = requests.post(
            f"{self._env_url}/execute",
            json={"task_id": self._task_id, "code": code},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        output = _maybe_recover(resp.json().get("output", ""))
        if os.environ.get("LOOP_BREAKER") and isinstance(output, str):
            if any(mk in output for mk in _ERR_MARKERS):
                self._consec_err += 1
                if self._consec_err >= _LOOP_BREAKER_THRESHOLD:
                    self._consec_err = 0
                    return output + _LOOP_BREAKER_MSG
            else:
                self._consec_err = 0
        return output

    async def execute(self, code: str) -> str:
        # Blocking HTTP -> run off the event loop so the loop stays responsive.
        return await asyncio.to_thread(self._post_execute, code)
