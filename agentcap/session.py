"""Session layer: a captured agent session = a start snapshot + an end snapshot of
the same repo, plus the delta between them. Both `mark-start`/`mark-end` (high
confidence, manual) and the watcher (best-effort) produce sessions through here.

Layout under <root>:
    <root>/blobs/                     shared CAS (dedup across start/end and sessions)
    <root>/sessions/<session_id>/
        session.json                  boundary metadata + confidence
        env_start/  env_end/          snapshot capture dirs (manifest + diffs)
        delta.json                    {added, modified, deleted} start->end
"""
import datetime
import json
import os
import uuid

from .snapshot import snapshot, load_manifest
from .verify import verify as verify_capture

DEFAULT_ROOT = os.path.expanduser("~/.agentcap")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _reponame(repo):
    return os.path.basename(os.path.abspath(repo.rstrip("/"))) or "repo"


def _write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.write("\n")


def _paths(root):
    return os.path.join(root, "blobs"), os.path.join(root, "sessions")


def start_session(repo, agent="manual", confidence="high", root=DEFAULT_ROOT,
                  session_id=None, extra=None):
    repo = os.path.abspath(repo)
    blobs, sessions = _paths(root)
    session_id = session_id or "%s-%s-%s" % (
        agent, _reponame(repo),
        datetime.datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6])
    sdir = os.path.join(sessions, session_id)
    if os.path.exists(sdir):
        raise RuntimeError("session already exists: %s" % session_id)
    os.makedirs(sdir)

    meta, _ = snapshot(repo, os.path.join(sdir, "env_start"), cas_root=blobs)
    session = {
        "session_id": session_id,
        "agent": agent,
        "repo": repo,
        "status": "open",
        "start_confidence": confidence,
        "end_confidence": None,
        "created_at": _now(),
        "closed_at": None,
        "base_sha_start": meta["base_sha"],
        "base_sha_end": None,
        "cas_root": blobs,
        "extra": extra or {},
    }
    _write_json(os.path.join(sdir, "session.json"), session)
    return session_id, session


def list_sessions(root=DEFAULT_ROOT, status=None):
    _, sessions = _paths(root)
    if not os.path.isdir(sessions):
        return []
    out = []
    for sid in os.listdir(sessions):
        sj = os.path.join(sessions, sid, "session.json")
        if not os.path.exists(sj):
            continue
        s = json.load(open(sj))
        if status is None or s["status"] == status:
            out.append(s)
    return out


def find_open_session(repo, root=DEFAULT_ROOT):
    repo = os.path.abspath(repo)
    _, sessions = _paths(root)
    if not os.path.isdir(sessions):
        return None
    hits = []
    for sid in os.listdir(sessions):
        sj = os.path.join(sessions, sid, "session.json")
        if not os.path.exists(sj):
            continue
        s = json.load(open(sj))
        if s["repo"] == repo and s["status"] == "open":
            hits.append((s["created_at"], sid))
    return sorted(hits)[-1][1] if hits else None  # most recent open one


def _delta(start_dir, end_dir):
    s = {e["path"]: e["content_hash"] for e in load_manifest(start_dir)["entries"]}
    e = {x["path"]: x["content_hash"] for x in load_manifest(end_dir)["entries"]}
    added = sorted(p for p in e if p not in s)
    deleted = sorted(p for p in s if p not in e)
    modified = sorted(p for p in e if p in s and e[p] != s[p])
    return {"added": added, "modified": modified, "deleted": deleted}


def end_session(session_id=None, repo=None, confidence="high", root=DEFAULT_ROOT):
    _, sessions = _paths(root)
    if session_id is None:
        if repo is None:
            raise ValueError("need session_id or repo")
        session_id = find_open_session(repo, root)
        if session_id is None:
            raise RuntimeError("no open session for %s" % repo)
    sdir = os.path.join(sessions, session_id)
    sj = os.path.join(sdir, "session.json")
    session = json.load(open(sj))
    if session["status"] != "open":
        raise RuntimeError("session %s is not open" % session_id)

    blobs = session["cas_root"]
    meta, _ = snapshot(session["repo"], os.path.join(sdir, "env_end"), cas_root=blobs)
    delta = _delta(os.path.join(sdir, "env_start"), os.path.join(sdir, "env_end"))
    _write_json(os.path.join(sdir, "delta.json"), delta)

    session["status"] = "closed"
    session["end_confidence"] = confidence
    session["closed_at"] = _now()
    session["base_sha_end"] = meta["base_sha"]
    _write_json(sj, session)
    return session, delta


def reopen_session(session_id, root=DEFAULT_ROOT):
    """Resume a session the watcher closed prematurely: the agent went idle past the
    threshold, then wrote to the same log again. We keep the original env_start and
    flip the session back to open; env_end + delta are recomputed against that start
    when it finally closes, so the captured delta spans the whole session (the mid-idle
    close is undone). `reopens` records how many idle gaps the session survived."""
    _, sessions = _paths(root)
    sj = os.path.join(sessions, session_id, "session.json")
    session = json.load(open(sj))
    if session["status"] == "open":
        return session
    session["status"] = "open"
    session["closed_at"] = None
    session["end_confidence"] = None
    session["base_sha_end"] = None
    session["reopens"] = session.get("reopens", 0) + 1
    _write_json(sj, session)
    return session


def verify_session(session_id, root=DEFAULT_ROOT):
    """Verify both env snapshots reconstruct + report benchmark eligibility for the pair."""
    _, sessions = _paths(root)
    sdir = os.path.join(sessions, session_id)
    session = json.load(open(sdir + "/session.json")) if False else json.load(
        open(os.path.join(sdir, "session.json")))
    repo, blobs = session["repo"], session["cas_root"]
    ok_s, rep_s = verify_capture(os.path.join(sdir, "env_start"), repo, cas_root=blobs)
    ok_e, rep_e = verify_capture(os.path.join(sdir, "env_end"), repo, cas_root=blobs)

    join_path = os.path.join(sdir, "join.json")
    join_conf = json.load(open(join_path))["join_confidence"] if os.path.exists(join_path) else None

    eligible = (
        rep_s["benchmark_eligible"] and rep_e["benchmark_eligible"]
        and session["start_confidence"] == "high"
        and session["end_confidence"] == "high"
        and join_conf == "high"          # v0.2: a benchmark unit needs a high-confidence join
    )
    report = {
        "session_id": session_id,
        "start": rep_s,
        "end": rep_e,
        "join_confidence": join_conf,
        "verified": ok_s and ok_e,
        "benchmark_eligible": eligible,
    }
    # persist so `value` can consume the verification result without re-reconstructing
    _write_json(os.path.join(sdir, "verify.json"), report)
    return report
