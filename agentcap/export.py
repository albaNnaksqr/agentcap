"""Export: one canonical record per session, plus a task-instance view for RL/DPO.

Layout, self-contained (reconstructs WITHOUT the original repo):
    <out>/export_summary.json
    <out>/<session_id>/
        record.json          canonical record: provenance + signals, no abs paths
        trajectory.jsonl     normalized steps (claude/codex -> one schema)
        task.json            RL task instance (when a seed exists)
        env/repo.bundle      git bundle carrying base_sha_start/end
        env/env_start|end/   manifest + diffs + runtime evidence
        env/blobs/           referenced untracked blobs

The task instance is the RL/DPO product: problem statement (first user message),
FAIL_TO_PASS/PASS_TO_PASS + observed test commands as the verifier spec, and the
captured trajectory embedded as `reference` — the chosen side of a future DPO pair;
`task_key` clusters sessions that solve the same tests. The exit is gated: secret
hits quarantine the whole session (trajectory, diffs, untracked blobs, .env names
are scanned; bundle HISTORY is not — exporting a repo means you own its history).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess

from . import gitutil as g
from . import session as sess
from . import tooltrace

_TIER = {"low": 0, "medium": 1, "high": 2}

RECORD_VERSION = 1
TASK_VERSION = 1

_SECRETS = [
    ("private_key", re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ("github_token", re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b'
                                r'|\bgithub_pat_[A-Za-z0-9_]{22,}\b')),
    ("aws_key", re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ("slack_token", re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b')),
    ("api_key", re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b')),
]
_ENV_OK = (".env.example", ".env.sample", ".env.template")


# --- trajectory normalization: claude/codex log -> one step schema -------------

def _text_blocks(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            out.append(b["text"])
        elif isinstance(b, str):
            out.append(b)
    return "\n".join(out)


def _norm_claude(path):
    steps = []
    for d in _jsonl(path):
        ts = d.get("timestamp")
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if d.get("type") == "user":
            if isinstance(content, str):
                steps.append({"type": "user_message", "ts": ts, "text": content})
                continue
            for b in content or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    steps.append({"type": "user_message", "ts": ts, "text": b.get("text", "")})
                elif b.get("type") == "tool_result":
                    steps.append({"type": "tool_result", "ts": ts,
                                  "output": _text_blocks(b.get("content"))})
        elif d.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    steps.append({"type": "assistant_message", "ts": ts,
                                  "text": b.get("text", "")})
                elif b.get("type") == "tool_use":
                    steps.append({"type": "tool_call", "ts": ts,
                                  "name": b.get("name"), "input": b.get("input")})
    return steps


def _norm_codex(path):
    steps = []
    for d in _jsonl(path):
        ts = d.get("timestamp")
        p = d.get("payload") if isinstance(d.get("payload"), dict) else d
        pt = p.get("type")
        if pt == "message":
            kind = "user_message" if p.get("role") == "user" else "assistant_message"
            steps.append({"type": kind, "ts": ts, "text": _text_blocks(p.get("content"))})
        elif pt == "function_call":
            args = p.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            steps.append({"type": "tool_call", "ts": ts,
                          "name": p.get("name"), "input": args})
        elif pt == "function_call_output":
            out = p.get("output")
            steps.append({"type": "tool_result", "ts": ts,
                          "output": out if isinstance(out, str) else _text_blocks(out)})
    return steps


def normalize_trajectory(log_path, agent):
    steps = _norm_codex(log_path) if agent == "codex" else _norm_claude(log_path)
    for i, s in enumerate(steps):
        s["idx"] = i
    return steps


# --- redaction gate -------------------------------------------------------------

def _scan_text(text, where, hits):
    for name, rx in _SECRETS:
        if rx.search(text):
            hits.append({"kind": name, "where": where})


def _git_blob(repo, oid):
    """Tracked content lives in the repo, not the CAS — a manifest entry's
    content_hash IS the git blob oid. None when it cannot be read (repo gone,
    object pruned), which the caller must treat as "assume the worst"."""
    if not repo or not oid:
        return None
    p = subprocess.run(["git", "-C", repo, "cat-file", "blob", oid],
                       capture_output=True)
    return p.stdout if p.returncode == 0 else None


def _dotenv_hit(env, e, session, hits):
    """A .env* entry: quarantine on the name only when the file is UNTRACKED.

    A tracked .env is already part of the repo we ship inside the artifact, so
    refusing to export the trajectory over it protects nothing — it just burns
    the session (litellm tracks `ui/litellm-dashboard/.env.production`, whose
    entire content is `NODE_ENV=production`). Untracked is the case that matters:
    a file that exists only on this machine is where local secrets live.
    Tracked ones are judged by content, with the same patterns as everything else.
    """
    where = "%s:%s" % (env, e["path"])
    if e.get("untracked"):
        hits.append({"kind": "dotenv_file", "where": where})
        return
    if e.get("type") != "file" or e.get("status") != "present":
        hits.append({"kind": "dotenv_file", "where": where,
                     "reason": "tracked .env not readable as a file"})
        return
    blob = _git_blob(session.get("repo"), e.get("content_hash"))
    if blob is None:
        hits.append({"kind": "dotenv_file", "where": where,
                     "reason": "tracked .env whose content could not be read"})
        return
    _scan_text(blob.decode(errors="ignore"), where, hits)


def _redaction_hits(session_dir, session, steps):
    hits = []
    _scan_text("\n".join(json.dumps(s) for s in steps), "trajectory", hits)
    for env in ("env_start", "env_end"):
        cap = os.path.join(session_dir, env)
        for diff in ("staged.diff", "unstaged.diff"):
            dp = os.path.join(cap, diff)
            if os.path.exists(dp):
                _scan_text(open(dp, "rb").read().decode(errors="ignore"),
                           "%s/%s" % (env, diff), hits)
        man = _load(os.path.join(cap, "manifest.json"))
        for e in (man or {}).get("entries", []):
            base = e["path"].rsplit("/", 1)[-1]
            if base.startswith(".env") and base not in _ENV_OK:
                _dotenv_hit(env, e, session, hits)
            if e.get("untracked") and e["status"] == "present" and e["type"] == "file":
                bp = _blob_path(session["cas_root"], e["content_hash"])
                if os.path.exists(bp):
                    _scan_text(open(bp, "rb").read().decode(errors="ignore"),
                               "%s:%s" % (env, e["path"]), hits)
    return hits


# --- env packaging ---------------------------------------------------------------

def _blob_path(cas_root, oid):
    return os.path.join(cas_root, oid[:2], oid[2:])


def _bundle(repo, shas, dest):
    """Bundle the session's base commits behind temp refs (bundles need refnames)."""
    refs = []
    try:
        for i, sha in enumerate(dict.fromkeys(shas)):
            ref = "refs/heads/agentcap-export-%d" % i
            g.out(repo, "update-ref", ref, sha)
            refs.append(ref)
        g.out(repo, "bundle", "create", dest, *refs)
    finally:
        for ref in refs:
            g.run(repo, "update-ref", "-d", ref)


