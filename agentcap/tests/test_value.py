"""Trajectory value test: the value score (groundedness x process richness x focus)
plus the env-verification gate.
Covers: a rich grounded+verified session = high; a trivial one-shot = low; an
ended-red session = ungrounded/low; a clean agent-authored TDD success = GROUNDED
(self-authored test = reproducibility anchor, not a defect); cross-cluster sprawl =
diffuse; a broken env snapshot = untrusted (demoted); an unverified env = capped at
medium (honest 'unknown', not high).
Run with:  python3 -m agentcap.tests.test_value
"""
import json
import os
import subprocess
import sys
import tempfile

from agentcap import session as sess
from agentcap import join as J
from agentcap import value as V


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
    """events: list of ('bash', cmd, out) or ('edit', file_path, None)."""
    lines = [{"type": "system", "cwd": cwd, "timestamp": "2026-07-01T00:00:00.000Z"}]
    for i, (kind, a, b) in enumerate(events):
        ts = "2026-07-01T00:%02d:00.000Z" % i
        if kind == "bash":
            cid = "c%d" % i
            lines.append({"type": "assistant", "timestamp": ts, "message": {"content": [
                {"type": "tool_use", "id": cid, "name": "Bash", "input": {"command": a}}]}})
            lines.append({"type": "user", "timestamp": ts, "message": {"content": [
                {"type": "tool_result", "tool_use_id": cid,
                 "content": [{"type": "text", "text": b}]}]}})
        else:
            lines.append({"type": "assistant", "timestamp": ts, "message": {"content": [
                {"type": "tool_use", "id": "e%d" % i, "name": "Edit",
                 "input": {"file_path": a}}]}})
    with open(path, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")


def build(tmp, name, files_at_start, end_edits, events):
    repo = os.path.join(tmp, name)
    store = os.path.join(tmp, name + "_store")
    os.makedirs(repo)
    git(repo, "init", "-q"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    for rel, c in files_at_start.items():
        write(repo, rel, c)
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")
    sid, _ = sess.start_session(repo, agent="claude", root=store, session_id="claude-S",
                                extra={"agent_session_id": "S", "log_path": "/x"})
    for rel, c in end_edits.items():
        write(repo, rel, c)
    sess.end_session(session_id=sid, root=store)
    log = os.path.join(tmp, name + ".jsonl")
    claude_log(log, repo, events)
    J.set_join(sid, {"agent": "claude", "session_id": "S", "cwd": repo, "log_path": log,
                     "first_ts": 0, "last_ts": 0, "n_steps": len(events)},
               confidence="high", root=store)
    # verify the env so `high` is reachable — a grounded claim only counts when the
    # environment reconstructs (writes verify.json that value.assess reads).
    sess.verify_session(sid, root=store)
    _, sdir = sess._paths(store)
    return os.path.join(sdir, sid)


def main():
    tmp = tempfile.mkdtemp(prefix="agentcap-value-")
    fails = []
    RED = "tests/test_a.py::test_x FAILED\nE   AssertionError\n=== 1 failed in 0.1s ==="
    GREEN = "tests/test_a.py::test_x PASSED\n=== 1 passed in 0.1s ==="

    # HIGH: agent-TDD but grounded (saw failure, ended green) + rich process
    # (2 source files changed, multiple test iterations, a red->green transition).
    hi = build(tmp, "hi",
               files_at_start={"src/a.py": "v=0\n", "src/b.py": "w=0\n",
                               "tests/test_a.py": "def test_x():\n    assert 1\n"},
               end_edits={"src/a.py": "v=1\n", "src/b.py": "w=1\n"},
               events=[("bash", "python -m pytest -q", RED),
                       ("edit", "src/a.py", None), ("edit", "src/b.py", None),
                       ("bash", "python -m pytest -q", GREEN)])
    v = V.assess(hi)
    if not v or v["value_tier"] != "high":
        fails.append("grounded+rich should be high: %s" % (v and v["value_tier"]))
    elif v["groundedness"] != "grounded":
        fails.append("TDD success (saw fail, ended green) should be grounded: %s" % v["groundedness"])
    else:
        print("[ok] grounded (agent-TDD success as anchor) + rich process -> high")

    # LOW: trivial one-shot, no tests run -> ungrounded + thin
    lo = build(tmp, "lo",
               files_at_start={"README.md": "hi\n"},
               end_edits={"README.md": "hi there\n"},
               events=[("edit", "README.md", None)])
    v2 = V.assess(lo)
    if not v2 or v2["value_tier"] != "low" or v2["groundedness"] != "ungrounded":
        fails.append("trivial no-test edit should be low/ungrounded: %s" % v2)
    else:
        print("[ok] trivial one-shot, no tests -> low / ungrounded")

    # UNGROUNDED: ended RED -> outcome can't be trusted, capped低
    rd = build(tmp, "red",
               files_at_start={"src/a.py": "v=0\n", "tests/test_a.py": "def test_x():\n    assert 0\n"},
               end_edits={"src/a.py": "v=9\n"},
               events=[("bash", "python -m pytest -q", RED),
                       ("edit", "src/a.py", None),
                       ("bash", "python -m pytest -q", RED)])
    v3 = V.assess(rd)
    if not v3 or v3["groundedness"] != "ungrounded":
        fails.append("ended-red should be ungrounded: %s" % v3)
    elif v3["value_tier"] == "high":
        fails.append("ended-red must not be high value")
    else:
        print("[ok] ended red -> ungrounded, not high")

    # DIFFUSE: grounded + rich but sprawled across many clusters -> capped, not high
    sp = build(tmp, "sprawl",
               files_at_start={"m1/x.py": "a=0\n", "m2/x.py": "b=0\n", "m3/x.py": "c=0\n",
                               "m4/x.py": "d=0\n", "docs/a.md": "x\n",
                               "tests/test_a.py": "def test_x():\n    assert 1\n"},
               end_edits={"m1/x.py": "a=1\n", "m2/x.py": "b=1\n", "m3/x.py": "c=1\n",
                          "m4/x.py": "d=1\n", "docs/a.md": "y\n"},
               events=[("bash", "python -m pytest -q", RED),
                       ("edit", "m1/x.py", None), ("edit", "m2/x.py", None),
                       ("edit", "m3/x.py", None), ("edit", "m4/x.py", None),
                       ("bash", "python -m pytest -q", GREEN)])
    v4 = V.assess(sp)
    if not v4 or v4["focus"] != "diffuse":
        fails.append("cross-cluster edits should be diffuse: %s" % (v4 and v4.get("focus")))
    elif v4["value_tier"] == "high":
        fails.append("diffuse session must not be high: %s" % v4["value_tier"])
    elif "docs/a.md" in "".join(v4["signals"]["code_clusters"]):
        fails.append("docs should be excluded from code clusters")
    else:
        print("[ok] cross-cluster sprawl -> diffuse, demoted from high; docs excluded")

    # ENV FAILED: same grounded+rich+focused trajectory, but the env snapshot doesn't
    # reconstruct -> the "green" isn't reproducible, so outcome is untrusted, not high.
    ef = build(tmp, "envfail",
               files_at_start={"src/a.py": "v=0\n", "src/b.py": "w=0\n",
                               "tests/test_a.py": "def test_x():\n    assert 1\n"},
               end_edits={"src/a.py": "v=1\n", "src/b.py": "w=1\n"},
               events=[("bash", "python -m pytest -q", RED),
                       ("edit", "src/a.py", None), ("edit", "src/b.py", None),
                       ("bash", "python -m pytest -q", GREEN)])
    vj = json.load(open(os.path.join(ef, "verify.json")))
    vj["verified"] = False                    # simulate a reconstruction mismatch
    with open(os.path.join(ef, "verify.json"), "w") as f:
        json.dump(vj, f)
    v5 = V.assess(ef)
    if not v5 or v5["env_verified"] != "failed":
        fails.append("broken env should be env_verified=failed: %s" % (v5 and v5.get("env_verified")))
    elif v5["groundedness"] != "untrusted":
        fails.append("failed-env outcome should be untrusted: %s" % v5["groundedness"])
    elif v5["value_tier"] == "high":
        fails.append("failed-env session must not be high: %s" % v5["value_tier"])
    else:
        print("[ok] env reconstruction failed -> untrusted, demoted from high")

    # ENV UNVERIFIED: verify never run -> honest 'unknown', capped at medium not high.
    uv = build(tmp, "unverified",
               files_at_start={"src/a.py": "v=0\n", "src/b.py": "w=0\n",
                               "tests/test_a.py": "def test_x():\n    assert 1\n"},
               end_edits={"src/a.py": "v=1\n", "src/b.py": "w=1\n"},
               events=[("bash", "python -m pytest -q", RED),
                       ("edit", "src/a.py", None), ("edit", "src/b.py", None),
                       ("bash", "python -m pytest -q", GREEN)])
    os.remove(os.path.join(uv, "verify.json"))   # pretend verify-session never ran
    v6 = V.assess(uv)
    if not v6 or v6["env_verified"] != "unverified":
        fails.append("no verify.json should be env_verified=unverified: %s" % (v6 and v6.get("env_verified")))
    elif v6["groundedness"] != "grounded":
        fails.append("unverified env keeps log-grounded label: %s" % v6["groundedness"])
    elif v6["value_tier"] != "medium":
        fails.append("unverified env should cap at medium, not high: %s" % v6["value_tier"])
    else:
        print("[ok] env unverified -> grounded label kept but capped at medium")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
