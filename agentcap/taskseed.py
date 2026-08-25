"""Mine a candidate verifiable-task seed from a joined session's trajectory, by
READING the agent's test-run outputs — nothing is executed. The seed is labeled
verified=false: it is observed validation evidence, not self-verification.

Method (see the method write-up):
  A. pull test-run events from the trajectory (tooltrace)
  B. parse each pytest output -> {node: status} + counts
  C. timeline-diff runs -> candidate FAIL_TO_PASS (red then green) / PASS_TO_PASS
  D. ground against the code delta + env_end manifest (test-only change = gaming risk)
  E. emit task_seed.json with a confidence grade
"""
import json
import os
import re
import subprocess

from . import session as sess
from . import testparse
from . import tooltrace
from .snapshot import load_manifest

# parsing lives in testparse now (framework-aware); kept as a name for callers/tests
parse_pytest = testparse.parse_pytest

# explicit pytest node targets in a command, e.g. `path/to/test_x.py::test_y`
# (also class::method and parametrized [id] forms).
_NODE_RE = re.compile(r"[\w./\\-]+\.py::[\w:.\[\]-]+")


def _cmd_nodes(cmd):
    return _NODE_RE.findall(cmd or "")


# PYTHONPATH=<val> in a test command; val may be quoted or bare (up to whitespace)
# The unquoted branch must swallow whole `$(...)` / backtick spans: they legally
# contain spaces (`PYTHONPATH=$(git rev-parse --show-toplevel)/python`), and a bare
# \S+ would truncate at the first one and yield a plausible-looking but nonexistent
# component -- worse than not matching, since it silently reintroduces the shadowing.
_PP_RE = re.compile(
    r'PYTHONPATH=("([^"]*)"|\'([^\']*)\'|((?:\$\([^)]*\)|`[^`]*`|\S)+))'
)
# `$(git rev-parse --show-toplevel)` and its backtick form, tolerant of whitespace.
# Only this one command is treated as a repo-root alias -- it is the idiom the
# prepared-runtime notes use; nothing else here executes or guesses at a command.
_REPO_ROOT_CMD_RE = re.compile(
    r'\$\(\s*git\s+rev-parse\s+--show-toplevel\s*\)|`\s*git\s+rev-parse\s+--show-toplevel\s*`'
)


def _pythonpath_components(cmd, cwd):
    """Repo-relative PYTHONPATH the agent used for a test run, so replay imports
    the reconstructed tree (not an installed copy). Agents commonly run e.g.
    `PYTHONPATH=$PWD/python pytest ...` for a src/ or python/ package layout.
    `$PWD`/absolute paths under the worktree are made relative; machine-specific
    absolute paths (site-packages, ...) are dropped.

    `$REPO_ROOT` counts as a repo-root alias too: prepared-runtime instructions
    commonly define `REPO_ROOT="$(git rev-parse --show-toplevel)"` and run
    `PYTHONPATH="$REPO_ROOT/python" ...`. Left unexpanded it survives into the seed
    as the literal string, replay then sets a PYTHONPATH that points nowhere, and
    an installed copy of the package silently shadows the reconstructed tree — the
    tests pass or fail against the WRONG code. Observed on sglang#33504/#33505,
    which replayed as not_green while the same tests were green in the worktree.

    The same applies when the agent inlines the substitution instead of binding it
    to a variable first -- `PYTHONPATH="$(git rev-parse --show-toplevel)/python"`.
    That is not a variable, so an alias table never catches it; it has to be matched
    as a command. Observed on sglang#33867."""
    m = _PP_RE.search(cmd or "")
    if not m:
        return []
    val = m.group(2) or m.group(3) or m.group(4) or ""
    cwd = (cwd or "").rstrip("/")
    out = []
    for raw in val.split(":"):
        c = raw.strip()
        # inline `$(git rev-parse --show-toplevel)` / backtick form -> repo root
        c = _REPO_ROOT_CMD_RE.sub(cwd, c)
        for alias in ("${PWD}", "$PWD", "${REPO_ROOT}", "$REPO_ROOT"):
            c = c.replace(alias, cwd)
        if not c:
            continue
        if os.path.isabs(c):
            if cwd and (c == cwd or c.startswith(cwd + "/")):
                c = os.path.relpath(c, cwd)
            else:
                continue  # outside the worktree -> not reconstructable
        if c.startswith("./"):
            c = c[2:] or "."
        if c and c not in out:
            out.append(c)
    return out


