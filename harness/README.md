# harness — Layer-1 capture harness

`run_batch.py` frames one coding session per queued issue so agentcap can capture and
later replay it:

```
for each queued issue:
    1. git worktree add at a clean base commit
    2. agentcap mark-start   (snapshot BEFORE any edit -> honest RED base)
    3. codex exec            (the measured agent; all intelligence lives here)
    4. agentcap mark-end     (snapshot AFTER -> delta)
```

Steps 2 and 4 are exogenous: **the measured agent never sets its own capture
boundary.** That ordering is the point — an agent that could choose when the
snapshot happens could choose what the delta says.

`contract_seedable_tdd.md` is prepended to every issue pack. It carries the
anti-gaming clauses (reproduce first, do not special-case the input, do not weaken
existing tests, stop rather than invent a passing test), so it is the standard a
captured session is later judged against. It lives here, versioned beside the code
that consumes it, and each run records its hash and commit in `run_meta.json` —
otherwise an old capture becomes unauditable as soon as the text moves on.

Run products (per-issue logs, composed prompts, worktrees) are NOT part of this
repo: logs default to `~/workspace/batch_b/runs/<ts>` and worktrees to `~/wt`.

```bash
python3 harness/run_batch.py --queue ~/workspace/osmind-packets/queue-YYYY-MM-DD.jsonl \
  --venv <prepared venv> --model <model> --reasoning high
# then
python3 -m agentcap join && seed && value && replay && export
```
