"""Watcher test (step 4): reconciliation decisions + a full open->idle->close tick
cycle against fake Claude session dirs. Run with
    python3 -m agentcap.tests.test_watcher
"""
import json
import os
import subprocess
import sys
import tempfile
import time

from agentcap import watcher as W
from agentcap import session as sess
from agentcap.adapters import ClaudeAdapter


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


def make_repo(path):
    os.makedirs(path)
    sh("git", "-C", path, "init", "-q")
    sh("git", "-C", path, "config", "user.email", "t@t")
    sh("git", "-C", path, "config", "user.name", "t")
    with open(os.path.join(path, "f.py"), "w") as f:
        f.write("x=1\n")
    sh("git", "-C", path, "add", "-A")
    sh("git", "-C", path, "commit", "-qm", "base")


def fake_claude_log(projects_root, session_id, cwd, mtime=None):
    d = os.path.join(projects_root, "-" + cwd.strip("/").replace("/", "-"))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, session_id + ".jsonl")
    with open(p, "w") as f:
        f.write(json.dumps({"type": "system", "sessionId": session_id, "cwd": cwd}) + "\n")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def main():
    tmp = tempfile.mkdtemp(prefix="agentcap-watch-")
    repo = os.path.join(tmp, "repo")
    projects = os.path.join(tmp, "claude_projects")
    store = os.path.join(tmp, "store")
    make_repo(repo)
    os.makedirs(projects)
    fails = []
    now = time.time()

    # --- pure reconcile: git + fresh-enough -> START; non-git and too-fresh -> not ---
    observed = [
        {"agent": "claude", "session_id": "s1", "cwd": repo, "log_path": "/x",
         "mtime": now - 30, "is_git": True},
        {"agent": "claude", "session_id": "s2", "cwd": "/not/a/repo", "log_path": "/y",
         "mtime": now - 30, "is_git": False},
        {"agent": "claude", "session_id": "s3", "cwd": repo, "log_path": "/z",
         "mtime": now - 1, "is_git": True},   # within grace -> skip
    ]
    to_start, to_end = W.reconcile(observed, [], now)
    starts = {o["session_id"] for o in to_start}
    if starts != {"s1"}:
        fails.append("reconcile start set wrong: %s (want {s1})" % starts)
    else:
        print("[ok] reconcile: git+fresh starts, non-git & in-grace skipped")

    # --- full tick cycle: open, then idle -> close ---
    log = fake_claude_log(projects, "abc123", repo, mtime=now - 60)
    ad = [ClaudeAdapter(root=projects)]
    started, ended = W.tick(ad, root=store, now=now)
    if started != ["claude-abc123"]:
        fails.append("tick did not open expected session: %s" % started)
    opened = sess.list_sessions(store, status="open")
    if len(opened) != 1 or opened[0]["start_confidence"] != "best-effort":
        fails.append("watcher session should be single + best-effort: %s" % opened)
    else:
        print("[ok] tick opened a best-effort session (not benchmark-eligible)")

    # second tick, still fresh -> no change (idempotent)
    started2, ended2 = W.tick(ad, root=store, now=now + 10)
    if started2 or ended2:
        fails.append("tick not idempotent while active: +%s -%s" % (started2, ended2))
    else:
        print("[ok] tick idempotent while session active")

    # age the log past idle -> tick closes it
    os.utime(log, (now - 10000, now - 10000))
    started3, ended3 = W.tick(ad, root=store, now=now, idle_secs=600)
    if ended3 != ["claude-abc123"]:
        fails.append("idle session not closed: %s" % ended3)
    elif sess.list_sessions(store, status="open"):
        fails.append("session still open after idle close")
    else:
        print("[ok] idle log -> session closed, delta computed")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
