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
        env/runtime_lock.txt pin list for the interpreter that earned `verified`
                             (absent when there was nothing to promise)

The task instance is the RL/DPO product: problem statement (the first user message
that is not harness preamble), FAIL_TO_PASS/PASS_TO_PASS + observed test commands
as the verifier spec, and the captured trajectory embedded as `reference` — the
chosen side of a future DPO pair. `task_key` clusters sessions that solve the same
tests in the same project, and is None when the seed has no tests; it is keyed on
the bare repo name so a fork and its upstream cluster together, while `repo` keeps
the precise `owner/name` for provenance. The exit is gated: secret
hits quarantine the whole session (trajectory, diffs, untracked blobs, .env names
are scanned; bundle HISTORY is not — exporting a repo means you own its history).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from . import gitutil as g
from . import session as sess
from . import tooltrace

_TIER = {"low": 0, "medium": 1, "high": 2}

# v2: `repo` is `owner/name` from the origin remote, not the worktree directory
#     name; task_key is keyed on the bare repo name (so forks cluster) and is
#     None when the seed has no tests.
# v3: pass_to_pass comes from replay's sweep instead of the session log, where it
#     was empty by construction, so the field's meaning changed for consumers.
#     Adds pass_to_pass_source / regression_scope / regression_reason /
#     regressions to the task, and the matching counts to the record.
# v4: `portability` split into artifact_portability + runtime_portability. The old
#     single field described the git tree while reading like a claim about the
#     whole instance, and 17 of 20 verified instances only ran because of one
#     machine's venv. env/runtime_lock.txt now ships the pin list for the
#     interpreter that earned the verdict.
# v5: fail_to_pass no longer contains guards (they were in BOTH lists, which no
#     consumer can honour); guards_moved_from_ftp records the move. A conda
#     runtime is machine_local -- a pip freeze of it does not install. Bundles are
#     clone-verified before being shipped.
RECORD_VERSION = 5
TASK_VERSION = 6

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
    """codex rollout -> steps. Two log shapes coexist.

    <0.144 uses `message` / `function_call` / `function_call_output`. >=0.144 splits
    messages into `user_message` / `agent_message` and runs tools through
    `custom_tool_call` / `custom_tool_call_output`. Recognising only the older set
    silently drops the entire action sequence: sglang#33504 exported 12 steps with
    ZERO tool calls while its rollout held 13 custom_tool_call pairs, which made the
    `reference` trajectory — the chosen side of a future DPO pair — a transcript of
    talk with no work in it. tooltrace was taught the new shape in July; this
    normalizer was not.

    `reasoning` items are counted, never emitted: their `summary` is empty and the
    content is in `encrypted_content`, readable only by the provider. The count is
    what lets a consumer tell "this agent did not reason" from "its reasoning was
    never legible to us" — see reasoning_stats().
    """
    steps = []
    for d in _jsonl(path):
        ts = d.get("timestamp")
        p = d.get("payload") if isinstance(d.get("payload"), dict) else d
        pt = p.get("type")
        if pt == "user_message":
            steps.append({"type": "user_message", "ts": ts,
                          "text": p.get("message") or ""})
        elif pt == "agent_message":
            steps.append({"type": "assistant_message", "ts": ts,
                          "text": p.get("message") or ""})
        elif pt == "custom_tool_call":
            steps.append({"type": "tool_call", "ts": ts,
                          "name": p.get("name"), "input": p.get("input")})
        elif pt == "custom_tool_call_output":
            out = p.get("output")
            steps.append({"type": "tool_result", "ts": ts,
                          "output": out if isinstance(out, str) else _text_blocks(out)})
        elif pt == "message":
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


def reasoning_stats(log_path, agent):
    """-> {"items": n, "readable": bool} or None when the agent emits no reasoning
    items at all. A provider that hides chain-of-thought still logs the item with
    an empty `summary` and an `encrypted_content` blob only it can open, so the
    absence of reasoning text in a trajectory is a property of the provider, not
    of the agent's behaviour. Recording the count keeps that distinction in the
    exported record instead of leaving a consumer to guess."""
    if agent != "codex":
        return None
    items = readable = 0
    for d in _jsonl(log_path):
        p = d.get("payload") if isinstance(d.get("payload"), dict) else d
        if p.get("type") != "reasoning":
            continue
        items += 1
        if _text_blocks(p.get("summary")).strip():
            readable += 1
    if not items:
        return None
    return {"items": items, "readable_summaries": readable,
            "readable": readable > 0}


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


