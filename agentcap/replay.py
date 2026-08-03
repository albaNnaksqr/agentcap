"""Replay gate: re-execute a seeded session's fail_to_pass tests against the
reconstructed exportable artifact. `verified` is EARNED here, not observed:
the END state must run all-green and the START state — with the end's test
files overlaid (the test patch, SWE-bench style) — must not pass. Replaying
from a bundle, never the live repo, is what makes verified mean
"self-contained replayable". No dependency install, no docker (L2-, not L3).

Two provenance axes are recorded alongside the verdict, because some repos
cannot produce a self-contained bundle at all (a blob:none partial clone makes
`git bundle create` fetch every historical blob from the promisor remote, which
fails on large repos). With --allow-local-repo the artifact is rebuilt from the
live local repo instead: red/green is still EARNED by re-execution, but the
result is only reproducible on this machine. That downgrade is never silent —
see `artifact_source` / `portability` / `interpreter_source` in the report.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import session as sess
from . import testparse
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


_PY_RE = re.compile(r"(/[^\s'\"]+/bin/)(?:python[0-9.]*|pytest)\b")


def _interpreter_from_seed(seed):
    """The interpreter the session actually ran its tests with, or None.

    The ambient python can import pytest but rarely the repo's third-party
    deps, so a recorded venv is often the only one that can run the tests at
    all. Only paths that exist on disk AND can import pytest are returned —
    never a guess. Reusing it costs the clean-room property, so the caller
    records `interpreter_source`.
    """
    counts = {}
    for e in seed.get("evidence") or []:
        for bindir in _PY_RE.findall(e.get("cmd") or ""):
            for name in ("python3", "python"):
                cand = os.path.join(bindir, name)
                if os.path.exists(cand):
                    counts[cand] = counts.get(cand, 0) + 1
                    break
    for cand, _ in sorted(counts.items(), key=lambda kv: -kv[1]):
        if subprocess.run([cand, "-c", "import pytest"],
                          capture_output=True).returncode == 0:
            return cand
    return None


def _is_partial_clone(repo):
    """True for a blob:none/tree:0 clone — history bundling will hit the remote."""
    p = subprocess.run(["git", "-C", repo, "config", "--get-regexp",
                        r"^remote\..*\.partialclonefilter$"], capture_output=True)
    return p.returncode == 0 and bool(p.stdout.strip())


def _artifact_source(session, workdir, allow_local_repo):
    """-> (source, kind, fallback_reason). A bundle is the honest default; the
    live repo is a machine-local fallback that must be asked for explicitly."""
    bundle = os.path.join(workdir, "repo.bundle")
    try:
        _bundle(session["repo"], [session["base_sha_start"], session["base_sha_end"]],
                bundle)
        return bundle, "bundle", None
    except Exception as e:
        if not allow_local_repo:
            raise
        reason = "%s: %s" % (type(e).__name__, str(e).strip().splitlines()[0] if str(e).strip() else "")
        if _is_partial_clone(session["repo"]):
            reason += " (source is a partial clone)"
        return session["repo"], "local_repo", reason


_PRUNE = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def runner_for(node_id):
    """pytest ids carry a path (`tests/t.py::test_x`); unittest ids are dotted
    (`pkg.mod.Case.test_x`) and only `python -m unittest` can run them."""
    return "pytest" if "::" in node_id else "unittest"


def _unittest_root(tree, node_id):
    """Directory that must be importable for a dotted id to resolve, relative to
    the tree. `unittest discover -s tests` yields ids relative to `tests/`, so the
    module is not importable from the repo root — find the module file and hand
    back the directory above it. None when it cannot be located."""
    parts = node_id.split(".")
    py_files = []
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE]
        for fn in filenames:
            if fn.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(dirpath, fn), tree))
    for i in range(len(parts), 0, -1):
        rel = os.path.join(*parts[:i]) + ".py"
        if rel in py_files:
            return "."
        hits = [p for p in py_files if p.endswith(os.sep + rel)]
        if len(hits) == 1:
            return hits[0][:-len(rel)].rstrip(os.sep)
        if len(hits) > 1:
            return None          # ambiguous basename -> refuse, same as taskseed
    return None


def _run_unittest(python, tree, node_id, timeout, pythonpath):
    root = _unittest_root(tree, node_id)
    if root is None:
        return "missing"
    pp = [root] + [c for c in (pythonpath or ["."]) if c != root]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(pp),
               PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run([python, "-m", "unittest", "-v", node_id],
                           cwd=tree, env=env, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout"
    if p.returncode == 0:
        return "passed"
    out = p.stdout + p.stderr
    if "Ran 0 tests" in out or testparse._U_LOADER_STUB in out or "has no attribute" in out:
        return "missing"          # id does not exist here — mirrors pytest collection
    if "Ran " not in out:
        return "error"            # the runner never started (bad interpreter, etc.)
    return "failed"


def _run_test(python, tree, node_id, timeout, pythonpath=None):
    """-> passed | failed | missing | error | timeout, for one node id.
    Per-id runs keep one stale id from aborting the whole batch (pytest
    refuses to run anything when any given id fails collection).
    pythonpath: repo-relative components (cwd=tree) so the reconstructed package
    is imported for src/ or python/ layouts; defaults to the tree root."""
    if runner_for(node_id) == "unittest":
        return _run_unittest(python, tree, node_id, timeout, pythonpath)
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


def replay_session(session_dir, timeout=DEFAULT_TIMEOUT, python=None,
                   allow_local_repo=False):
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
        "artifact_source": None, "portability": None,
        "artifact_fallback_reason": None, "interpreter_source": None,
        "interpreter_fallback_reason": None,
        "durations": {}, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    workdir = tempfile.mkdtemp(prefix="agentcap-replay-")
    try:
        # The clean-room interpreter stays the default: a session's own venv can
        # run tests the artifact does not actually declare. It is used only as a
        # last resort below, when the clean one cannot collect anything at all.
        fixed_python = bool(python)
        python = python or _ensure_python(os.path.dirname(os.path.dirname(session_dir)))
        report["interpreter_source"] = "explicit" if fixed_python else "ambient"
        report["python"] = python

        source, kind, fb_reason = _artifact_source(session, workdir, allow_local_repo)
        report["artifact_source"] = kind
        report["portability"] = "self_contained" if kind == "bundle" else "machine_local"
        report["artifact_fallback_reason"] = fb_reason

        # GREEN first (fail fast): the END state must run every ftp test green
        t0 = time.time()
        end_tree = _materialize(workdir, "end", source,
                                os.path.join(session_dir, "env_end"),
                                session["cas_root"])
        tests = [{"node_id": n, "runner": runner_for(n),
                  "end_status": _run_test(python, end_tree, n, timeout, pp),
                  "start_status": None} for n in ftp]

        # Nothing collected at all usually means the interpreter cannot import the
        # repo's third-party deps, not that the ids are stale. Retry once with the
        # interpreter the session itself used — a labelled downgrade, and the only
        # way such a repo gets any verdict.
        if not fixed_python and all(t["end_status"] == "missing" for t in tests):
            recorded = _interpreter_from_seed(seed)
            # compare invocation paths, NOT realpath: a venv's bin/python3 is a
            # symlink to the base interpreter, and it is the invocation path that
            # gives it its site-packages. realpath would collapse them into one.
            if recorded and os.path.abspath(recorded) != os.path.abspath(python):
                retry = [{"node_id": n, "runner": runner_for(n),
                          "end_status": _run_test(recorded, end_tree, n, timeout, pp),
                          "start_status": None} for n in ftp]
                if any(t["end_status"] != "missing" for t in retry):
                    tests, python = retry, recorded
                    report["python"] = recorded
                    report["interpreter_source"] = "session_recorded"
                    report["interpreter_fallback_reason"] = (
                        "clean interpreter collected no fail_to_pass id; reused the "
                        "interpreter recorded in the session")
        report["durations"]["end_s"] = round(time.time() - t0, 1)
        if any(t["end_status"] == "timeout" for t in tests):
            report.update(tests=tests, reason="timeout")
            return report
        runnable = [t for t in tests if t["end_status"] != "missing"]
        if runnable and all(t["end_status"] == "passed" for t in runnable):
            # RED: START state + the end's test files overlaid (the test patch)
            t0 = time.time()
            start_tree = _materialize(workdir, "start", source,
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


def replay_all(root=sess.DEFAULT_ROOT, timeout=DEFAULT_TIMEOUT, python=None,
               allow_local_repo=False):
    _, sessions_dir = sess._paths(root)
    results = {}
    for s in sess.list_sessions(root, status="closed"):
        sid = s["session_id"]
        try:
            # python=None on purpose: each session resolves its own interpreter
            # (recorded venv first), which _ensure_python cannot do store-wide.
            results[sid] = replay_session(os.path.join(sessions_dir, sid),
                                          timeout=timeout, python=python,
                                          allow_local_repo=allow_local_repo)
        except Exception as e:  # one bad session must not kill the batch
            results[sid] = {"outcome": "setup_failed", "verified": False,
                            "error": str(e)}
    return results


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.write("\n")
