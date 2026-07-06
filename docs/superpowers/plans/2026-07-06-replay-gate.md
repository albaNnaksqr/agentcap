# Replay Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agentcap replay` re-executes each seeded session's fail_to_pass tests against the reconstructed exportable artifact and writes a graded `replay.json`; export sets `task.json.verified` from it.

**Architecture:** New store-stage module `agentcap/replay.py` (sibling of join/seed/value). Per session: build a temp bundle (reuse `export._bundle`), reconstruct END state via `verify.reconstruct()` with the bundle as source, run fail_to_pass node IDs with pytest (green check), reconstruct START state, overlay end-state test files (test patch), run again (red check), grade. Export reads `replay.json`.

**Tech Stack:** Python stdlib only (subprocess, tempfile, venv). Follows repo conventions: no external deps, script-style tests run via `python3 -m agentcap.tests.test_replay`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-06-replay-gate-design.md`.
- No repo-dependency installation, no docker (L1/L2 boundary: "flight recorder, not flight simulator").
- Outcomes: `red_green` (only one granting `verified: true`), `green_only`, `not_green`, `setup_failed`.
- Gate is computed over "runnable" IDs = fail_to_pass IDs whose test collects at END state; IDs missing at end are recorded but excluded (renamed-test case); zero runnable IDs → `setup_failed` with reason `no_runnable_tests`.
- Red requirement per runnable ID: start(+test patch) status is anything **except** `passed` (a collection error at base is still "not passing" — SWE-bench convention).
- Any pytest run exceeding `--timeout` (default 300 s) → `setup_failed`.
- Style: match existing modules — module docstring explaining the *why*, compact helpers, `_write`/`_load` json convention, one bad session must not kill a batch.

## File Structure

- Create: `agentcap/replay.py` — all replay logic.
- Create: `agentcap/tests/test_replay.py` — script test, imports fixture helpers from `test_export`.
- Modify: `agentcap/cli.py` — add `replay` subcommand.
- Modify: `agentcap/export.py` — consume `replay.json` (~6 lines).

---

### Task 1: grading logic (`_grade`) — pure function

**Files:**
- Create: `agentcap/replay.py` (module skeleton + `_grade`)
- Create: `agentcap/tests/test_replay.py` (grade cases only)

**Interfaces:**
- Produces: `_grade(tests: list[dict]) -> (outcome: str, verified: bool, reason: str|None)`.
  Each test row: `{"node_id": str, "end_status": str, "start_status": str|None}`.
  Statuses: `"passed" | "failed" | "error" | "missing"` (start_status may be None when green check already failed and red check was skipped).

- [ ] **Step 1: Write the failing test**

```python
# agentcap/tests/test_replay.py
"""Replay gate: reconstruct the exportable artifact and re-run fail_to_pass.
verified is EARNED: end all-green AND start(+test patch) all-red. Run with
    python3 -m agentcap.tests.test_replay
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from agentcap import replay as R


def main():
    fails = []

    # --- grading: pure outcome logic ---
    rg = R._grade([{"node_id": "t.py::a", "end_status": "passed", "start_status": "failed"}])
    if rg != ("red_green", True, None):
        fails.append("red_green grade wrong: %s" % (rg,))
    go = R._grade([{"node_id": "t.py::a", "end_status": "passed", "start_status": "passed"}])
    if go[:2] != ("green_only", False):
        fails.append("green_only grade wrong: %s" % (go,))
    ng = R._grade([{"node_id": "t.py::a", "end_status": "failed", "start_status": None}])
    if ng[:2] != ("not_green", False):
        fails.append("not_green grade wrong: %s" % (ng,))
    # collection error at start still demonstrates "not passing" -> red ok
    ce = R._grade([{"node_id": "t.py::a", "end_status": "passed", "start_status": "error"}])
    if ce != ("red_green", True, None):
        fails.append("start error should count as red: %s" % (ce,))
    # id missing at end is excluded; remaining ids gate
    mx = R._grade([{"node_id": "t.py::gone", "end_status": "missing", "start_status": None},
                   {"node_id": "t.py::a", "end_status": "passed", "start_status": "failed"}])
    if mx != ("red_green", True, None):
        fails.append("missing-at-end should be excluded: %s" % (mx,))
    # nothing runnable -> setup_failed
    nr = R._grade([{"node_id": "t.py::gone", "end_status": "missing", "start_status": None}])
    if nr[0] != "setup_failed" or nr[1] or nr[2] != "no_runnable_tests":
        fails.append("no runnable ids should be setup_failed: %s" % (nr,))
    if not fails:
        print("[ok] grading: red_green/green_only/not_green/excluded/none-runnable")

    if fails:
        print("\n".join("[FAIL] " + f for f in fails))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/workspace/agentcap && python3 -m agentcap.tests.test_replay`
Expected: `AttributeError` / `ImportError` (module or `_grade` missing)

- [ ] **Step 3: Write minimal implementation**

```python
# agentcap/replay.py
"""Replay gate: re-execute a seeded session's fail_to_pass tests against the
reconstructed exportable artifact. `verified` is EARNED here, not observed:
the END state must run all-green and the START state — with the end's test
files overlaid (the test patch, SWE-bench style) — must not pass. Replaying
from a bundle, never the live repo, is what makes verified mean
"self-contained replayable". No dependency install, no docker (L2-, not L3).
"""
import json
import os


def _grade(tests):
    """-> (outcome, verified, reason). Gate over ids runnable at END;
    missing-at-end ids (renamed mid-session) are recorded but excluded."""
    runnable = [t for t in tests if t["end_status"] != "missing"]
    if not runnable:
        return "setup_failed", False, "no_runnable_tests"
    if any(t["end_status"] != "passed" for t in runnable):
        return "not_green", False, None
    if any(t["start_status"] == "passed" for t in runnable):
        return "green_only", False, None
    return "red_green", True, None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/workspace/agentcap && python3 -m agentcap.tests.test_replay`
Expected: `[ok] grading: ...` then `ALL PASS`

- [ ] **Step 5: Commit**

```bash
cd ~/workspace/agentcap && git add agentcap/replay.py agentcap/tests/test_replay.py && git commit -m "feat: replay grading logic (outcome semantics)"
```

---

### Task 2: replay_session end-to-end (red_green / green_only / not_green / setup_failed)

**Files:**
- Modify: `agentcap/replay.py` (add pytest runner, materialize, `replay_session`, `replay_all`)
- Modify: `agentcap/tests/test_replay.py` (add 4 end-to-end scenarios)

**Interfaces:**
- Consumes: `export._bundle(repo, shas, dest)`, `verify.reconstruct(capture_dir, source_repo, dest, cas_root)`, `session._paths`, `session.list_sessions`, fixture helpers `build`, `claude_log` from `agentcap.tests.test_export`.
- Produces:
  - `replay_session(session_dir, timeout=300, python=None) -> report dict` — writes `<session_dir>/replay.json`, returns it. Report keys: `replay_version(=1), outcome, verified, reason, tests(list of rows), overlaid_test_files, python, durations, error`.
  - `replay_all(root=sess.DEFAULT_ROOT, timeout=300, python=None) -> {session_id: report|None}` (None = no seed; error dict on crash, one bad session never kills the batch).
  - `_ensure_python(root)` — returns a python executable that can `import pytest`: `sys.executable` if it can, else a cached venv at `<root>/replay-venv` (created once, `pip install pytest`).

- [ ] **Step 1: Write the failing test** — append to `test_replay.py` `main()` before the `if fails:` block, and add the helpers/constants at module level:

```python
# module level, after imports
from agentcap import export as E
from agentcap import session as sess
from agentcap.tests.test_export import build, claude_log  # fixture factories

MOD_RED = "def f():\n    return 0\n"
MOD_GREEN = "def f():\n    return 1\n"
TEST_F = "import mod\n\ndef test_f():\n    assert mod.f() == 1\n"
NODE = "tests/test_mod.py::test_f"
PROMPT = "make test_f pass"
RED_OUT = "%s FAILED\nE   AssertionError\n=== 1 failed in 0.1s ===" % NODE
GREEN_OUT = "%s PASSED\n=== 1 passed in 0.1s ===" % NODE
EVENTS = [("user", PROMPT), ("bash", "python -m pytest -q", RED_OUT),
          ("edit", "mod.py"), ("bash", "python -m pytest -q", GREEN_OUT)]


def seeded(tmp, name, files, edits):
    """build() + taskseed so replay has a seed to chew on."""
    from agentcap import taskseed as T
    repo, store, sid = build(tmp, name, files, edits, EVENTS)
    sdir = os.path.join(store, "sessions", sid)
    seed = T.extract_seed(sdir)
    assert seed and seed["candidate_fail_to_pass"] == [NODE], "fixture seed broken: %s" % seed
    return repo, store, sid, sdir
```

```python
    # in main(), after grading checks
    tmp = tempfile.mkdtemp(prefix="agentcap-replay-")
    py = R._ensure_python(tmp)

    # --- S1 red_green: real fix, start red -> end green, verified earned ---
    files = {"mod.py": MOD_RED, "tests/test_mod.py": TEST_F}
    repo1, store1, sid1, sdir1 = seeded(tmp, "rg", files, {"mod.py": MOD_GREEN})
    rep = R.replay_session(sdir1, python=py)
    if rep["outcome"] != "red_green" or not rep["verified"]:
        fails.append("S1 should be red_green: %s / %s" % (rep["outcome"], rep.get("error")))
    elif not os.path.exists(os.path.join(sdir1, "replay.json")):
        fails.append("S1 replay.json not persisted")
    else:
        row = next(t for t in rep["tests"] if t["node_id"] == NODE)
        if row["end_status"] != "passed" or row["start_status"] == "passed":
            fails.append("S1 per-test rows wrong: %s" % row)
        else:
            print("[ok] S1 red_green: verified earned, per-test rows recorded")

    # --- S2 green_only: no real change, test passed all along ---
    files_g = {"mod.py": MOD_GREEN, "tests/test_mod.py": TEST_F}
    _, _, _, sdir2 = seeded(tmp, "go", files_g, {"README.md": "x\n"})
    rep2 = R.replay_session(sdir2, python=py)
    if rep2["outcome"] != "green_only" or rep2["verified"]:
        fails.append("S2 should be green_only: %s" % rep2["outcome"])
    else:
        print("[ok] S2 green_only: task invalid, verified withheld")

    # --- S3 not_green: end state still fails (e.g. uncaptured fixture dep) ---
    _, _, _, sdir3 = seeded(tmp, "ng", files, {"README.md": "x\n"})
    rep3 = R.replay_session(sdir3, python=py)
    if rep3["outcome"] != "not_green" or rep3["verified"]:
        fails.append("S3 should be not_green: %s" % rep3["outcome"])
    else:
        print("[ok] S3 not_green: unreplayable end state caught")

    # --- S4 setup_failed: source repo gone, bundle cannot be built ---
    repo4, store4, sid4, sdir4 = seeded(tmp, "sf", files, {"mod.py": MOD_GREEN})
    shutil.rmtree(repo4)
    rep4 = R.replay_session(sdir4, python=py)
    if rep4["outcome"] != "setup_failed" or rep4["verified"] or not rep4.get("error"):
        fails.append("S4 should be setup_failed with error: %s" % rep4)
    else:
        print("[ok] S4 setup_failed: infrastructure failure graded, error recorded")

    # --- replay_all: batch shape, no-seed session -> None ---
    results = R.replay_all(root=store1, python=py)
    if results.get(sid1, {}).get("outcome") != "red_green":
        fails.append("replay_all should reuse/recompute S1: %s" % results)
    else:
        print("[ok] replay_all: batch summary over the store")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/workspace/agentcap && python3 -m agentcap.tests.test_replay`
Expected: `AttributeError: module 'agentcap.replay' has no attribute '_ensure_python'`

- [ ] **Step 3: Write the implementation** — extend `agentcap/replay.py`:

```python
# add to imports at top
import shutil
import subprocess
import sys
import tempfile
import time

from . import session as sess
from .export import _bundle
from .verify import reconstruct

REPLAY_VERSION = 1
DEFAULT_TIMEOUT = 300


def _ensure_python(root):
    """A python that can `import pytest`: the current one if able, else a
    cached venv at <root>/replay-venv (pytest only — never repo deps)."""
    if subprocess.run([sys.executable, "-c", "import pytest"],
                      capture_output=True).returncode == 0:
        return sys.executable
    venv = os.path.join(root, "replay-venv")
    py = os.path.join(venv, "bin", "python")
    if not os.path.exists(py):
        subprocess.run([sys.executable, "-m", "venv", venv],
                       check=True, capture_output=True)
        subprocess.run([py, "-m", "pip", "install", "--quiet", "pytest"],
                       check=True, capture_output=True)
    return py


def _run_test(python, tree, node_id, timeout):
    """-> passed | failed | missing | error (rc>1 without a real run) | timeout."""
    env = dict(os.environ, PYTHONPATH=".", PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run([python, "-m", "pytest", "-q", "--no-header", node_id],
                           cwd=tree, env=env, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout"
    if p.returncode == 0:
        return "passed"
    if p.returncode == 1:
        return "failed"
    out = p.stdout + p.stderr
    if p.returncode in (4, 5) or "no tests ran" in out or "not found" in out:
        return "missing"
    return "error"


def _materialize(workdir, name, bundle, capture_dir, cas_root):
    dest = os.path.join(workdir, name)
    reconstruct(capture_dir, bundle, dest, cas_root)
    return dest


def replay_session(session_dir, timeout=DEFAULT_TIMEOUT, python=None):
    session = json.load(open(os.path.join(session_dir, "session.json")))
    seed_p = os.path.join(session_dir, "task_seed.json")
    if not os.path.exists(seed_p):
        return None
    seed = json.load(open(seed_p))
    ftp = seed["candidate_fail_to_pass"]
    report = {
        "replay_version": REPLAY_VERSION, "outcome": "setup_failed",
        "verified": False, "reason": None, "error": None, "tests": [],
        "overlaid_test_files": [], "python": None,
        "durations": {}, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    workdir = tempfile.mkdtemp(prefix="agentcap-replay-")
    try:
        python = python or _ensure_python(os.path.dirname(os.path.dirname(session_dir)))
        report["python"] = python
        bundle = os.path.join(workdir, "repo.bundle")
        _bundle(session["repo"], [session["base_sha_start"], session["base_sha_end"]],
                bundle)

        # GREEN first (fail fast): END state must run all ftp tests green
        t0 = time.time()
        end_tree = _materialize(workdir, "end", bundle,
                                os.path.join(session_dir, "env_end"),
                                session["cas_root"])
        tests = [{"node_id": n, "end_status": _run_test(python, end_tree, n, timeout),
                  "start_status": None} for n in ftp]
        report["durations"]["end_s"] = round(time.time() - t0, 1)
        if any(t["end_status"] == "timeout" for t in tests):
            report.update(tests=tests, reason="timeout")
            return report
        runnable = [t for t in tests if t["end_status"] != "missing"]
        if runnable and all(t["end_status"] == "passed" for t in runnable):
            # RED: START state + end's test files overlaid (the test patch)
            t0 = time.time()
            start_tree = _materialize(workdir, "start", bundle,
                                      os.path.join(session_dir, "env_start"),
                                      session["cas_root"])
            for rel in seed["test_files"]:
                src = os.path.join(end_tree, rel)
                if not os.path.exists(src):
                    continue
                dst = os.path.join(start_tree, rel)
                os.makedirs(os.path.dirname(dst) or start_tree, exist_ok=True)
                shutil.copyfile(src, dst)
                report["overlaid_test_files"].append(rel)
            for t in runnable:
                t["start_status"] = _run_test(python, start_tree, t["node_id"], timeout)
            report["durations"]["start_s"] = round(time.time() - t0, 1)
            if any(t["start_status"] == "timeout" for t in runnable):
                report.update(tests=tests, reason="timeout")
                return report
        report["tests"] = tests
        report["outcome"], report["verified"], report["reason"] = _grade(tests)
        return report
    except Exception as e:
        report["error"] = "%s: %s" % (type(e).__name__, e)
        return report
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _write(os.path.join(session_dir, "replay.json"), report)


def replay_all(root=sess.DEFAULT_ROOT, timeout=DEFAULT_TIMEOUT, python=None):
    _, sessions_dir = sess._paths(root)
    python = python or _ensure_python(root)
    results = {}
    for s in sess.list_sessions(root, status="closed"):
        sid = s["session_id"]
        try:
            results[sid] = replay_session(os.path.join(sessions_dir, sid),
                                          timeout=timeout, python=python)
        except Exception as e:  # one bad session must not kill the batch
            results[sid] = {"outcome": "setup_failed", "verified": False,
                            "error": str(e)}
    return results


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.write("\n")
```

Note: `_grade` must ignore rows whose `start_status` stayed `None` because the
green check already failed — the Task 1 implementation already handles this
(`not_green` is decided before start statuses are consulted).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/workspace/agentcap && python3 -m agentcap.tests.test_replay`
Expected: `[ok] S1..S4` lines, `[ok] replay_all`, `ALL PASS`

- [ ] **Step 5: Commit**

```bash
cd ~/workspace/agentcap && git add -A agentcap/ && git commit -m "feat: replay_session/replay_all — reconstruct from bundle, green-then-red check"
```

---

### Task 3: TDD test-patch overlay scenario

**Files:**
- Modify: `agentcap/tests/test_replay.py` (one more scenario)

**Interfaces:**
- Consumes: everything from Task 2. No new production surface expected — this scenario *proves* the overlay path; if it fails, fix `replay_session`, not the test.

- [ ] **Step 1: Write the test** — append to `main()`:

```python
    # --- S5 TDD: fail_to_pass test authored mid-session, absent at base ---
    files_tdd = {"mod.py": MOD_RED}          # no test file at start
    edits_tdd = {"mod.py": MOD_GREEN, "tests/test_mod.py": TEST_F}
    _, _, _, sdir5 = seeded(tmp, "tdd", files_tdd, edits_tdd)
    rep5 = R.replay_session(sdir5, python=py)
    if rep5["outcome"] != "red_green" or not rep5["verified"]:
        fails.append("S5 TDD overlay should verify: %s / %s"
                     % (rep5["outcome"], rep5.get("error")))
    elif "tests/test_mod.py" not in rep5["overlaid_test_files"]:
        fails.append("S5 test file not overlaid: %s" % rep5["overlaid_test_files"])
    else:
        print("[ok] S5 TDD: end-authored test overlaid onto base, red verified")
```

- [ ] **Step 2: Run test**

Run: `cd ~/workspace/agentcap && python3 -m agentcap.tests.test_replay`
Expected: PASS if Task 2's overlay code is correct; if it fails, debug `replay_session` overlay path.

- [ ] **Step 3: Commit**

```bash
cd ~/workspace/agentcap && git add agentcap/tests/test_replay.py && git commit -m "test: TDD test-patch overlay scenario for replay"
```

---

### Task 4: CLI subcommand

**Files:**
- Modify: `agentcap/cli.py`

**Interfaces:**
- Produces: `agentcap replay [--root ROOT] [--session ID] [--timeout N]` printing a summary json `{"red_green": n, "green_only": n, "not_green": n, "setup_failed": n, "no_seed": n, "verified": n}`.

- [ ] **Step 1: Add parser** — in `cli.py` after the `vl` (value) parser block:

```python
    rp = sub.add_parser("replay", help="re-run fail_to_pass against the reconstructed "
                                       "artifact; verified is earned here")
    rp.add_argument("--root", default=sess.DEFAULT_ROOT)
    rp.add_argument("--session", default=None, help="replay one session id")
    rp.add_argument("--timeout", type=int, default=300)
```

and the dispatch arm after the `value` arm:

```python
    elif a.cmd == "replay":
        from . import replay as RP
        if a.session:
            sdir = os.path.join(a.root, "sessions", a.session)
            print(json.dumps(RP.replay_session(sdir, timeout=a.timeout), indent=2))
        else:
            results = RP.replay_all(root=a.root, timeout=a.timeout)
            summary = {"red_green": 0, "green_only": 0, "not_green": 0,
                       "setup_failed": 0, "no_seed": 0, "verified": 0}
            for r in results.values():
                if r is None:
                    summary["no_seed"] += 1
                else:
                    summary[r["outcome"]] += 1
                    summary["verified"] += bool(r.get("verified"))
            print(json.dumps(summary, indent=2))
```

`cli.py` needs `import os` added to its imports (it currently lacks it). Also
extend the module docstring's command list with the replay line.

- [ ] **Step 2: Smoke test**

Run: `cd ~/workspace/agentcap && python3 -m agentcap replay --help`
Expected: usage text with `--root/--session/--timeout`.

- [ ] **Step 3: Commit**

```bash
cd ~/workspace/agentcap && git add agentcap/cli.py && git commit -m "feat: agentcap replay subcommand"
```

---

### Task 5: export consumes replay.json

**Files:**
- Modify: `agentcap/export.py` (`export_session`, after `_task_view` call)
- Modify: `agentcap/tests/test_replay.py` (assert export integration)

**Interfaces:**
- Produces: `task.json.verified = replay.verified` (still `False` when no replay ran); `task.json.replay_outcome`; `record.json.replay = {"outcome", "verified"} | None`.

- [ ] **Step 1: Write the failing test** — append to `test_replay.py` `main()`:

```python
    # --- export integration: verified flows from replay.json ---
    out1 = os.path.join(tmp, "out_rg")
    E.export_all(root=store1, out=out1)
    task1 = json.load(open(os.path.join(out1, sid1, "task.json")))
    rec1 = json.load(open(os.path.join(out1, sid1, "record.json")))
    if not task1["verified"] or task1.get("replay_outcome") != "red_green":
        fails.append("export should carry earned verified: %s / %s"
                     % (task1["verified"], task1.get("replay_outcome")))
    elif (rec1.get("replay") or {}).get("outcome") != "red_green":
        fails.append("record should embed replay outcome: %s" % rec1.get("replay"))
    else:
        print("[ok] export: verified earned via replay, embedded in task+record")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/workspace/agentcap && python3 -m agentcap.tests.test_replay`
Expected: FAIL on "export should carry earned verified" (task.verified is hard-coded False)

- [ ] **Step 3: Implement** — in `export.py::export_session`, replace the seed/task block:

```python
    seed = _load(os.path.join(session_dir, "task_seed.json")) or T.extract_seed(session_dir)
    replay_rep = _load(os.path.join(session_dir, "replay.json"))
    task = None
    if seed:
        runs = tooltrace.test_runs(traj["log_path"], traj.get("agent"))
        task = _task_view(session, seed, val, steps, runs, repo_name)
        if replay_rep:
            task["verified"] = bool(replay_rep.get("verified"))
            task["replay_outcome"] = replay_rep.get("outcome")
        _write(os.path.join(sdir, "task.json"), task)
```

and in the `record` dict add one key after `"verify": ...`:

```python
        "replay": ({"outcome": replay_rep.get("outcome"),
                    "verified": replay_rep.get("verified")} if replay_rep else None),
```

Also update `_task_view`'s `"verified": False` comment to
`# earned by `agentcap replay`; stays False until a replay grades red_green`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/workspace/agentcap && python3 -m agentcap.tests.test_replay`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd ~/workspace/agentcap && git add agentcap/export.py agentcap/tests/test_replay.py && git commit -m "feat: export consumes replay.json — verified is earned, not asserted"
```

---

### Task 6: full suite + real-store reading

**Files:** none new.

- [ ] **Step 1: Run every existing test module** (regression):

```bash
cd ~/workspace/agentcap && for t in autoverify export join roundtrip runtime session taskseed testparse value watcher replay; do python3 -m agentcap.tests.test_$t || echo "FAILED: $t"; done
```

Expected: every module ends with `ALL PASS`, no `FAILED:` lines.

- [ ] **Step 2: Run the gate on the real store** (the 3 seeded papyrus sessions):

```bash
cd ~/workspace/agentcap && python3 -m agentcap replay
```

Expected (honest prediction from the 2026-07-06 manual experiment): mostly
`not_green` because papyrus tests read gitignored `output/` fixtures. Whatever
the outcome, record the summary in the session notes — this is the first
verified-rate reading.

- [ ] **Step 3: Commit any straggler + report**

```bash
cd ~/workspace/agentcap && git status --porcelain
```

Expected: clean tree; report the real-store summary to the user.
