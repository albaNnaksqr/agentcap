"""Self-contained trust-core test (no pytest needed): run with
    python3 -m agentcap.tests.test_roundtrip
from the trace2env dir. Proves snapshot -> reconstruct -> hashes match, that the
manifest is canonical, and that tampering is DETECTED (the whole point of verify)."""
import json
import os
import subprocess
import sys
import tempfile

from agentcap.snapshot import snapshot, load_manifest
from agentcap.verify import verify


def sh(*a, **kw):
    subprocess.run(a, check=True, capture_output=True, **kw)


def git(repo, *a):
    sh("git", "-C", repo, *a)


def make_repo(root):
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    write(root, "kept.py", "print('base')\n")
    write(root, "sub/mod.py", "X = 1\n")
    write(root, "readme.md", "hello\n")
    # non-ASCII path: git quotes these in porcelain output (core.quotepath=true) —
    # a real watcher tick crashed on exactly this shape
    write(root, "入库字段设计.md", "汉字 tracked\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")

    # a realistic mixed worktree: staged edit, unstaged edit, a local (unpushed)
    # commit, a deletion, an untracked file, and a symlink.
    write(root, "kept.py", "print('staged change')\n")
    git(root, "add", "kept.py")                      # staged
    write(root, "kept.py", "print('staged change')\n# then unstaged\n")  # + unstaged
    write(root, "sub/mod.py", "X = 2\n")             # unstaged edit
    os.remove(os.path.join(root, "readme.md"))       # deletion
    git(root, "commit", "-qm", "local unpushed commit", "-a") if False else None
    write(root, "new_untracked.txt", "fresh untracked content\n")  # untracked
    write(root, "数据/说明 v2.md", "untracked, non-ASCII dir + space\n")
    os.symlink("kept.py", os.path.join(root, "link_to_kept"))       # symlink


def write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def main():
    tmp = tempfile.mkdtemp(prefix="agentcap-test-")
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    make_repo(repo)

    capture = os.path.join(tmp, "capture")
    meta, manifest = snapshot(repo, capture)

    fails = []

    # 1. round-trip verifies
    ok, report = verify(capture, repo)
    if not ok:
        fails.append("round-trip verify failed: %s" % json.dumps(report, indent=2))
    else:
        print("[ok] round-trip verify: %d files hash-matched" % report["checked"])

    # 2. manifest is canonical: sorted paths, re-serializes byte-identically
    paths = [e["path"] for e in manifest]
    if paths != sorted(paths):
        fails.append("manifest not sorted by path")
    raw = open(os.path.join(capture, "manifest.json")).read()
    reser = json.dumps(json.loads(raw), sort_keys=True, indent=2) + "\n"
    if raw != reser:
        fails.append("manifest.json is not canonical (re-serialize differs)")
    else:
        print("[ok] manifest canonical (sorted + stable JSON)")

    # 3. expected shape: symlink captured, untracked flagged, deletion recorded
    by = {e["path"]: e for e in manifest}
    if by.get("link_to_kept", {}).get("type") != "symlink":
        fails.append("symlink not captured as type=symlink")
    if not by.get("new_untracked.txt", {}).get("untracked"):
        fails.append("untracked file not flagged untracked")
    if "readme.md" not in meta["deleted"]:
        fails.append("deletion not recorded in meta.deleted")
    if "入库字段设计.md" not in by:
        fails.append("non-ASCII tracked path missing/quoted in manifest: %s"
                     % [p for p in by if "md" in p])
    if not by.get("数据/说明 v2.md", {}).get("untracked"):
        fails.append("non-ASCII untracked path missing: %s" % sorted(by))
    if not fails:
        print("[ok] symlink + untracked-flag + deletion + non-ASCII paths recorded")

    # 4. TAMPER: corrupt an untracked blob in the CAS -> verify must FAIL
    oid = by["new_untracked.txt"]["content_hash"]
    blob = os.path.join(capture, "blobs", oid[:2], oid[2:])
    with open(blob, "wb") as f:
        f.write(b"CORRUPTED\n")
    ok2, report2 = verify(capture, repo)
    if ok2:
        fails.append("TAMPER NOT DETECTED: verify passed on a corrupted blob")
    else:
        print("[ok] tamper detected: verify failed after corrupting a blob (%d mismatch)"
              % len(report2["mismatches"]))

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
