# Evolver on Raven — runbook and lessons learned

The self-evolution pipeline (**evolver**) + the **AppWorld** benchmark, ported
from the upstream harness repo onto Raven's clean core. This document = *how to run
it* + *the traps already paid for on your behalf*. **Read §4 (lessons) before
launching anything** — most of the traps below cost real wall-clock money and
are invisible from the code alone.

Ported / verified: 2026-07-14. Subject model (the one being evolved): `qwen3.6-27B`.

---

## 1. Port status — what is here, what changed

Ported into Raven:

- `raven/evolver/` — the evolver machinery (orchestrator state machine, gates,
  taxonomy, activation, judge, tree).
- `raven/benchmarks/appworld/` — the AppWorld bench (`agent_cli.py`,
  `batch.py`, `tool.py`) + the `evolve/` glue.

Because Raven's core is a **refactor**, not a rename, these adaptations were made:

| Where | upstream (old) | Raven (now) |
|---|---|---|
| import root | upstream package root | `raven.*` (+ `load_raven_config`) |
| predicates | `agent/hook/predicates.py` | homed at `raven/evolver/activation/predicates.py` (keeps `raven/agent/` clean) |
| agent construction | `AgentLoop(bus=MessageBus(), …, hooks=install_env_gated_hooks(None))` | `AgentLoop(…, hooks=None)` — **no bus** (spine-driven now), no env hooks |
| run path | `await agent.process_direct(prompt, session_key)` | spine `await agent.run_turn(TurnRequest(...), emit, drain, stream=False, text_sink=sink)`; the reply lands in `sink["text"]` |
| session files | `ws/sessions/<key>.jsonl` (flat) | `conversation="appworld:<key>"` -> `ws/sessions/appworld/<key>.jsonl` (the evolver's `default_sess_path` has a matching fallback) |
| skills | `memory_engine.skill.SkillService` | `LocalSkillCatalog` (always-skills) + `SkillForgeRouter([LocalSkillSource(catalog.pool, catalog.registry)])` (select) |
| agentic front-end | upstream harness paths | `harness/raven/...` |
| **contamination** | 5 env-gated dormant "answer" levers baked into agent_cli.py | **stripped** — a true vanilla harness (see §4) |

**Verified runnable:** one real AppWorld task end-to-end on Raven's own
`.venv` (`success=True`, ~80-120s), the trajectory landing on the clean
session path, readable by the evolver's `render_trajectory` (task_desc +
`[ran python]`/`[stdout]` blocks + verdict).

---

## 2. Environment

- `uv sync` (already done once) builds Raven's `.venv` (Python 3.13). The
  `raven` package — including the ported `evolver` + `benchmarks/appworld` —
  **imports natively, no `PYTHONPATH` needed**.
- **Deliberate cross-venv design:** the AppWorld environment server runs in a
  **separate** `appworld-venv` (pydantic **v1**); the runner/agent runs in
  Raven's `.venv` (pydantic **v2**). `batch.py` orchestrates both.
- `APPWORLD_ROOT` (default `/Users/admin/workspace/appworld-run`) locates the
  `appworld-venv/bin/appworld` server binary + task data.

---

## 3. How to run

### 3-0. The unified entry (new, recommended) — one command for the whole flow

```bash
# Preflight (validate config / models / bench environment; runs nothing, seconds)
uv run python -m raven.evolver check --config <yaml> --smoke

# Smoke (3 tasks / K=1 / 1 round, lands in <work_dir>_smoke, never touches real state)
uv run python -m raven.evolver run --config docs/examples/evolve_appworld.yaml --smoke

# Real run: cold start (auto if missing) -> evolution rounds -> terminate -> auto-unseal retention
uv run python -m raven.evolver run --config docs/examples/evolve_appworld.yaml

# Interrupt at will; re-running the same command resumes. Progress (no test-number
# leakage) / early termination:
uv run python -m raven.evolver status   --config <yaml>
uv run python -m raven.evolver finalize --config <yaml> --yes
```

Config schema: `docs/examples/evolve_appworld.yaml` (annotated); the contract
for wiring a new bench: `docs/specs/evolve-bench-contract.md`. The launcher
scripts in 3a/3b below still work, but new experiments should use the unified
entry.

### 3a. Single-task smoke — works right now

```bash
APPWORLD_ROOT=/Users/admin/workspace/appworld-run \
/Users/admin/workspace/Raven/.venv/bin/python -m raven.benchmarks.appworld.batch \
  --split train --k 1 --conc 1 \
  --config /Users/admin/workspace/appworld-run/cfg_qwen36.json \
  --out-dir /tmp/raven_smoke --experiment raven_smoke \
  --tasklist <(printf '82e2fac_1\n') \
  --workspace /tmp/raven_smoke/ws --base-port 8780 --task-timeout 400
```

