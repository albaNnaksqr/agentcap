# agentcap

Capture coding-agent sessions (Claude Code / Codex) at the source — the **trajectory**
plus a **verifiable git-environment snapshot** — and score each session's training value.

Local, deterministic, no model at runtime.

## What it captures, per session

```
{ initial git tree, trajectory (steps + tool outputs), final git tree, delta }
```

- **Env snapshots** at start & end — a per-file manifest of git-normalized blob hashes,
  reconstructable and **hash-verified** (reconstruct into a clean dir, compare every hash;
  tampering is detected, not assumed away).
- **Trajectory join** — pairs the agent's conversation/tool stream to the env capture with a
  *confidence* (session_id / cwd / time-overlap), not a boolean.
- **Runtime evidence** (`runtime.json` per snapshot) — platform, dependency-declaration
  file hashes, toolchain versions. Facts for a later rebuild, not a gate: agentcap
  records the runtime, it does not build one.

## Value score (3 deterministic axes)

Test runs are recognized across frameworks (pytest / unittest / go test / cargo test /
jest / vitest / mocha and npm-style runners), node-level where the framework prints it,
counts-only otherwise.

| axis | question | levels |
|---|---|---|
| **groundedness** | is the success trustworthy / reproducible? | grounded · weakly_grounded · untrusted · ungrounded |
| **process richness** | difficulty + failure→recovery signal (code files only) | rich · moderate · thin |
| **focus** | one coherent problem, not sprawl | focused · diffuse · sprawling |

`high = grounded + rich + focused + env verified`. The value score consumes the **verification
result**, not just the log: a session whose env snapshot fails to reconstruct is `untrusted`
(the "green" isn't reproducible), and a session you haven't verified yet caps at `medium` — so
"high" always means the environment provably rebuilds. All raw signals land in `value.json`; the
tier is a transparent proxy, not a benchmark grade. An agent-authored test that ends green counts
as a reproducibility anchor, not an independent oracle.

## CLI

```
watch → snapshot → verify → join → value

agentcap watch                    # background daemon (launchd template in agentcap/deploy/)
agentcap mark-start <repo>        # or pin a session manually
agentcap mark-end --repo <repo>
agentcap verify-session <id>      # reconstruct both snapshots + hash-check
agentcap join                     # pair trajectories (with confidence)
agentcap value                    # score every session -> value.json
agentcap seed                     # candidate red->green signals (neutral sub-signal)
```

## Scope

Captures git-normalized working-tree content + a dependency **declaration** (lockfile) — not a
runnable runtime. Not a sandbox builder or verifier factory. The watcher is a best-effort
collector, not a correctness boundary. No publish path yet.
