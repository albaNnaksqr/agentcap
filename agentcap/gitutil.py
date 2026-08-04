"""Thin git helpers. All content hashing goes through git so we get git-normalized
blob OIDs (the v0.2 fidelity contract: normalized blob content, not exact disk bytes)."""
import subprocess


def run(repo, *args, binary=False):
    """Run `git -C repo <args>`. Returns (rc, out, err). out is bytes if binary else str."""
    p = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=not binary,
    )
    return p.returncode, p.stdout, p.stderr


def out(repo, *args):
    rc, o, e = run(repo, *args)
    if rc != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), e.strip()))
    return o


def object_format(repo):
    """'sha1' or 'sha256' — Codex note: record the OID algorithm assumption."""
    rc, o, _ = run(repo, "rev-parse", "--show-object-format")
    return o.strip() if rc == 0 and o.strip() else "sha1"


def head_sha(repo):
    return out(repo, "rev-parse", "HEAD").strip()


def tree_sha(repo, rev="HEAD"):
    """Tree object of a commit: content identity, independent of history."""
    return out(repo, "rev-parse", "%s^{tree}" % rev).strip()


def current_branch(repo):
    rc, o, _ = run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return o.strip() if rc == 0 else ""


def hash_file(repo, relpath):
    """git-normalized blob OID of a worktree file (applies .gitattributes/clean/eol)."""
    return out(repo, "hash-object", "--", relpath).strip()


def hash_bytes(repo, data):
    """blob OID of raw bytes (used for symlink targets — git stores the target as a blob)."""
    p = subprocess.run(
        ["git", "-C", repo, "hash-object", "--stdin"],
        input=data, capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError("git hash-object --stdin failed: %s" % p.stderr.decode())
    return p.stdout.decode().strip()


def _zlist(repo, *args):
    """ls-files and friends, NUL-separated: -z is the only mode where git does NOT
    C-quote non-ASCII paths (core.quotepath), so 入库.md comes back as itself."""
    return [p for p in out(repo, *args, "-z").split("\0") if p]


def tracked_present(repo):
    """Tracked files that currently exist in the worktree (ls-files minus deleted)."""
    files = set(_zlist(repo, "ls-files"))
    deleted = set(_zlist(repo, "ls-files", "-d"))
    return sorted(files - deleted)


def deleted_tracked(repo):
    return sorted(_zlist(repo, "ls-files", "-d"))


def untracked(repo):
    """Untracked, respecting .gitignore. -z for path safety (spaces/newlines)."""
    return sorted(_zlist(repo, "ls-files", "--others", "--exclude-standard"))


def diff(repo, *extra):
    """Binary-safe diff bytes."""
    rc, o, e = run(repo, "diff", "--binary", "--full-index", *extra, binary=True)
    if rc not in (0, 1):  # git diff exits 1 when there are differences with some flags; 0 normally
        raise RuntimeError("git diff failed: %s" % e.decode())
    return o
