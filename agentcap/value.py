"""Trajectory value assessment — replaces the naive strong/weak/counts grade.

The reframe: agent-authored tests are NORMAL for Claude/Codex sessions, not a
defect. So we don't judge "is this an independent SWE-bench oracle." We judge the
VALUE of the trajectory as training data, on two deterministic axes:

  A. Groundedness (is the success trustworthy / reproducible?)
     A self-authored test that ends green is a REPRODUCIBILITY ANCHOR — not an
     independent judge, but proof the trajectory didn't just claim success. And it
     only counts if the ENVIRONMENT the test ran in verifiably reconstructs (from
     verify.json) — otherwise the "green" isn't reproducible by anyone.
       grounded        : ran tests, observed a failure, ended green
       weakly_grounded : ended green but never observed a failure
       untrusted       : log looks grounded but env snapshot failed to reconstruct
       ungrounded      : no test / ended red  -> outcome can't be trusted

  B. Process richness (how much learning signal — difficulty + failure->recovery)
       rich / moderate / thin, from files changed, edit churn, test iterations,
       red->green transitions, error variety.

  C. Focus/coherence — one problem, not sprawl (focused / diffuse / sprawling).

value_tier = groundedness_score (0-2) + process_score (0-2), bucketed, then gated:
`high` also requires focus==focused AND env_verified=="verified". All raw signals
are exposed — the tier is a transparent proxy, not a benchmark grade.
"""
import json
import os
import re

from . import testparse
from . import tooltrace
from . import taskseed
from .snapshot import load_manifest  # noqa: F401 (kept for parity / future grounding)

_ERR = re.compile(r'\b([A-Z][A-Za-z0-9]*Error)\b')

# richness/focus count CODE only — docs, brainstorm artifacts, pid/state, .env
# examples etc. are churn that inflates size without being problem-solving.
_CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb",
              ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".swift", ".kt",
              ".scala", ".sh", ".sql"}


def _is_code(p):
    if ".superpowers/" in p or "/brainstorm/" in p:
        return False
    return os.path.splitext(p)[1].lower() in _CODE_EXTS or taskseed._looks_test(p)


def _cluster(p):
    parts = p.split("/")
    return parts[0] if len(parts) > 1 else "."   # coarse: top-level dir


def _load(path):
    return json.load(open(path)) if os.path.exists(path) else None