TREE_SNAPSHOT = "repo.tar.gz"


def _tree_snapshot(repo, shas, dest):
    """Self-contained artifact for repos that cannot be bundled.

    `git bundle create` packs every object reachable from the refs, so a
    blob:none partial clone refetches its whole history from the promisor remote
    and fails on anything large. Replay only ever checks out ONE commit and
    applies diffs on top, so history is dead weight: ship the base trees instead.
    Still self-contained — every byte needed is in the tarball — but history-free,
    so the reconstruction is checked against base_tree_sha, not base_sha.
    """
    os.makedirs(dest, exist_ok=True)
    written = {}
    for sha in dict.fromkeys(shas):
        path = os.path.join(dest, "%s.tar.gz" % sha)
        with open(path, "wb") as f:
            p = subprocess.Popen(["git", "-C", repo, "archive", "--format=tar.gz", sha],
                                 stdout=f, stderr=subprocess.PIPE)
            _, err = p.communicate()
        if p.returncode != 0:
            raise RuntimeError("git archive %s failed: %s"
                               % (sha, err.decode(errors="ignore").strip()))
        written[sha] = os.path.basename(path)
    return written


def _pack_env(session_dir, session, dest):
    os.makedirs(os.path.join(dest, "blobs"), exist_ok=True)
    shas = [session["base_sha_start"], session["base_sha_end"]]
    repo, fell_back = sess.resolve_repo(session)
    try:
        _bundle(repo, shas, os.path.join(dest, "repo.bundle"))
        source = {"kind": "bundle", "path": "repo.bundle"}
    except Exception as e:
        # keep self-containment, drop history — see _tree_snapshot
        trees = _tree_snapshot(repo, shas, os.path.join(dest, "trees"))
        source = {"kind": "tree_snapshot", "trees": trees,
                  "bundle_error": str(e).strip().splitlines()[0] if str(e).strip() else ""}
    if fell_back:
        source["read_from_object_source"] = True
    _write(os.path.join(dest, "source.json"), source)
    for env in ("env_start", "env_end"):
        src, dst = os.path.join(session_dir, env), os.path.join(dest, env)
        os.makedirs(dst, exist_ok=True)
        for f in ("manifest.json", "staged.diff", "unstaged.diff", "runtime.json"):
            if os.path.exists(os.path.join(src, f)):
                shutil.copyfile(os.path.join(src, f), os.path.join(dst, f))
        man = _load(os.path.join(src, "manifest.json"))
        for e in (man or {}).get("entries", []):
            if e.get("untracked") and e["status"] == "present":
                bp = _blob_path(session["cas_root"], e["content_hash"])
                if os.path.exists(bp):
                    tp = _blob_path(os.path.join(dest, "blobs"), e["content_hash"])
                    os.makedirs(os.path.dirname(tp), exist_ok=True)
                    shutil.copyfile(bp, tp)


