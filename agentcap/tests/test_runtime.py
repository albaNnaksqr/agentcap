"""Runtime-evidence test (L1): snapshot writes a runtime.json sidecar — OS/arch,
dependency-declaration file hashes, toolchain versions. Evidence only: it must not
gate verify/value. Run with
    python3 -m agentcap.tests.test_runtime
"""
import json
import os
import subprocess
import sys
import tempfile

from agentcap import gitutil as g
from agentcap import runtime
from agentcap import session as sess
from agentcap.snapshot import snapshot


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
    tmp = tempfile.mkdtemp(prefix="agentcap-rt-")
    repo = os.path.join(tmp, "repo")
    root = os.path.join(tmp, "store")
    os.makedirs(repo)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    write(repo, "a.py", "A = 1\n")
    write(repo, "requirements.txt", "requests==2.31.0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    write(repo, "uv.lock", "version = 1\n")   # untracked dep declaration

    fails = []

    # --- collect(): dep files found with git-normalized hashes ---
    rt = runtime.collect(repo)
    deps = {d["path"]: d for d in rt["dep_files"]}
    if set(deps) != {"requirements.txt", "uv.lock"}:
        fails.append("dep_files wrong: %s" % sorted(deps))
    elif deps["requirements.txt"]["content_hash"] != g.hash_file(repo, "requirements.txt"):
        fails.append("dep file hash not git-normalized blob oid")
    else:
        print("[ok] dep_files: tracked + untracked declarations, git blob oids")

    # --- collect(): toolchain versions (git always; python via requirements.txt) ---
    tools = rt["tools"]
    if not tools.get("git", "").strip():
        fails.append("git version missing: %s" % tools)
    if "python3" not in tools:
        fails.append("python3 not probed despite requirements.txt: %s" % sorted(tools))
    if not fails:
        print("[ok] tools probed: %s" % sorted(tools))

    # --- collect(): platform facts present ---
    if not (rt.get("os") and rt.get("arch")):
        fails.append("platform facts missing: os=%r arch=%r" % (rt.get("os"), rt.get("arch")))
    if rt.get("runtime_version") != 1:
        fails.append("runtime_version missing/wrong: %r" % rt.get("runtime_version"))
    if not fails:
        print("[ok] platform facts: os=%s arch=%s" % (rt["os"], rt["arch"]))

    # --- snapshot() writes runtime.json beside manifest.json ---
    cap = os.path.join(tmp, "cap")
    snapshot(repo, cap)
    rj = os.path.join(cap, "runtime.json")
    if not os.path.exists(rj):
        fails.append("snapshot did not write runtime.json")
    else:
        on_disk = json.load(open(rj))
        if {d["path"] for d in on_disk["dep_files"]} != {"requirements.txt", "uv.lock"}:
            fails.append("runtime.json dep_files wrong: %s" % on_disk["dep_files"])
        else:
            print("[ok] snapshot writes runtime.json sidecar")

    # --- session end-to-end: both captures carry runtime.json; dep drift visible;
    #     evidence only — verify/value gating unchanged ---
    sid, _ = sess.start_session(repo, agent="manual", root=root)
    write(repo, "uv.lock", "version = 2\n")    # agent bumps deps mid-session
    sess.end_session(repo=repo, root=root)
    sdir = os.path.join(root, "sessions", sid)
    rt_s = json.load(open(os.path.join(sdir, "env_start", "runtime.json")))
    rt_e = json.load(open(os.path.join(sdir, "env_end", "runtime.json")))
    hs = {d["path"]: d["content_hash"] for d in rt_s["dep_files"]}
    he = {d["path"]: d["content_hash"] for d in rt_e["dep_files"]}
    if hs["uv.lock"] == he["uv.lock"]:
        fails.append("dep drift not captured: uv.lock hash unchanged start->end")
    else:
        print("[ok] dep-declaration drift visible across start/end")

    rep = sess.verify_session(sid, root=root)
    if not rep["verified"]:
        fails.append("runtime sidecar broke verification: %s" % rep)
    else:
        print("[ok] verification unaffected by runtime.json (evidence, not a gate)")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