def _timeline(runs):
    """runs sorted by time. Return (candidate_ftp, candidate_ptp, evidence)."""
    parsed = []
    for r in sorted(runs, key=lambda r: (r.get("ts") or "", r["idx"])):
        p = testparse.parse(r["output"], r.get("framework"))
        # When the command explicitly targets a SINGLE node, attribute this run's
        # count-level pass/fail to that node id. Covers `pytest -q path::node`,
        # whose output carries counts + a bare-name FAILURES banner but no
        # `path::node FAILED` line for the parser to key a node on. Single target
        # only -> the counts are unambiguous.
        nodes = _cmd_nodes(r.get("cmd"))
        if len(nodes) == 1:
            n = nodes[0]
            c = p.get("counts", {})
            failed = c.get("failed", 0) or c.get("error", 0)
            if failed and n not in p["failed"]:
                p = {**p, "failed": p["failed"] | {n}}
            elif c.get("passed", 0) and not failed and n not in p["passed"]:
                p = {**p, "passed": p["passed"] | {n}}
        parsed.append((r, p))
    if not parsed:
        return set(), set(), []

    ever_failed = set()
    for _, p in parsed:
        ever_failed |= p["failed"]
    last = parsed[-1][1]
    first = parsed[0][1]

    # red -> green: failed at some point, not failed in the last run, and either
    # explicitly passed later or the last run has no failures at all.
    ftp = set()
    for n in ever_failed:
        if n in last["failed"]:
            continue
        passed_later = any(n in p["passed"] for _, p in parsed)
        if passed_later or not last["failed"]:
            ftp.add(n)

    # stayed green (thin signal): passed first and last, never failed.
    ptp = {n for n in first["passed"] if n in last["passed"] and n not in ever_failed}

    evidence = [{
        "idx": r["idx"], "ts": r.get("ts"), "cmd": r["cmd"], "counts": p["counts"],
    } for r, p in parsed]
    return ftp, ptp, evidence


# Horizontal whitespace only ([^\S\n], not \s). \s crosses newlines, so a lone `+`
# blank line -- which an agent leaves whenever it appends a test above an existing
# one -- let the match run past it into the following CONTEXT line and claim a
# pre-existing test as authored. That silently defeats the narrowing this whole
# function exists to provide. Seen on litellm#36197.
_ADDED_TEST_RE = re.compile(r"^\+[^\S\n]*(?:async[^\S\n]+)?def[^\S\n]+(test_\w+)", re.M)
# Same shape without the diff's leading '+', for reading a whole new file.
_TEST_DEF_RE = re.compile(r"^[^\S\n]*(?:async[^\S\n]+)?def[^\S\n]+(test_\w+)", re.M)


def _names_from_new_files(session_dir, session):
    """Test functions in files this session CREATED.

    `git diff` covers tracked files only, so a brand-new test file produces no
    hunk and the diff-based scan returns nothing — the narrowing then silently
    does not apply. Any session whose tests live in a new file was affected, e.g.
    sglang#35564 (authored_tests 0 while the codex captures had 3-4) whose whole
    test suite was one new file.

    The content IS captured: delta.added names the file and the end manifest
    carries its blob hash, so read it out of the CAS. This is not a fallback for
    a missing diff — new files and edited files are simply recorded in different
    places, and both have to be read."""
    names = set()
    delta = _load_json(os.path.join(session_dir, "delta.json")) or {}
    added = [p for p in (delta.get("added") or []) if _looks_test(p)]
    if not added:
        return names
    man = _load_json(os.path.join(session_dir, "env_end", "manifest.json")) or {}
    by_path = {e["path"]: e for e in man.get("entries", []) if e.get("path")}
    cas = (session or {}).get("cas_root")
    if not cas:
        return names
    for rel in added:
        ent = by_path.get(rel)
        if not ent or ent.get("status") != "present" or not ent.get("content_hash"):
            continue
        h = ent["content_hash"]
        blob = os.path.join(cas, h[:2], h[2:])
        try:
            text = open(blob, "rb").read().decode(errors="ignore")
        except OSError:
            continue
        names |= set(_TEST_DEF_RE.findall(text))
    return names


