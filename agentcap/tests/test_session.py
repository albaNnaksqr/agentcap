"""Session-layer test (step 3): mark-start -> agent-like changes -> mark-end.
Proves the delta is correct and both env snapshots verify. Run with
    python3 -m agentcap.tests.test_session
"""
import os
import subprocess
import sys
import tempfile

from agentcap import session as sess


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


def git(repo, *a):
    sh("git", "-C", repo, *a)


def write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def main():
    tmp = tempfile.mkdtemp(prefix="agentcap-sess-")
    repo = os.path.join(tmp, "repo")
    root = os.path.join(tmp, "store")
    os.makedirs(repo)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    write(repo, "a.py", "A = 1\n")
    write(repo, "gone.py", "removed_later = True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")

    fails = []

    # --- mark-start (high confidence) ---
    sid, s = sess.start_session(repo, agent="manual", root=root)
    if s["start_confidence"] != "high":
        fails.append("start confidence not high")

    # --- agent-like work during the session ---
    write(repo, "a.py", "A = 2\n")                 # modified
    write(repo, "b.py", "B = new_file\n")          # added (untracked)
    os.remove(os.path.join(repo, "gone.py"))       # deleted
    # also a local *unpushed* commit, to exercise that path
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "agent work (unpushed)")

    # --- mark-end (found by repo, no explicit id) ---
    s2, delta = sess.end_session(repo=repo, root=root)

    if delta["modified"] != ["a.py"]:
        fails.append("modified wrong: %s" % delta["modified"])
    if delta["added"] != ["b.py"]:
        fails.append("added wrong: %s" % delta["added"])
    if delta["deleted"] != ["gone.py"]:
        fails.append("deleted wrong: %s" % delta["deleted"])
    if not fails:
        print("[ok] delta correct: modified=a.py added=b.py deleted=gone.py")

    # base_sha moved because of the in-session commit; start != end
    if s2["base_sha_start"] == s2["base_sha_end"]:
        fails.append("base_sha_end should differ after an in-session commit")
    else:
        print("[ok] in-session commit reflected: base_sha_start != base_sha_end")

    # --- both env snapshots verify; but NOT benchmark-eligible without a join ---
    rep = sess.verify_session(sid, root=root)
    if not rep["verified"]:
        fails.append("session did not verify: %s" % rep)
    elif rep["benchmark_eligible"]:
        fails.append("eligible with no trajectory join (join gate not applied)")
    else:
        print("[ok] both env snapshots verify; not eligible without a join")

    # add a high-confidence trajectory join -> now benchmark-eligible
    from agentcap import join as J
    J.set_join(sid, {"agent": "manual", "session_id": sid}, confidence="high", root=root)
    rep2 = sess.verify_session(sid, root=root)
    if not rep2["benchmark_eligible"]:
        fails.append("high/high + high join still not benchmark-eligible: %s" % rep2)
    else:
        print("[ok] high/high env + high join -> benchmark-eligible")

    # find_open_session should now return None (it was closed)
    if sess.find_open_session(repo, root=root) is not None:
        fails.append("closed session still reported open")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
