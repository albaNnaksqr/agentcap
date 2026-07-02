"""Snapshot a git worktree into a capture dir: a canonical per-file manifest + the
diffs needed to reconstruct tracked files + untracked blobs in the CAS.

Fidelity contract (v0.2): we record git-normalized blob content hashes, NOT exact disk
bytes. `verify` reconstructs and recomputes these same hashes.

Manifest canonicalization (Codex step-1 note): entries sorted by path; JSON with
sorted keys + trailing newline; explicit hash_algo + manifest_version. Stable so the
manifest itself is content-addressable and diffable.
"""
import json
import os
import stat

from . import gitutil as g
from . import runtime
from .cas import CAS

MANIFEST_VERSION = 1


def _fingerprint(repo):
    """Cheap index+worktree fingerprint to bracket the capture and detect mid-capture
    mutation (v0.2 snapshot_inconsistent)."""
    status = g.out(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    head = g.head_sha(repo)
    return head + "\0" + status


def _entry(repo, cas, relpath):
    """Build one manifest entry for a present worktree path, storing its blob in CAS."""
    abspath = os.path.join(repo, relpath)
    st = os.lstat(abspath)
    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(abspath)
        oid = g.hash_bytes(repo, target.encode())
        cas.put_bytes(target.encode(), oid)
        return {
            "path": relpath, "type": "symlink", "mode": "120000",
            "exec_bit": False, "symlink_target": target,
            "content_hash": oid, "size": len(target.encode()), "status": "present",
        }
    oid = g.hash_file(repo, relpath)
    exec_bit = bool(st.st_mode & 0o111)
    stored = cas.put_file(abspath, oid)
    return {
        "path": relpath, "type": "file",
        "mode": "100755" if exec_bit else "100644",
        "exec_bit": exec_bit, "symlink_target": None,
        "content_hash": oid, "size": st.st_size,
        "status": "present" if stored else "skipped_size",
    }


def snapshot(repo, capture_dir, cas_root=None):
    repo = os.path.abspath(repo)
    os.makedirs(capture_dir, exist_ok=True)
    cas = CAS(cas_root or os.path.join(capture_dir, "blobs"))

    fp_pre = _fingerprint(repo)

    base_sha = g.head_sha(repo)
    staged = g.diff(repo, "--cached")           # HEAD -> index
    unstaged = g.diff(repo)                       # index -> worktree
    with open(os.path.join(capture_dir, "staged.diff"), "wb") as f:
        f.write(staged)
    with open(os.path.join(capture_dir, "unstaged.diff"), "wb") as f:
        f.write(unstaged)

    present = g.tracked_present(repo) + g.untracked(repo)
    untracked_set = set(g.untracked(repo))
    manifest = [_entry(repo, cas, p) for p in sorted(set(present))]
    for e in manifest:
        e["untracked"] = e["path"] in untracked_set  # untracked entries reconstruct from CAS

    fp_post = _fingerprint(repo)
    inconsistent = fp_pre != fp_post

    meta = {
        "manifest_version": MANIFEST_VERSION,
        "hash_algo": "git-blob-" + g.object_format(repo),
        "base_sha": base_sha,
        "branch": g.current_branch(repo),
        "deleted": g.deleted_tracked(repo),        # derived metadata only, not a reconstruction op
        "snapshot_inconsistent": inconsistent,
        # a capture with skipped/quarantined entries is non-verifying -> non-benchmark
        "has_nonverifying_entries": any(e["status"] != "present" for e in manifest),
    }

    _write_json(os.path.join(capture_dir, "manifest.json"),
                {"meta": meta, "entries": manifest})

    # runtime evidence sidecar (L1): best-effort, never gates verify/value and never
    # fails the capture — the watcher must stay a collector, not a correctness boundary
    try:
        rt = runtime.collect(repo)
    except Exception as e:
        rt = {"runtime_version": runtime.RUNTIME_VERSION, "error": str(e)}
    _write_json(os.path.join(capture_dir, "runtime.json"), rt)
    return meta, manifest


def _write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.write("\n")


def load_manifest(capture_dir):
    with open(os.path.join(capture_dir, "manifest.json")) as f:
        return json.load(f)
