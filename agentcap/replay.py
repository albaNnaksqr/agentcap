"""Replay gate: re-execute a seeded session's fail_to_pass tests against the
reconstructed exportable artifact. `verified` is EARNED here, not observed:
the END state must run all-green and the START state — with the end's test
files overlaid (the test patch, SWE-bench style) — must not pass. Replaying
from a bundle, never the live repo, is what makes verified mean
"self-contained replayable". No dependency install, no docker (L2-, not L3).
"""
import json
import os
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


def _run_test(python, tree, node_id, timeout, pythonpath=None):
    """-> passed | failed | missing | error | timeout, for one node id.
    Per-id runs keep one stale id from aborting the whole batch (pytest
    refuses to run anything when any given id fails collection).
    pythonpath: repo-relative components (cwd=tree) so the reconstructed package
    is imported for src/ or python/ layouts; defaults to the tree root."""
    pp = os.pathsep.join(pythonpath) if pythonpath else "."
    env = dict(os.environ, PYTHONPATH=pp, PYTHONDONTWRITEBYTECODE="1")
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
    """Rebuild the exportable artifact in a temp dir and grade it. Writes
    <session_dir>/replay.json; returns the report (None when no seed)."""
    seed_p = os.path.join(session_dir, "task_seed.json")
    if not os.path.exists(seed_p):
        return None
    session = json.load(open(os.path.join(session_dir, "session.json")))
    seed = json.load(open(seed_p))
    ftp = seed["candidate_fail_to_pass"]
    pp = seed.get("test_pythonpath") or None
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

        # GREEN first (fail fast): the END state must run every ftp test green
        t0 = time.time()
        end_tree = _materialize(workdir, "end", bundle,
                                os.path.join(session_dir, "env_end"),
                                session["cas_root"])
        tests = [{"node_id": n, "end_status": _run_test(python, end_tree, n, timeout, pp),
                  "start_status": None} for n in ftp]
        report["durations"]["end_s"] = round(time.time() - t0, 1)
        if any(t["end_status"] == "timeout" for t in tests):
            report.update(tests=tests, reason="timeout")
            return report
        runnable = [t for t in tests if t["end_status"] != "missing"]
        if runnable and all(t["end_status"] == "passed" for t in runnable):
            # RED: START state + the end's test files overlaid (the test patch)
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
                t["start_status"] = _run_test(python, start_tree, t["node_id"], timeout, pp)
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