def _test_funcs_in(session_dir, rel, session=None):
    """Test functions defined in ONE file, read out of env_end via the CAS.

    `_names_from_new_files` answers "which names did this session author" across
    every added file at once. The collection-error promotion needs the per-file
    answer, because it builds `path::name` node ids and must not attach a name to
    a file that does not define it."""
    man = _load_json(os.path.join(session_dir, "env_end", "manifest.json")) or {}
    ent = next((e for e in man.get("entries", []) if e.get("path") == rel), None)
    if not ent or ent.get("status") != "present" or not ent.get("content_hash"):
        return set()
    sess_json = session or _load_json(os.path.join(session_dir, "session.json")) or {}
    cas = sess_json.get("cas_root")
    if not cas:
        return set()
    h = ent["content_hash"]
    try:
        text = open(os.path.join(cas, h[:2], h[2:]), "rb").read().decode(errors="ignore")
    except OSError:
        return set()
    return set(_TEST_DEF_RE.findall(text))


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _added_test_names(session_dir, session=None):
    """Test functions this session ADDED — from the end-state diffs for edits to
    existing files, and from the CAS for files it created.

    Observing a node go red then green is not enough to call it the task: a broad
    run can mark tests red that are simply never run again (they did not turn
    green, they vanished), and an environment error can red-then-green an entire
    file at once. Both were seen in one day — litellm#35793 dragged in three
    long-standing failures from a `pytest tests/litellm/test_*.py` sweep, and
    litellm#35531 swept in eight pre-existing tests whose file had errored at
    collection before prisma was installed.

    A test the session wrote is a much stronger statement of intent than a flip
    it merely witnessed. Returns bare function names (node ids are compared on
    their last segment, so this covers pytest, class-scoped and unittest ids).

    The stored diffs go EMPTY when the agent commits its own work — the worktree
    is then clean against its new HEAD. gpt-5.6-terra does this, gpt-5.6-sol does
    not, and that difference alone decided whether the narrowing applied
    (litellm#35796 fell back to eight observed flips and replayed not_green,
    while the same fix by a non-committing agent verified). So when the diffs
    yield nothing and the session moved HEAD, read the range instead. Capture must
    not depend on an agent's git habits.
    """
    names = set()
    for env in ("env_end", "env_start"):
        for diff in ("staged.diff", "unstaged.diff"):
            p = os.path.join(session_dir, env, diff)
            if not os.path.exists(p):
                continue
            try:
                text = open(p, "rb").read().decode(errors="ignore")
            except OSError:
                continue
            names |= set(_ADDED_TEST_RE.findall(text))
    # New files are recorded separately from edits; read both before deciding
    # there is nothing to narrow on.
    names |= _names_from_new_files(session_dir, session)
    if names or not session:
        return names
    start, end = session.get("base_sha_start"), session.get("base_sha_end")
    if not start or not end or start == end:
        return names
    try:
        repo, _ = sess.resolve_repo(session)
    except Exception:
        return names
    p = subprocess.run(["git", "-C", repo, "diff", "%s..%s" % (start, end)],
                       capture_output=True)
    if p.returncode == 0:
        names |= set(_ADDED_TEST_RE.findall(p.stdout.decode(errors="ignore")))
    return names


def _node_func(node):
    """Last segment of a node id, without a parametrization suffix."""
    seg = node.split("::")[-1].split(".")[-1]
    return seg.split("[", 1)[0]


