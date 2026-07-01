"""Mine a candidate verifiable-task seed from a joined session's trajectory, by
READING the agent's test-run outputs — nothing is executed. The seed is labeled
verified=false: it is observed validation evidence, not self-verification.

Method (see the method write-up):
  A. pull test-run events from the trajectory (tooltrace)
  B. parse each pytest output -> {node: status} + counts
  C. timeline-diff runs -> candidate FAIL_TO_PASS (red then green) / PASS_TO_PASS
  D. ground against the code delta + env_end manifest (test-only change = gaming risk)
  E. emit task_seed.json with a confidence grade
"""
import json
import os
import re

from . import session as sess
from . import tooltrace
from .snapshot import load_manifest

# pytest node results
_FAILED = re.compile(r'^(?:FAILED|ERROR)\s+(\S+::\S+)', re.M)          # -q summary + verbose
_FAILED_V = re.compile(r'^(\S+::\S+)\s+(?:FAILED|ERROR)\b', re.M)      # verbose "node FAILED"
_PASSED_V = re.compile(r'^(\S+::\S+)\s+PASSED\b', re.M)                # verbose only
_PASSED_V2 = re.compile(r'^PASSED\s+(\S+::\S+)', re.M)
_COUNTS = re.compile(r'(\d+)\s+(passed|failed|error|errors|skipped)')


def parse_pytest(output):
    failed = set(_FAILED.findall(output)) | set(_FAILED_V.findall(output))
    passed = set(_PASSED_V.findall(output)) | set(_PASSED_V2.findall(output))
    passed -= failed
    counts = {}
    for n, kind in _COUNTS.findall(output):
        counts[kind.rstrip("s")] = counts.get(kind.rstrip("s"), 0) + int(n)
    return {"failed": failed, "passed": passed, "counts": counts}


def _timeline(runs):
    """runs sorted by time. Return (candidate_ftp, candidate_ptp, evidence)."""
    parsed = []
    for r in sorted(runs, key=lambda r: (r.get("ts") or "", r["idx"])):
        p = parse_pytest(r["output"])
        parsed.append((r, p))
    if not parsed:
        return set(), set(), []

    ever_failed = set()
    for _, p in parsed:
        ever_failed |= p["failed"]
    last = parsed[-1][1]
    first = parsed[0][1]

    # red -> green: failed at some point, not failed in the last run, and either
    # explicitly passed later or the last run has no failures at all.
    ftp = set()
    for n in ever_failed:
        if n in last["failed"]:
            continue
        passed_later = any(n in p["passed"] for _, p in parsed)
        if passed_later or not last["failed"]:
            ftp.add(n)

    # stayed green (thin signal): passed first and last, never failed.
    ptp = {n for n in first["passed"] if n in last["passed"] and n not in ever_failed}

    evidence = [{
        "idx": r["idx"], "ts": r.get("ts"), "cmd": r["cmd"], "counts": p["counts"],
    } for r, p in parsed]
    return ftp, ptp, evidence


def _load_json(path):
    return json.load(open(path)) if os.path.exists(path) else None


def extract_seed(session_dir):
    session = _load_json(os.path.join(session_dir, "session.json"))
    join = _load_json(os.path.join(session_dir, "join.json"))
    delta = _load_json(os.path.join(session_dir, "delta.json"))
    if not (session and join and delta):
        return None  # need a joined session with a delta

    traj = join["trajectory"]
    runs = tooltrace.test_runs(traj["log_path"], traj.get("agent"))
    if not runs:
        return None
    ftp, ptp, evidence = _timeline(runs)

    # D. ground: test files must exist in env_end; classify the delta.
    end_paths = {e["path"] for e in load_manifest(os.path.join(session_dir, "env_end"))["entries"]}
    ftp = {n for n in ftp if n.split("::", 1)[0] in end_paths}
    ptp = {n for n in ptp if n.split("::", 1)[0] in end_paths}

    changed = sorted(set(delta["added"]) | set(delta["modified"]))
    test_files = sorted({n.split("::", 1)[0] for n in (ftp | ptp)}
                        | {c for c in changed if _looks_test(c)})
    source_delta = [c for c in changed if not _looks_test(c)]
    test_only = bool(changed) and not source_delta

    # counts-only evidence: failures seen earlier, zero in the last run
    counts_only = bool(evidence) and evidence[-1]["counts"].get("failed", 0) == 0 \
        and any(e["counts"].get("failed", 0) for e in evidence)
    if not ftp and not counts_only:
        return None

    # NOTE: no strong/weak/counts quality tier here anymore. Judging trajectory
    # value (incl. that agent-authored tests are normal) lives in value.assess().
    # These are neutral candidate signals a sandbox-builder can use.
    seed = {
        "base_commit": session["base_sha_start"],
        "candidate_fail_to_pass": sorted(ftp),
        "candidate_pass_to_pass": sorted(ptp),
        "test_files": test_files,
        "source_delta": source_delta,
        "test_only_delta": test_only,       # a flag, not a verdict
        "evidence": evidence,
        "verified": False,   # we did not run anything — observed evidence only
    }
    _write(os.path.join(session_dir, "task_seed.json"), seed)
    return seed


def _looks_test(path):
    b = os.path.basename(path)
    return b.startswith("test_") or b.endswith("_test.py") or "/tests/" in path or path.startswith("tests/")


def seed_all(root=sess.DEFAULT_ROOT):
    _, sessions_dir = sess._paths(root)
    results = {}
    for s in sess.list_sessions(root):
        try:
            results[s["session_id"]] = extract_seed(os.path.join(sessions_dir, s["session_id"]))
        except Exception as e:  # a bad log must not kill the batch
            results[s["session_id"]] = {"error": str(e)}
    return results


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.write("\n")