Expected: `[batch] env server :8780 UP`, `success=True`, result JSON at
`/tmp/raven_smoke/82e2fac_1_k0.json`, trajectory at
`/tmp/raven_smoke/ws/sessions/appworld/82e2fac_1_raven_smoke_k0.jsonl`.

### 3b. Full evolver run — wiring status (updated 2026-07-14)

1. ~~Write a Raven launcher~~ **done**: `/Users/admin/workspace/appworld-run/evo_scripts/orch_cmp_raven.py`
   (modeled on `orch_cmp.py`, re-pointed at the Raven repo/venv, output to a
   fresh `cmp_raven/` tree, never mixing with the old upstream arms).
   `vanilla` / `prep` / `agentic|qwen|claude [smoke]` modes as before.
   Deliberate changes: `n_sentinel` and `why_selection` no longer pinned
   (repo defaults: stratified sentinels + driver WHY selection).
2. ~~Fresh vanilla baseline~~ **done** (2026-07-14): 90 tasks x K=3, Raven
   harness, **pass@1 = 0.656** (175P/87F/5I/3INFRA), 93.9 min (a slow
   endpoint stretch), at `cmp_raven/shared/runs/van_cmp`, log
   `cmp_vanilla_raven_0714.log`.
3. ~~Commit the port~~ **committed**: `adfbe37` (port) + `df13e98` (fixes,
   branch `feat/port_evolver_appworld_bench`). **The base must be `df13e98`
   or newer** — `adfbe37`'s sandbox whitelist / path_guard still carried
   upstream paths (the whitelist matched nothing -> every designer edit was
   dropped). `df13e98` also includes: designer prompt re-pointing, driver
   WHY-selection input enrichment (taxonomy definitions + WHERE-hint
   distribution + unfixable share).
4. **The agentic arm is running** (2026-07-14, `CMP_BASE_SHA=df13e98`):
   `orch_cmp_raven.py agentic`, tree `cmp_raven/agentic/`, log
   `cmp_agentic_raven_0714.log` (baseline auto-copied by build_arm).

---

## 4. Lessons learned — read before launching

### Endpoint health — a small ping will lie to you
- Subject endpoint: `qwen3.6-27B` @ `sd8ppg9648…apigateway-cn-shanghai.volceapi.com`
  (in `cfg_qwen36.json`). It is a **shared batch backend**; throughput swings
  with *other tenants'* load (measured TTFT 0.2s -> 4s, 12 -> 33 tok/s). One
  earlier run had **68% of trials hit the 900s runner timeout while a small
  ping still answered in 3.5s.**
- **A small ping cannot detect degradation.** Probe with a **real
  generation**: a 300-token completion should finish in **<25s**; sturdier
  still, run one real task and confirm **<~200s**. Gate0's precheck **now
  probes with a real generation** (2026-07-14, `evolve/precheck.py`:
  300-token completion, judged at >=12 tok/s; timeout / empty generation /
  low throughput all fail; validated against the live endpoint).
- The other endpoint (`sd95i02…`, `cfg_qwen36_main.json`) is **dead (NXDOMAIN)**.

### Endpoint dying mid-round -> INCOMPLETE contamination (0714 field trap, fixed)
- Gate0 only guards **round boundaries**; when the endpoint dies mid-round,
  the LLM returns **empty responses** or litellm error text, recorded as
  INCOMPLETE (a genuine failure), and the **rerun ladder does not trigger**
  (it only recognizes INFRA) -> one confirm measured 270/270 INCOMPLETE,
  z=-14.67, nearly executing a good +5.2 candidate (v1-c2, W4 date anchoring).
- Fix (`8ea11fa` + `8c3110a`): on an empty response, probe the endpoint;
  transport-level failure => INFRA (an empty response under a healthy
  endpoint keeps its W1 semantics); litellm-rendered error text
  (`"Error calling LLM: ... Connection error."`) added to the transport
  signatures.
- Post-hoc salvage path (validated): cherry-pick the candidate edit onto the
  fixed base -> re-run the confirm manually via `make_appworld_eval_fn` +
  `eval_with_infra_rerun` (indistinguishable from gate output) -> rewrite the
  journal round record + `work/history.json` + the node ledger (all backed up
  to quarantine/ first) -> restart the launcher; journal replay resumes with
  the promoted parent.

### Same-regime methodology (SOP iron law)
- The baseline must run **back-to-back** with the arm that consumes it.
  **Never compare across time.** The subject's regime drifted between 07-10
  and 07-14 (vanilla pass@1 `0.604` -> `0.678`). A stale baseline gives a
  wrong patience bar **and** stale diagnosis trajectories -> an entire
  (expensive) arm can be wasted on phantom failures.

### Contamination — the root reason we moved to Raven
- The upstream `agent_cli.py` was **born with** (commit `e9344ad9`) 5 env-gated
  dormant "answer" levers: `DATETIME_ANCHOR` (a W4 time fix), `APPWORLD_KB`,
  `PLAN_FIRST`, `SUBMIT_VERIFY`, `FINALIZE_ENFORCE`. No clean version ever
  existed in git.
