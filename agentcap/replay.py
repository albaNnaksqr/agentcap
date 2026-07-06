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