def _dotenv_hit(env, e, repo, hits):
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
    blob = _git_blob(repo, e.get("content_hash"))
    if blob is None:
        hits.append({"kind": "dotenv_file", "where": where,
                     "reason": "tracked .env whose content could not be read"})
        return
    _scan_text(blob.decode(errors="ignore"), where, hits)


def _redaction_hits(session_dir, session, steps):
    hits = []
    # objects may live in the parent repo when the capture worktree is gone —
    # reading through the recorded source is what keeps a tracked .env readable
    # instead of failing closed on every one of them
    try:
        repo, _ = sess.resolve_repo(session)
    except Exception:
        repo = session["repo"]
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
                _dotenv_hit(env, e, repo, hits)
            if e.get("untracked") and e["status"] == "present" and e["type"] == "file":
                bp = _blob_path(session["cas_root"], e["content_hash"])
                if os.path.exists(bp):
                    _scan_text(open(bp, "rb").read().decode(errors="ignore"),
                               "%s:%s" % (env, e["path"]), hits)
    return hits


# --- env packaging ---------------------------------------------------------------

def _blob_path(cas_root, oid):
    return os.path.join(cas_root, oid[:2], oid[2:])


def _assert_bundle_usable(bundle, shas):
    """Raise unless a consumer can actually clone this bundle and reach the bases.

    `git bundle create` can succeed and still write a bundle nobody can use: the
    sglang-omni#1360 export shipped one for eleven days that dies on
    `error: Could not read 0f8718...` / `Failed to traverse parents`, while its
    record claimed artifact_portability self_contained. The producer's own success
    is not evidence — only the consumer's operation is, so run that operation
    here. --no-checkout keeps it cheap: object transfer is what fails, not the
    worktree write."""
    tmp = tempfile.mkdtemp(prefix="agentcap-bundlecheck-")
    try:
        p = subprocess.run(["git", "clone", "--quiet", "--no-checkout", bundle,
                            os.path.join(tmp, "c")],
                           capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError("bundle is not cloneable: %s"
                               % (p.stderr or p.stdout).strip().splitlines()[0][:200])
        for sha in {s for s in shas if s}:
            q = subprocess.run(["git", "-C", os.path.join(tmp, "c"), "cat-file", "-e",
                                "%s^{tree}" % sha], capture_output=True)
            if q.returncode != 0:
                raise RuntimeError("bundle clones but lacks the tree of %s" % sha[:12])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
        _assert_bundle_usable(os.path.join(dest, "repo.bundle"), shas)
        source = {"kind": "bundle", "path": "repo.bundle"}
    except Exception as e:
        # keep self-containment, drop history — see _tree_snapshot
        trees = _tree_snapshot(repo, shas, os.path.join(dest, "trees"))
        # remove the unusable bundle so nothing downstream can pick it up
        stale = os.path.join(dest, "repo.bundle")
        if os.path.exists(stale):
            os.remove(stale)
        source = {"kind": "tree_snapshot", "trees": trees,
                  "bundle_error": str(e).strip().splitlines()[0] if str(e).strip() else ""}
    if fell_back:
        source["read_from_object_source"] = True
    _write(os.path.join(dest, "source.json"), source)

    # The pin list for the interpreter that earned the verdict. Tiny next to the
    # trees beside it (1.4 KB against 49 MB for litellm) and it is the difference
    # between a tree an RL consumer can rebuild and one it can rebuild but not
    # run. Absent when replay had nothing to promise — see runtime_portability.
    lock = os.path.join(session_dir, "runtime_lock.txt")
    if os.path.exists(lock):
        shutil.copyfile(lock, os.path.join(dest, "runtime_lock.txt"))
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

# Both harnesses open a session with a preamble delivered AS a user message —
# plugin recommendations, `<environment_context>` (cwd, shell, date, timezone,
# permission profile), system reminders. Taking the first user_message therefore
# picked the preamble, never the task: 45 of 48 instances in export-20260813 had
# a problem_statement made entirely of it, and the RL/DPO product's prompt is
# exactly this field, so those instances were unusable as tasks while looking
# complete. It also dragged absolute host paths into the statement.
# The list is enumerated from the whole exported population rather than grown one
# session at a time: after the tag wrappers were handled, all 19 statements still
# contaminated were the injected project-instructions block, in exactly two
# spellings ("# AGENTS.md instructions for <path>" x15, "# AGENTS.md
# instructions" x4). `[^\n]*` not `.*` for the heading — re.S is on for the tag
# bodies and would otherwise swallow the rest of the message.
_HARNESS_WRAPPER = re.compile(
    r"<(recommended_plugins|environment_context|system[-_]reminder|INSTRUCTIONS)\b[^>]*>.*?</\1>"
    r"|^\#+[ \t]*(?:AGENTS|CLAUDE)\.md instructions[^\n]*",
    re.S | re.M)


def _problem_statement(steps):
    """The first user message that carries an actual task, and its 1-based index.

    A candidate is judged on what is left after the known harness wrappers are
    removed, but the ORIGINAL text is returned: that is what the agent was given,
    and rewriting it would trade a wrong statement for a doctored one. The index
    is reported so "we skipped some messages" is visible in the artifact rather
    than implied."""
    n = 0
    for s in steps:
        if s["type"] != "user_message":
            continue
        n += 1
        text = s.get("text") or ""
        if _HARNESS_WRAPPER.sub("", text).strip():
            return text, n
    return None, None


def _repo_fields(session):
    """(repo, repo_source, cluster_name) for the record and the task key.

    `repo` is the precise `owner/name` so provenance survives; the KEY uses the
    bare name so the same project captured through a fork and through upstream
    lands in one cluster (this store has both spellings of sglang-omni). When no
    identity was recorded — a pre-`repo_identity` session whose worktree is gone —
    the old directory name is kept and repo_source says so, rather than guessing
    an owner."""
    ident = session.get("repo_identity")
    dirname = os.path.basename((session.get("repo") or "").rstrip("/")) or "repo"
    if ident:
        return ident, "git_remote", ident.split("/")[-1]
    return dirname, "worktree_dirname", dirname


def _apply_regression_spec(task, replay_rep):
    """Take the regression half of the verifier spec from replay, not from the seed.

    taskseed derives pass_to_pass from named PASSED node ids in the session log,
    and agents run `pytest -q`, which prints dots and no names — so the seed's set
    was empty for all 50 seeds in the store, and every exported instance shipped a
    verifier spec with no regression guard. replay runs the sweep itself against
    the reconstructed trees, so its set is the real one.

    `pass_to_pass_source` and `regression_reason` are what keep an empty guard
    honest: 5 of the 18 verified sessions collect no tests at all (a repo with no
    pytest, or a scope that does not collect), and an unexplained empty list reads
    as a clean bill of health rather than as "nothing was measured"."""
    scope = replay_rep.get("regression_scope")
    reason = replay_rep.get("regression_reason")
    ptp = replay_rep.get("pass_to_pass")
    if scope and not reason and ptp:
        task["pass_to_pass"] = ptp
        task["pass_to_pass_source"] = "replay_sweep"
    else:
        # keep whatever the seed had (in practice []), and say why
        task["pass_to_pass_source"] = "session_log"

    # A guard cannot be in BOTH lists. taskseed misclassifies guards as
    # fail_to_pass (they do flip red->green once during development), replay
    # detects them and folds them into pass_to_pass, and until now the SHIPPED
    # spec kept them in fail_to_pass as well. Any consumer applying the standard
    # rule "every FAIL_TO_PASS must fail before the patch" then rejects the
    # instance or computes a wrong reward. Fixing the grade was not fixing the
    # artifact -- found 2026-08-19 by a loader that reads only the export.
    guards = [g for g in (replay_rep.get("guards_in_ftp") or [])]
    if guards:
        gset = set(guards)
        kept = [n for n in (task.get("fail_to_pass") or []) if n not in gset]
        # never empty the verifier spec: if every candidate is a guard the session
        # is green_only and not verified anyway, but do not corrupt it here.
        if kept:
            task["fail_to_pass"] = kept
        # guards belong in pass_to_pass whatever the sweep managed to do, so the
        # witness replay proved directly is never lost to a failed sweep
        task["pass_to_pass"] = sorted(set(task.get("pass_to_pass") or []) | gset)
        task["guards_moved_from_ftp"] = guards
    task["regression_scope"] = scope
    task["regression_reason"] = reason
    # non-empty means `verified` is already False; shipped so a consumer can see
    # WHY without going back to the record
    task["regressions"] = replay_rep.get("regressions") or []


def _task_view(session, seed, val, steps, runs, repo, repo_source, cluster_name):
    tests = seed["candidate_fail_to_pass"] or seed["test_files"]
    # No tests means nothing to cluster ON. A key built from an empty test list
    # asserts "these sessions solve the same tests" about sessions that have no
    # tests at all -- it put 16 unrelated papyrus sessions in one bucket.
    key = (hashlib.sha256(("%s|%s" % (cluster_name, ",".join(sorted(tests))))
                          .encode()).hexdigest()[:16] if tests else None)
    statement, stmt_step = _problem_statement(steps)
    return {
        "task_version": TASK_VERSION,
        # sessions solving the same tests in the same project cluster here; None
        # when the seed carries no tests, since there is nothing to cluster on
        "task_key": key,
        "repo": repo,
        "repo_source": repo_source,    # git_remote | worktree_dirname
        "base_commit": session["base_sha_start"],
        "problem_statement": statement,
        # which user message it came from (1-based); >1 means harness preamble
        # messages were skipped to find it
        "problem_statement_step": stmt_step,
        "fail_to_pass": seed["candidate_fail_to_pass"],
        # How those nodes were arrived at. "observed" is a witnessed name-level
        # red->green. "authored_after_collection_error" is weaker: the RED run
        # died at collection and named no tests, so the nodes are the ones the
        # session AUTHORED in a file that went error -> green. Shipped because a
        # consumer that treats the two alike cannot tell a witnessed flip from an
        # inferred one, and the inference is the part that could be wrong.
        "fail_to_pass_source": seed.get("ftp_source"),
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
    if not join.get("trajectory"):
        # Since 2073fd3 a refused join is RECORDED rather than left absent, so the
        # file exists and is truthy while carrying no trajectory. Carry the reason
        # out: "unjoined: cwd_disjoint" tells a reader the capture had candidates
        # and every one of them ran somewhere unrelated, which a bare "no_join"
        # would flatten into "we never looked".
        return "skipped", "unjoined: %s" % (join.get("unjoined_reason") or "no_reason")
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
    repo, repo_source, cluster_name = _repo_fields(session)
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
        task = _task_view(session, seed, val, steps, runs, repo, repo_source, cluster_name)
        if replay_rep:
            task["verified"] = bool(replay_rep.get("verified"))
            task["replay_outcome"] = replay_rep.get("outcome")
            # a consumer that needs to rebuild the artifact elsewhere must be able
            # to tell an earned-but-machine-local verdict from a portable one
            task["replay_artifact_portability"] = replay_rep.get("artifact_portability")
            # the half that decides whether an RL consumer can run this at all
            task["replay_runtime_portability"] = replay_rep.get("runtime_portability")
            _apply_regression_spec(task, replay_rep)
        _write(os.path.join(sdir, "task.json"), task)

    verify_rep = _load(os.path.join(session_dir, "verify.json")) or {}
    record = {
        "record_version": RECORD_VERSION,
        "session_id": sid,
        "agent": session["agent"],
        "repo": repo,
        "repo_source": repo_source,
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
                    "artifact_portability": replay_rep.get("artifact_portability"),
                    "runtime_portability": replay_rep.get("runtime_portability"),
                    "runtime_lock": replay_rep.get("runtime_lock"),
                    "artifact_source": replay_rep.get("artifact_source"),
                    "interpreter_source": replay_rep.get("interpreter_source"),
                    # counts here, full lists in task.json: the record is the
                    # summary doc, the task is the spec. regression_reason is
                    # what stops n_pass_to_pass == 0 from reading as "clean".
                    "regression_scope": replay_rep.get("regression_scope"),
                    "regression_reason": replay_rep.get("regression_reason"),
                    "n_pass_to_pass": len(replay_rep.get("pass_to_pass") or []),
                    "n_regressions": len(replay_rep.get("regressions") or []),
                    "n_improved": len(replay_rep.get("improved") or [])}
                   if replay_rep else None),
        # reasoning items exist but are provider-encrypted: this says "not legible",
        # not "not present", so a consumer knows the trajectory is action-level by
        # necessity rather than by omission
        "reasoning": reasoning_stats(traj["log_path"], traj.get("agent")),
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