- They are **traps**: the designer is *forbidden* from relying on env vars
  (eval sets none), so any fix behind `if os.environ.get(...)` is **dead
  code** -> culled by the zero-hit preflight. The dormant levers also *lure*
  the designer (observed on opus) into imitating the pattern and shipping
  dead code.
- The Raven port **stripped them** -> a true vanilla harness. Evolution now
  measures invented fixes, not "find the buried easter egg". (The qwen arm's
  earlier "win" was exactly discovering the dormant `FINALIZE_ENFORCE` hook —
  an overestimate of capability.)

### The designer's editable surface and activation
- Editable whitelist = **`raven/benchmarks/appworld/` + `raven/agent/`**
  (both whole trees), not just `agent_cli.py`. New hooks go under
  `raven/agent/hook/`, wired **unconditionally**.
- Activation rule: changes must take effect **with no env vars set**. **Code**
  changes must embed an `activation_beacon` (Gate-b reads which tasks fired
  through it); **prompt/string** changes are beacon-exempt.

### Diagnosis-model confusion
- The *tier* of the diagnosis/judge model determines which levers get
  suggested. haiku suggested hook levers **0/68** times; 397B/opus/sonnet do
  it readily (replay hit rates: haiku 0/5, sonnet-5 4/5, opus 5/5).
  **Do not use a weak model for diagnosis.**
- The `analysis_mode="agentic"` front-end (a read-only opus Claude Code
  session: ledger summary -> group by signature -> deep-read representative
  trajectories -> read harness source) produces high-quality diagnosis and
  honestly separates *fixable* (e.g. W4 time anchoring,
  `WHERE=appworld_prompt`) from *capability ceiling* (`WHERE=none`).

### WHY selection can override diagnosis
- Observed in Raven's predecessor runs: the agentic analyst correctly flagged
  a fixable **W4** (a real, reliably prompt-fixable time-anchoring bug) **and**
  a large unfixable **W6** bucket. But `why_selection="formula"` (count x
  fixability x `0.55^losing-streak`) picked **W6** (the biggest bucket, which
  the analyst itself had labeled *capability ceiling*) -> over-triggering
  candidates -> **sentinel regression** -> culled. Prefer
  `why_selection="driver"` (it reads the fixability signals). The comparison
  experiments pinned `"formula"` only for cross-arm comparability.

### Dominant failure mode / gates
- The predominant "not promoted" outcome is **focused win / full-set
  regression** (the candidate helps its target subset but over-triggers and
  wrecks healthy tasks). **Stratified sentinels** (6 stable + 6 borderline,
  rotated by node hash) catch it; the comparison experiments pinned
  `n_sentinel=3` (old behavior). Across the two-arm experiment the gates were
  **17/17 correct** — the bottleneck was never the gates, it was the
  designer's aim.

### Raven core specifics (handled — just know them)
- `AgentLoop` is **spine-driven** now: `process_direct` and `MessageBus` are
  gone. Run via `run_turn(TurnRequest, emit, drain, stream=False,
  text_sink={})`; the final reply lands in `text_sink["text"]`. **AppWorld
  pass/fail is judged by the environment oracle** (`/evaluate`), not the
  reply text — so the run path only needs to drive the agent to completion.
- Config: `load_raven_config()`; `Config.workspace_path`,
  `.agents.defaults.*`, `.tools.exec` / `.tools.restrict_to_workspace` keep
  their old field shapes.

---

## 5. Durable locations

| What | Where |
|---|---|
| Run dirs, task data, `appworld-venv`, `cfg_qwen36.json` | `/Users/admin/workspace/appworld-run/` |
| The old (upstream-path) launcher — copy then re-point | `/Users/admin/workspace/appworld-run/evo_scripts/orch_cmp.py` |
| Historical experiment journals / arm dirs | `/Users/admin/workspace/appworld-run/cmp/` |
| upstream source (reference; contaminated) | local checkout of the upstream repo |
| This port | `raven/evolver/`, `raven/benchmarks/appworld/` |

---

## 6. TL;DR — five steps to a clean run

1. `uv sync` (done) -> Raven's `.venv` imports the ported code natively.
2. **Probe the endpoint with a real generation**, never a ping (§4; the Gate0
   precheck already does real-generation probing).
3. Commit the port -> `export CMP_BASE_SHA=<sha>` (the launcher
   `orch_cmp_raven.py` is written and requires this variable).
4. A fresh same-regime vanilla baseline (run on the Raven harness 0714), then
   `orch_cmp_raven.py prep && orch_cmp_raven.py agentic` — or use the unified
   entry (§3-0), which folds baseline + rounds + unseal into one command.
5. Watch for focused-win / full-set regression; `why_selection` defaults to
   `"driver"`; never use a weak model for diagnosis.
