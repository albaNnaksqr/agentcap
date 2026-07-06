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
