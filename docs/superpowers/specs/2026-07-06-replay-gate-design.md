# Replay gate (`agentcap replay`) — design

Date: 2026-07-06
Status: approved (user-reviewed in conversation)

## Why

A manual replay experiment (2026-07-06, session
`codex-rollout-2026-07-02T10-48-38…`) proved the capture layer is faithful
(bundle + CAS blobs hash-verified exactly) but exported task instances are
**not verified**: `task.json.verified` is hard-coded `false`, and the one
instance tried did not replay green out of the box (tests read gitignored
fixtures). Three concrete defects surfaced:

1. No test-patch concept: fail_to_pass tests authored mid-session (TDD) don't
   exist at `base_commit`; one was renamed and doesn't exist at end either.
   A stale pytest node ID aborts the whole run ("no tests ran").
2. Seed's red/green timeline spans intra-session states, so some ftp tests
   are green at base.
3. End state may depend on files capture excludes by design (gitignored
   fixtures) — instance not self-contained.

The gate turns `verified` into an earned property and produces the headline
funnel metric: *% of exported task instances verified replayable*.

## What

New store-stage command, sibling of join/seed/value:

    agentcap replay [--root ROOT] [--session ID] [--timeout SECS]

For each closed session with `task_seed.json` (or the one named by
`--session`): rebuild the exportable artifact in a temp dir, check
fail_to_pass tests are green at end and red at start (with test-patch
overlay), write `replay.json` into the session store dir. Export then sets
`task.json.verified = (outcome == "red_green")` and copies the outcome into
`record.json`.

## Per-session flow (`replay.py::replay_session`)

1. **Bundle**: reuse `export._bundle()` to create a temp bundle from the
   session's repo carrying `base_sha_start` + `base_sha_end`. Replaying from
   the bundle (not the live repo) is what makes `verified` mean
   "self-contained replayable".
2. **Clone** bundle into temp workdir; ensure both SHAs are reachable
   (explicit `git fetch <bundle> <sha>` fallback — `git clone` from a bundle
   materializes only one head).
3. **GREEN check first** (fail-fast): reconstruct END state — checkout
   `base_sha_end`, apply `env_end` staged/unstaged diffs, materialize
   untracked files from CAS (reuse `verify.reconstruct()` mechanics).
   `--collect-only` to split ftp node IDs into collected vs missing, then run
   collected ones with pytest. All must pass.
4. **RED check**: reconstruct START state the same way, then **overlay the
   test patch**: for each `seed.test_files` path, copy the END-state version
   (from the end git tree or CAS blob) over the start tree. Collect again
   (records `exists_at_start` after overlay), run; all collected ftp tests
   must fail.
5. **Write `replay.json`** with graded outcome + per-test detail.

## Outcome semantics

| outcome        | meaning                                                    | verified |
|----------------|------------------------------------------------------------|----------|
| `red_green`    | end all-green AND start(+test patch) all-red               | **true** |
| `green_only`   | end green, but some ftp test already passes at start       | false    |
| `not_green`    | some ftp test fails at end (e.g. gitignored fixture dep)   | false    |
| `setup_failed` | bundle/clone/reconstruct/collect infrastructure failure    | false    |

`replay.json` records: `outcome`, `verified`, per-test rows
(`node_id`, `exists_at_end`, `exists_at_start_with_patch`, `end_status`,
`start_status`), pytest/python versions, durations, and an `error` string for
`setup_failed`.

## Execution environment

- Shared cached venv at `<store>/replay-venv` with pytest only; created on
  first use. No repo dependency installation, no docker — this is half a step
  into L2, deliberately short of L3 ("flight recorder, not flight simulator").
- Tests run with `PYTHONPATH=.` and cwd = reconstructed tree; per-pytest-run
  timeout (default 300 s) → exceeding it is `setup_failed` with reason.
- v1 supports tests runnable by pytest (includes unittest-style node IDs,
  which covers every seed produced so far). Other frameworks →
  `setup_failed` with `reason: unsupported_framework`.

## Changes by file

- `agentcap/replay.py` (new): flow above.
- `agentcap/cli.py`: `replay` subcommand → summary JSON
  (counts per outcome).
- `agentcap/export.py`: read `replay.json`; set `task.json.verified`,
  embed `replay` outcome in `record.json`. (~4 lines)
- No changes to taskseed/value/snapshot. Per-test existence flags live in
  `replay.json`, not the seed (YAGNI).
- Tests: follow existing script-test pattern — synthesize a small git repo
  with a red→green session history, drive seed→replay end-to-end, assert
  each of the four outcomes is reachable.

## Honest expectation

Current store (3 seeded sessions, all papyrus) will likely grade `not_green`
because their tests read gitignored `output/` fixtures. First verified-rate
reading may be 0% — that is the true number, and the quantified motivation
for the follow-up papyrus test-hygiene fix (move trace fixtures into a
tracked `tests/data/`).