# --- record + task views ----------------------------------------------------------

def _task_view(session, seed, val, steps, runs, repo_name):
    tests = seed["candidate_fail_to_pass"] or seed["test_files"]
    key = hashlib.sha256(("%s|%s" % (repo_name, ",".join(sorted(tests))))
                         .encode()).hexdigest()[:16]
    statement = next((s["text"] for s in steps if s["type"] == "user_message"
                      and s.get("text", "").strip()), None)
    return {
        "task_version": TASK_VERSION,
        "task_key": key,               # sessions solving the same tests cluster here
        "repo": repo_name,
        "base_commit": session["base_sha_start"],
        "problem_statement": statement,
        "fail_to_pass": seed["candidate_fail_to_pass"],
        "pass_to_pass": seed["candidate_pass_to_pass"],
        "test_files": seed["test_files"],
        "test_commands": list(dict.fromkeys(r["cmd"] for r in runs)),
        "env": {"bundle": "env/repo.bundle", "start": "env/env_start",
                "end": "env/env_end", "blobs": "env/blobs"},
        "env_verified": val["env_verified"],
        "verified": False,   # earned by `agentcap replay`; False until red/green reproduces
        # DPO chosen side: the captured solving trajectory + its provenance grade
        "reference": {
            "trajectory": "trajectory.jsonl",
            "value_tier": val["value_tier"],
            "ended_green": val["signals"]["ended_green"],
            "test_iterations": val["signals"]["test_iterations"],
            "n_steps": len(steps),
        },
    }