def _node_path(node, end_paths):
    """The repo file a node id lives in, or None when it cannot be pinned.

    A pytest id carries the path outright. A unittest id is dotted
    (pkg.mod.Case.test_x) and its module part is relative to whatever root the
    run used — `unittest discover -s tests` yields `test_mod.Case.test_x` for
    `tests/test_mod.py` — so walk the dotted prefixes longest-first and match by
    path suffix. An ambiguous basename resolves to nothing rather than a guess.
    """
    if "::" in node:
        p = node.split("::", 1)[0]
        return p if p in end_paths else None
    parts = node.split(".")
    for i in range(len(parts), 0, -1):
        rel = "/".join(parts[:i]) + ".py"
        if rel in end_paths:
            return rel
        hits = [p for p in end_paths if p.endswith("/" + rel)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    return None


def _load_json(path):
    return json.load(open(path)) if os.path.exists(path) else None


def extract_seed(session_dir):
    session = _load_json(os.path.join(session_dir, "session.json"))
    join = _load_json(os.path.join(session_dir, "join.json"))
    delta = _load_json(os.path.join(session_dir, "delta.json"))
    if not (session and join and delta):
        return None  # need a joined session with a delta

    traj = join["trajectory"]
    runs = tooltrace.test_runs(traj["log_path"], traj.get("agent"))
    if not runs:
        return None
    ftp, ptp, evidence = _timeline(runs)

    # D. ground: test files must exist in env_end; classify the delta.
    end_paths = {e["path"] for e in load_manifest(os.path.join(session_dir, "env_end"))["entries"]}
    node_paths = {n: _node_path(n, end_paths) for n in (ftp | ptp)}
    ftp = {n for n in ftp if node_paths.get(n)}
    ptp = {n for n in ptp if node_paths.get(n)}

    # Prefer the tests this session wrote over every flip it merely witnessed —
    # see _added_test_names. Only narrows when the session added a test that is
    # actually among the observed flips; a session that fixed a pre-existing test
    # without writing one keeps the observed set, which is all it has.
    added_tests = _added_test_names(session_dir, session)
    authored = {n for n in ftp if _node_func(n) in added_tests}
    dropped_observed = sorted(ftp - authored) if authored else []
    if authored:
        ftp = authored

    # E. the collection-error case: an empty ftp that is a measurement artefact,
    # not an absence of red->green.
    #
    # `_timeline` can only see a flip when the RED run NAMES the tests that
    # failed. A run that dies at collection names none -- pytest reports
    # `error: N` for the file and no per-test ids. That is precisely what a
    # test-first session looks like when the module under test does not exist
    # yet: the first run is an ImportError at collection, so ftp is
    # structurally always empty and replay can only ever answer
    # `no_runnable_tests`. The whole "write a new module, then test it" shape was
    # therefore unverifiable by construction.
    #
    # Found on claude-litellm-38026: 6 authored tests, evidence idx6 `error: 2`
    # -> idx27 `passed: 6`, and candidate_fail_to_pass came out [].
    #
    # Promoted only under all of: nothing observed at name level, the session
    # AUTHORED the names, an earlier run carried failures or errors, the last run
    # is green, and every promoted name resolves to a file present in env_end.
    # The provenance is recorded rather than blended in, because this is a weaker
    # claim than a witnessed name-level flip and a consumer must be able to tell.
    ftp_source = "observed" if ftp else None
    if not ftp and added_tests and evidence:
        # The LAST run that says anything -- not the literal last one. A run whose
        # output carries no counts is no evidence, and both agents truncate long
        # tails often enough that reading evidence[-1] verbatim decides on silence.
        # value.ended_green already walks back like this; taskseed did not, and on
        # claude-litellm-38026 the final two runs were empty so the promotion below
        # never fired despite idx27 reporting `passed: 6`.
        idx_last = next((i for i in range(len(evidence) - 1, -1, -1)
                         if evidence[i]["counts"]), None)
        last_counts = evidence[idx_last]["counts"] if idx_last is not None else {}
        green_last = (last_counts.get("failed", 0) == 0
                      and last_counts.get("error", 0) == 0
                      and last_counts.get("passed", 0) > 0)
        red_before = any(e["counts"].get("failed", 0) or e["counts"].get("error", 0)
                         for e in evidence[:idx_last or 0])
        if green_last and red_before:
            new_test_files = [c for c in (set(delta["added"]) | set(delta["modified"]))
                              if _looks_test(c) and c in end_paths]
            promoted = {"%s::%s" % (f, n) for f in new_test_files for n in added_tests
                        if n in _test_funcs_in(session_dir, f)}
            if promoted:
                ftp = promoted
                node_paths.update({n: n.split("::", 1)[0] for n in promoted})
                ftp_source = "authored_after_collection_error"

    changed = sorted(set(delta["added"]) | set(delta["modified"]))
    test_files = sorted({node_paths[n] for n in (ftp | ptp)}
                        | {c for c in changed if _looks_test(c)})
    source_delta = [c for c in changed if not _looks_test(c)]
    test_only = bool(changed) and not source_delta

    # counts-only evidence: failures seen earlier, zero in the last run.
    # `error` counts as red alongside `failed`: a collection error is not a pass,
    # and for a test-first session it is the ONLY red there is. Looking at
    # `failed` alone made claude-litellm-38026 survive by accident -- its real red
    # was `error: 2`, and it only got a seed because unrelated mutation-testing
    # runs later happened to produce `failed: 18`.
    def _red(c):
        return c.get("failed", 0) or c.get("error", 0)
    _spoken = [e for e in evidence if e["counts"]]
    counts_only = bool(_spoken) and _red(_spoken[-1]["counts"]) == 0 \
        and any(_red(e["counts"]) for e in _spoken)
    if not ftp and not counts_only:
        return None

    # PYTHONPATH the agent actually used (prefer a run that targeted an ftp node,
    # else any pytest run). Lets replay import the reconstructed tree for src/ or
    # python/ package layouts instead of falling back to an installed copy.
    cwd = traj.get("cwd", "")
    test_pythonpath = []
    for r in runs:
        cmd = r.get("cmd") or ""
        if any(n in cmd for n in ftp):
            test_pythonpath = _pythonpath_components(cmd, cwd)
            break
    if not test_pythonpath:
        for r in runs:
            pp = _pythonpath_components(r.get("cmd") or "", cwd)
            if pp:
                test_pythonpath = pp
                break

    # NOTE: no strong/weak/counts quality tier here anymore. Judging trajectory
    # value (incl. that agent-authored tests are normal) lives in value.assess().
    # These are neutral candidate signals a sandbox-builder can use.
    seed = {
        "base_commit": session["base_sha_start"],
        "candidate_fail_to_pass": sorted(ftp),
        "candidate_pass_to_pass": sorted(ptp),
        "test_files": test_files,
        "test_pythonpath": test_pythonpath,  # repo-relative; [] -> replay uses "."
        "source_delta": source_delta,
        "test_only_delta": test_only,       # a flag, not a verdict
        # transparency for the narrowing above: what the session wrote, and which
        # merely-observed flips were set aside because of it
        "authored_tests": sorted(added_tests),
        # "observed" = a witnessed name-level red->green. Anything else is a
        # weaker claim and is named so a consumer can refuse it.
        "ftp_source": ftp_source,
        "dropped_observed_ftp": dropped_observed,
        "evidence": evidence,
        "verified": False,   # we did not run anything — observed evidence only
    }
    _write(os.path.join(session_dir, "task_seed.json"), seed)
    return seed


def _looks_test(path):
    b = os.path.basename(path)
    return b.startswith("test_") or b.endswith("_test.py") or "/tests/" in path or path.startswith("tests/")


def seed_all(root=sess.DEFAULT_ROOT):
    _, sessions_dir = sess._paths(root)
    results = {}
    for s in sess.list_sessions(root):
        try:
            results[s["session_id"]] = extract_seed(os.path.join(sessions_dir, s["session_id"]))
        except Exception as e:  # a bad log must not kill the batch
            results[s["session_id"]] = {"error": str(e)}
    return results


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.write("\n")
