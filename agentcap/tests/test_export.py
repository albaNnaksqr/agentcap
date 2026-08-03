"""Export: canonical record + task-instance view (RL/DPO). A high-tier session
exports a self-contained dir — normalized trajectory, provenance-rich record.json,
task.json with problem statement + FAIL_TO_PASS + reference trajectory — whose env
reconstructs from the shipped bundle WITHOUT the original repo. Secrets quarantine,
low tiers and joinless sessions skip with reasons. Run with
    python3 -m agentcap.tests.test_export
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from agentcap import export as E
from agentcap import join as J
from agentcap import session as sess
from agentcap.verify import verify


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


def git(repo, *a):
    sh("git", "-C", repo, *a)


def write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def claude_log(path, cwd, events):
    """events: ('user', text) | ('bash', cmd, out) | ('edit', file_path)."""
    lines = [{"type": "system", "cwd": cwd, "timestamp": "2026-07-01T00:00:00.000Z"}]
    for i, ev in enumerate(events):
        ts = "2026-07-01T00:%02d:00.000Z" % (i + 1)
        if ev[0] == "user":
            lines.append({"type": "user", "timestamp": ts, "message": {
                "role": "user", "content": [{"type": "text", "text": ev[1]}]}})
        elif ev[0] == "bash":
            cid = "c%d" % i
            lines.append({"type": "assistant", "timestamp": ts, "message": {"content": [
                {"type": "tool_use", "id": cid, "name": "Bash",
                 "input": {"command": ev[1]}}]}})
            lines.append({"type": "user", "timestamp": ts, "message": {"content": [
                {"type": "tool_result", "tool_use_id": cid,
                 "content": [{"type": "text", "text": ev[2]}]}]}})
        else:
            lines.append({"type": "assistant", "timestamp": ts, "message": {"content": [
                {"type": "tool_use", "id": "e%d" % i, "name": "Edit",
                 "input": {"file_path": os.path.join(cwd, ev[1])}}]}})
    with open(path, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")


PROMPT = "fix the flaky parser so test_x passes"
RED = "tests/test_a.py::test_x FAILED\nE   AssertionError\n=== 1 failed in 0.1s ==="
GREEN = "tests/test_a.py::test_x PASSED\n=== 1 passed in 0.1s ==="


def build(tmp, name, files_at_start, end_edits, events, extra_untracked=None):
    repo = os.path.join(tmp, name)
    store = os.path.join(tmp, name + "_store")
    os.makedirs(repo)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    for rel, c in files_at_start.items():
        write(repo, rel, c)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    sid, _ = sess.start_session(repo, agent="claude", root=store, session_id="claude-S",
                                extra={"agent_session_id": "S", "log_path": "/x"})
    for rel, c in end_edits.items():
        write(repo, rel, c)
    for rel, c in (extra_untracked or {}).items():
        write(repo, rel, c)
    sess.end_session(session_id=sid, root=store)
    log = os.path.join(tmp, name + ".jsonl")
    claude_log(log, repo, events)
    J.set_join(sid, {"agent": "claude", "session_id": "S", "cwd": repo, "log_path": log,
                     "first_ts": 0, "last_ts": 0, "n_steps": len(events)},
               confidence="high", root=store)
    return repo, store, sid


def main():
    tmp = tempfile.mkdtemp(prefix="agentcap-export-")
    fails = []

    events = [("user", PROMPT),
              ("bash", "python -m pytest -q", RED),
              ("edit", "src/a.py"), ("edit", "src/b.py"),
              ("bash", "python -m pytest -q", GREEN)]
    files = {"src/a.py": "v=0\n", "src/b.py": "w=0\n",
             "tests/test_a.py": "def test_x():\n    assert 1\n"}
    edits = {"src/a.py": "v=1\n", "src/b.py": "w=1\n"}

    # --- HIGH session exports: record + trajectory + task + self-contained env ---
    # scratch.py is untracked at end: forces the CAS-blob packaging path
    repo, store, sid = build(tmp, "hi", files, edits, events,
                             extra_untracked={"scratch.py": "tmp = 1\n"})
    out = os.path.join(tmp, "out_hi")
    summary = E.export_all(root=store, out=out)
    if summary["exported"] != [sid]:
        fails.append("high session not exported: %s" % summary)
    sdir = os.path.join(out, sid)

    rec = json.load(open(os.path.join(sdir, "record.json")))
    if rec["value"]["value_tier"] != "high" or rec["join_confidence"] != "high":
        fails.append("record provenance wrong: %s / %s"
                     % (rec["value"]["value_tier"], rec["join_confidence"]))
    elif rec["repo"] != "hi" or "/" in rec["repo"]:
        fails.append("record leaks repo path: %r" % rec["repo"])
    else:
        print("[ok] record.json: provenance carried, repo path sanitized")

    steps = [json.loads(l) for l in open(os.path.join(sdir, "trajectory.jsonl"))]
    kinds = [s["type"] for s in steps]
    if steps[0]["type"] != "user_message" or steps[0]["text"] != PROMPT:
        fails.append("first step should be the user prompt: %s" % steps[0])
    elif "tool_call" not in kinds or "tool_result" not in kinds:
        fails.append("normalized trajectory missing tool events: %s" % kinds)
    elif repo in open(os.path.join(sdir, "trajectory.jsonl")).read():
        fails.append("trajectory leaks absolute repo path")
    else:
        print("[ok] trajectory normalized: prompt first, tool events, paths sanitized")

    task = json.load(open(os.path.join(sdir, "task.json")))
    if task["problem_statement"] != PROMPT:
        fails.append("problem_statement wrong: %r" % task["problem_statement"])
    elif task["fail_to_pass"] != ["tests/test_a.py::test_x"]:
        fails.append("fail_to_pass wrong: %s" % task["fail_to_pass"])
    elif "python -m pytest -q" not in task["test_commands"]:
        fails.append("test_commands wrong: %s" % task["test_commands"])
    elif len(task["task_key"]) != 16:
        fails.append("task_key malformed: %r" % task["task_key"])
    elif task["reference"]["value_tier"] != "high" or not task["reference"]["ended_green"]:
        fails.append("reference (DPO chosen side) wrong: %s" % task["reference"])
    else:
        print("[ok] task.json: statement + verifier + task_key + reference")

    # env must reconstruct from the shipped bundle with the ORIGINAL REPO GONE
    shutil.rmtree(repo)
    bundle = os.path.join(sdir, "env", "repo.bundle")
    blobs = os.path.join(sdir, "env", "blobs")
    ok_s, _ = verify(os.path.join(sdir, "env", "env_start"), bundle, cas_root=blobs)
    ok_e, _ = verify(os.path.join(sdir, "env", "env_end"), bundle, cas_root=blobs)
    if not (ok_s and ok_e):
        fails.append("exported env not self-contained: start=%s end=%s" % (ok_s, ok_e))
    else:
        print("[ok] env self-contained: verifies from bundle after repo deleted")

    # --- LOW tier is skipped with a reason ---
    _, store2, sid2 = build(tmp, "lo", {"README.md": "hi\n"}, {"README.md": "yo\n"},
                            [("user", "tweak readme"), ("edit", "README.md")])
    out2 = os.path.join(tmp, "out_lo")
    s2 = E.export_all(root=store2, out=out2)
    if s2["exported"] or s2["skipped"].get(sid2) != "below_min_tier":
        fails.append("low tier should skip below_min_tier: %s" % s2)
    else:
        print("[ok] low-tier session skipped with reason")

    # --- secrets quarantine: an untracked .env with an AWS key must not ship ---
    _, store3, sid3 = build(tmp, "sec", files, edits, events,
                            extra_untracked={".env": "AWS_KEY=AKIAIOSFODNN7EXAMPLE\n"})
    out3 = os.path.join(tmp, "out_sec")
    s3 = E.export_all(root=store3, out=out3)
    if s3["exported"] or sid3 not in s3["quarantined"]:
        fails.append("secret session not quarantined: %s" % s3)
    elif os.path.exists(os.path.join(out3, sid3)):
        fails.append("quarantined session left files in the export dir")
    else:
        print("[ok] secret-bearing session quarantined, nothing written")

    # --- tracked .env: judged by content, not by filename ---
    # benign one (litellm ships exactly this shape) must NOT burn the session
    files_env = dict(files)
    files_env["ui/.env.production"] = "NODE_ENV=production\n"
    _, store4, sid4 = build(tmp, "envok", files_env, edits, events)
    out4 = os.path.join(tmp, "out_envok")
    s4 = E.export_all(root=store4, out=out4)
    if sid4 in s4["quarantined"]:
        fails.append("benign tracked .env should not quarantine: %s"
                     % s4["quarantined"][sid4])
    elif sid4 not in s4["exported"]:
        fails.append("benign tracked .env session should export: %s" % s4)
    else:
        print("[ok] tracked .env with no secret exports normally")

    # a tracked .env that really holds a key must still be caught
    files_bad = dict(files)
    files_bad["ui/.env.production"] = "AWS_KEY=AKIAIOSFODNN7EXAMPLE\n"
    _, store5, sid5 = build(tmp, "envbad", files_bad, edits, events)
    out5 = os.path.join(tmp, "out_envbad")
    s5 = E.export_all(root=store5, out=out5)
    if sid5 not in s5["quarantined"]:
        fails.append("tracked .env holding a key must quarantine: %s" % s5)
    elif not any(h["kind"] == "aws_key" for h in s5["quarantined"][sid5]):
        fails.append("hit should name the secret, not the filename: %s"
                     % s5["quarantined"][sid5])
    else:
        print("[ok] tracked .env holding a real key still quarantined, by content")

    # unreadable tracked content (repo gone) must fail closed, not open
    repo6, store6, sid6 = build(tmp, "envgone", files_env, edits, events)
    shutil.rmtree(repo6)
    out6 = os.path.join(tmp, "out_envgone")
    s6 = E.export_all(root=store6, out=out6)
    if sid6 not in s6["quarantined"]:
        fails.append("unreadable tracked .env must fail closed: %s" % s6)
    elif not any("could not be read" in (h.get("reason") or "")
                 for h in s6["quarantined"][sid6]):
        fails.append("fail-closed reason not recorded: %s" % s6["quarantined"][sid6])
    else:
        print("[ok] unreadable tracked .env fails closed with a reason")

    # --- codex log shape normalizes too ---
    clog = os.path.join(tmp, "codex.jsonl")
    with open(clog, "w") as f:
        for d in [
            {"timestamp": "t0", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "do the thing"}]}},
            {"timestamp": "t1", "payload": {"type": "function_call", "name": "shell",
             "call_id": "c1", "arguments": json.dumps({"cmd": ["pytest", "-q"]})}},
            {"timestamp": "t2", "payload": {"type": "function_call_output",
             "call_id": "c1", "output": "1 passed"}},
        ]:
            f.write(json.dumps(d) + "\n")
    csteps = E.normalize_trajectory(clog, "codex")
    ckinds = [s["type"] for s in csteps]
    if ckinds != ["user_message", "tool_call", "tool_result"]:
        fails.append("codex normalization wrong: %s" % ckinds)
    elif csteps[0]["text"] != "do the thing":
        fails.append("codex user text wrong: %s" % csteps[0])
    else:
        print("[ok] codex log shape normalizes")

    # --- no join -> skipped with reason ---
    repo4 = os.path.join(tmp, "nj")
    store4 = os.path.join(tmp, "nj_store")
    os.makedirs(repo4)
    git(repo4, "init", "-q")
    git(repo4, "config", "user.email", "t@t")
    git(repo4, "config", "user.name", "t")
    write(repo4, "a.py", "A=1\n")
    git(repo4, "add", "-A")
    git(repo4, "commit", "-qm", "base")
    sid4, _ = sess.start_session(repo4, agent="claude", root=store4)
    write(repo4, "a.py", "A=2\n")
    sess.end_session(session_id=sid4, root=store4)
    s4 = E.export_all(root=store4, out=os.path.join(tmp, "out_nj"))
    if s4["exported"] or s4["skipped"].get(sid4) != "no_join":
        fails.append("joinless session should skip no_join: %s" % s4)
    else:
        print("[ok] joinless session skipped with reason")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