def export_session(session_dir, out, min_tier="medium"):
    """-> (status, detail). status: exported | skipped | quarantined."""
    from . import taskseed as T
    from . import value as V

    session = _load(os.path.join(session_dir, "session.json"))
    if session["status"] != "open" and not os.path.exists(
            os.path.join(session_dir, "delta.json")):
        return "skipped", "no_delta"
    if session["status"] == "open":
        return "skipped", "open"
    join = _load(os.path.join(session_dir, "join.json"))
    if not join:
        return "skipped", "no_join"
    val = _load(os.path.join(session_dir, "value.json")) or V.assess(session_dir)
    if not val:
        return "skipped", "no_value"
    if _TIER[val["value_tier"]] < _TIER[min_tier]:
        return "skipped", "below_min_tier"

    traj = join["trajectory"]
    steps = normalize_trajectory(traj["log_path"], traj.get("agent"))
    hits = _redaction_hits(session_dir, session, steps)
    if hits:
        return "quarantined", hits

    sid = session["session_id"]
    repo_name = os.path.basename(session["repo"].rstrip("/")) or "repo"
    sdir = os.path.join(out, sid)
    os.makedirs(sdir, exist_ok=True)
    try:
        _pack_env(session_dir, session, os.path.join(sdir, "env"))
    except Exception as e:
        shutil.rmtree(sdir, ignore_errors=True)
        return "skipped", "env_pack_failed: %s" % e

    # sanitize: the absolute repo path must not leave the machine
    repo_abs = os.path.abspath(session["repo"])
    with open(os.path.join(sdir, "trajectory.jsonl"), "w") as f:
        for s in steps:
            f.write(json.dumps(s, sort_keys=True).replace(repo_abs, ".") + "\n")

    seed = _load(os.path.join(session_dir, "task_seed.json")) or T.extract_seed(session_dir)
    replay_rep = _load(os.path.join(session_dir, "replay.json"))
    task = None
    if seed:
        runs = tooltrace.test_runs(traj["log_path"], traj.get("agent"))
        task = _task_view(session, seed, val, steps, runs, repo_name)
        if replay_rep:
            task["verified"] = bool(replay_rep.get("verified"))
            task["replay_outcome"] = replay_rep.get("outcome")
            # a consumer that needs to rebuild the artifact elsewhere must be able
            # to tell an earned-but-machine-local verdict from a portable one
            task["replay_portability"] = replay_rep.get("portability")
        _write(os.path.join(sdir, "task.json"), task)

    verify_rep = _load(os.path.join(session_dir, "verify.json")) or {}
    record = {
        "record_version": RECORD_VERSION,
        "session_id": sid,
        "agent": session["agent"],
        "repo": repo_name,
        "created_at": session["created_at"],
        "closed_at": session["closed_at"],
        "start_confidence": session["start_confidence"],
        "end_confidence": session["end_confidence"],
        "reopens": session.get("reopens", 0),
        "base_sha_start": session["base_sha_start"],
        "base_sha_end": session["base_sha_end"],
        "join_confidence": join["join_confidence"],
        "join_signals": join["signals"],
        "value": val,
        "verify": {k: verify_rep.get(k) for k in
                   ("verified", "benchmark_eligible", "join_confidence")},
        "replay": ({"outcome": replay_rep.get("outcome"),
                    "verified": replay_rep.get("verified"),
                    "portability": replay_rep.get("portability"),
                    "artifact_source": replay_rep.get("artifact_source"),
                    "interpreter_source": replay_rep.get("interpreter_source")}
                   if replay_rep else None),
        "delta": _load(os.path.join(session_dir, "delta.json")),
        "files": {"trajectory": "trajectory.jsonl",
                  "task": "task.json" if task else None, "env": "env/"},
    }
    _write(os.path.join(sdir, "record.json"), record)
    return "exported", record


def export_all(root=None, out=None, min_tier="medium"):
    root = root or sess.DEFAULT_ROOT
    _, sessions_dir = sess._paths(root)
    os.makedirs(out, exist_ok=True)
    summary = {"exported": [], "skipped": {}, "quarantined": {}}
    for s in sess.list_sessions(root):
        sid = s["session_id"]
        try:
            status, detail = export_session(os.path.join(sessions_dir, sid), out,
                                            min_tier=min_tier)
        except Exception as e:  # one bad session must not kill the batch
            status, detail = "skipped", "error: %s" % e
        if status == "exported":
            summary["exported"].append(sid)
        elif status == "quarantined":
            summary["quarantined"][sid] = detail
        else:
            summary["skipped"][sid] = detail
    summary["exported"].sort()
    _write(os.path.join(out, "export_summary.json"), summary)
    return summary


def _jsonl(path):
    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _load(path):
    return json.load(open(path)) if os.path.exists(path) else None


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.write("\n")