def assess(session_dir):
    session = _load(os.path.join(session_dir, "session.json"))
    join = _load(os.path.join(session_dir, "join.json"))
    delta = _load(os.path.join(session_dir, "delta.json"))
    if not (session and join and delta):
        return None
    traj = join["trajectory"]
    log, agent = traj["log_path"], traj.get("agent")

    # env verification state (persisted by `verify-session`). This is the whole point
    # of a "verifiable" snapshot: a grounded claim only holds if the environment the
    # tests ran in actually reconstructs. Three states, kept distinct on purpose:
    #   verified   both snapshots reconstruct hash-for-hash and are consistent
    #   failed     reconstruction mismatch OR snapshot captured mid-mutation
    #   unverified verify-session hasn't been run yet (unknown, not a failure)
    env = _load(os.path.join(session_dir, "verify.json"))
    if env is None:
        env_state = "unverified"
    else:
        inconsistent = (env.get("start", {}).get("snapshot_inconsistent")
                        or env.get("end", {}).get("snapshot_inconsistent"))
        env_state = "verified" if (env.get("verified") and not inconsistent) else "failed"

    runs = tooltrace.test_runs(log, agent)
    edits = tooltrace.edit_events(log, agent)
    ftp, _ptp, _ev = taskseed._timeline(runs) if runs else (set(), set(), [])

    parsed = [testparse.parse(r["output"], r.get("framework")) for r in
              sorted(runs, key=lambda r: (r.get("ts") or "", r["idx"]))]
    errors = set()
    for r in runs:
        errors |= set(_ERR.findall(r["output"]))
    had_failure = any(p["failed"] or p["counts"].get("failed") or p["counts"].get("error")
                      for p in parsed)
    # `ended_green` reads the last run that actually SAYS something. A run whose
    # output carries no counts and no test names is not evidence of red — it is
    # no evidence at all, and codex truncates a long tail often enough that
    # scoring such a session `ungrounded` is a measurement artefact, not a
    # judgement. Seen on litellm#35428: replay proved red_green on 6 tests while
    # the final recorded run was `original_token_count: 11` worth of progress
    # dots. How far back we had to look is exposed rather than hidden, because
    # skipping runs is exactly the kind of leniency that should be auditable.
    ended_green = False
    ended_green_run = None
    for i in range(len(parsed) - 1, -1, -1):
        p = parsed[i]
        if not (p["counts"] or p["passed"] or p["failed"]):
            continue                      # silent run: no counts, no names
        ended_green = (p["counts"].get("failed", 0) == 0
                       and p["counts"].get("error", 0) == 0
                       and (p["counts"].get("passed", 0) > 0 or bool(p["passed"])))
        ended_green_run = i
        break

    # --- axis A: groundedness ---
    if not runs or not ended_green:
        grounded, gscore = ("ungrounded", 0)
    elif had_failure or ftp:
        grounded, gscore = ("grounded", 2)
    else:
        grounded, gscore = ("weakly_grounded", 1)

    # env verification gates the log-derived outcome: if the environment can't be
    # reconstructed, the "green" isn't reproducible, so the outcome is untrusted —
    # no matter how clean the log looks.
    if env_state == "failed" and gscore > 0:
        grounded, gscore = ("untrusted", 0)

    # --- axis B: process richness (CODE surface only) ---
    changed = set(delta["added"]) | set(delta["modified"]) | set(delta["deleted"])
    code_changed = sorted(c for c in changed if _is_code(c))
    code_surface = len(code_changed)
    clusters = sorted({_cluster(c) for c in code_changed})
    edit_count = len(edits)
    rechurn = edit_count - len(set(edits))          # re-editing the same files = backtracking
    test_iters = len(runs)
    transitions = len(ftp)
    recovery = (transitions >= 1) or (had_failure and ended_green) or rechurn >= 1

    if code_surface >= 2 and (recovery or test_iters >= 3):
        process, pscore = ("rich", 2)
    elif code_surface >= 1 and (recovery or test_iters >= 1 or edit_count >= 1):
        process, pscore = ("moderate", 1)
    else:
        process, pscore = ("thin", 0)

    # --- axis C: focus/coherence (Codex fix — stop rewarding sprawl) ---
    mega = code_surface > 25 or test_iters > 200 or edit_count > 200
    diffuse = mega or code_surface > 8 or len(clusters) > 3 \
        or test_iters > 60 or edit_count > 60
    focus = "sprawling" if mega else "diffuse" if diffuse else "focused"

    total = gscore + pscore
    if mega:
        tier = "low"                         # a multi-task mega-session isn't one unit
    elif total >= 3 and not diffuse and env_state == "verified":
        tier = "high"                        # grounded + rich + focused + env verifies
    elif total >= 2:
        tier = "medium"                      # incl. would-be-high w/ unverified/failed env
    else:
        tier = "low"

    val = {
        "value_tier": tier,
        "value_score": total,                # 0-4, = groundedness + process
        "groundedness": grounded,
        "process_richness": process,
        "focus": focus,
        "env_verified": env_state,           # verified | failed | unverified
        "signals": {
            "code_files_changed": code_surface,
            "code_clusters": clusters,
            "edit_events": edit_count,
            "file_rechurn": rechurn,
            "test_iterations": test_iters,
            "frameworks": sorted({r.get("framework") for r in runs if r.get("framework")}),
            "red_green_transitions": transitions,
            "error_types": sorted(errors),
            "observed_failure": had_failure,
            "ended_green": ended_green,
            # index (in ts order) of the run ended_green was read from, and how
            # many trailing runs were silent. silent_trailing_runs > 0 means the
            # verdict rests on an earlier run than the literal last one.
            "ended_green_run": ended_green_run,
            "silent_trailing_runs": (0 if ended_green_run is None
                                     else len(parsed) - 1 - ended_green_run),
        },
        "candidate_fail_to_pass": sorted(ftp),   # a sub-signal now, not the headline
        "note": "heuristic value proxy from deterministic trajectory+env signals; "
                "not a benchmark grade; agent-authored tests count as reproducibility "
                "anchors; high = grounded + rich + focused + env verified "
                "(sprawl demoted; unverified/failed env caps at medium)",
    }
    _write(os.path.join(session_dir, "value.json"), val)
    return val


def assess_all(root=None):
    from . import session as sess
    root = root or sess.DEFAULT_ROOT
    _, sessions_dir = sess._paths(root)
    out = {}
    for s in sess.list_sessions(root):
        try:
            out[s["session_id"]] = assess(os.path.join(sessions_dir, s["session_id"]))
        except Exception as e:  # a bad log must not kill the batch
            out[s["session_id"]] = {"error": str(e)}
    return out


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.write("\n")
