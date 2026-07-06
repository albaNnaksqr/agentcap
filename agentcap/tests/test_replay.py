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

    # --- replay_all: batch shape over the store ---
    results = R.replay_all(root=store1, python=py)
    if results.get(sid1, {}).get("outcome") != "red_green":
        fails.append("replay_all should reuse/recompute S1: %s" % results)
    else:
        print("[ok] replay_all: batch summary over the store")

    if fails:
        print("\n".join("[FAIL] " + f for f in fails))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
