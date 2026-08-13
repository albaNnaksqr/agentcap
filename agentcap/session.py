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

from . import gitutil as g
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


def resolve_repo(session):
    """-> (path, fell_back). The repo a consumer should read objects from.

    Captures taken inside a throwaway worktree name that worktree as `repo`, and
    batch harnesses delete those (run_batch.py --rm-worktree does it by default in
    some flows). The recorded parent still holds every object, so fall back to it
    rather than declaring the session unreplayable. Never rewrite session["repo"]:
    it is where the capture happened, not where the objects live today.
    """
    repo = session["repo"]
    if os.path.isdir(repo):
        return repo, False
    alt = session.get("repo_object_source")
    if alt and os.path.isdir(alt):
        return alt, True
    raise RuntimeError("repo gone and no usable object source: %s (recorded parent: %s)"
                       % (repo, alt))


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
        # None for a normal repo; for a throwaway worktree, the repo holding the
        # objects — the only thing that can rebuild this base once it is deleted.
        "repo_object_source": g.object_source(repo),
        # `owner/name` from the origin remote. Captured HERE and not derived at
        # export time on purpose: the worktree a batch runs in is deleted, and
        # after that the only surviving clue is its directory name, which carries
        # an issue number and a timestamp and is therefore useless as an identity.
        "repo_identity": g.repo_identity(repo),
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


def end_session(session_id=None, repo=None, confidence="high", root=DEFAULT_ROOT,
                auto_verify=True):
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

    # verify NOW, while base_sha is still reachable — a later rebase/gc makes the
    # capture permanently unverifiable, so the close-time report banks the result.
    # Best-effort: a verify failure must never make close fail (watcher path).
    if auto_verify:
        try:
            verify_session(session_id, root=root)
        except Exception:
            pass
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
    # the persisted verification talks about an env_end this reopen supersedes
    vj = os.path.join(sessions, session_id, "verify.json")
    if os.path.exists(vj):
        os.remove(vj)
    return session


def verify_session(session_id, root=DEFAULT_ROOT):
    """Verify both env snapshots reconstruct + report benchmark eligibility for the pair."""
    _, sessions = _paths(root)
    sdir = os.path.join(sessions, session_id)
    session = json.load(open(os.path.join(sdir, "session.json")))
    # same reason as replay: a deleted capture worktree must not make an already
    # captured session unverifiable when the parent repo still has the objects
    repo, _ = resolve_repo(session)
    blobs = session["cas_root"]
    ok_s, rep_s = verify_capture(os.path.join(sdir, "env_start"), repo, cas_root=blobs)
    ok_e, rep_e = verify_capture(os.path.join(sdir, "env_end"), repo, cas_root=blobs)

    report = {
        "session_id": session_id,
        "start": rep_s,
        "end": rep_e,
        "join_confidence": None,
        "verified": ok_s and ok_e,
        "benchmark_eligible": False,
    }
    # persist so `value` can consume the verification result without re-reconstructing
    _write_json(os.path.join(sdir, "verify.json"), report)
    return refresh_eligibility(session_id, root=root)


def refresh_eligibility(session_id, root=DEFAULT_ROOT):
    """Recompute benchmark eligibility in a persisted verify.json — cheap (no
    reconstruction). Sessions auto-verify at close, usually before any join exists;
    when the join lands later this folds it into the banked report."""
    _, sessions = _paths(root)
    sdir = os.path.join(sessions, session_id)
    vp = os.path.join(sdir, "verify.json")
    if not os.path.exists(vp):
        return None
    report = json.load(open(vp))
    session = json.load(open(os.path.join(sdir, "session.json")))
    jp = os.path.join(sdir, "join.json")
    join_conf = json.load(open(jp))["join_confidence"] if os.path.exists(jp) else None

    report["join_confidence"] = join_conf
    report["benchmark_eligible"] = (
        report["start"]["benchmark_eligible"] and report["end"]["benchmark_eligible"]
        and session["start_confidence"] == "high"
        and session["end_confidence"] == "high"
        and join_conf == "high"          # v0.2: a benchmark unit needs a high-confidence join
    )
    _write_json(vp, report)
    return report
