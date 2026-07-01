# agentcap

**A provenance-rich collector of coding-agent sessions — trajectory + git
environment, captured at the source — that scores each session's *training
value*. Not a sandbox builder, not a benchmark-task factory.**

Every developer running Claude Code / Codex generates real agent sessions daily,
and they evaporate. Trajectory-only tools (proxy logs, `dataclaw`) capture the
conversation but not the **environment** the agent acted on. agentcap captures
both, on the machine where the agent runs, and — crucially — is honest about how
valuable each captured session actually is.

## Positioning (how we got here)

We started aiming to mine **SWE-bench-style verifiable tasks** from sessions
(tests that go FAILED → PASSED). That framing was wrong for this data:

- **Agent-authored tests are normal, not a defect.** ~99% of the time Claude/Codex
  write code TDD-style: write a test, watch it fail, implement, watch it pass. So
  `FAILED → PASSED` is the *default rhythm*, not a quality signal — and the test,
  written by the same agent, is not an independent oracle.
- So we don't ask *"is this an independent verifier"* (it usually isn't). We ask
  **"how valuable is this trajectory as training data,"** and score it.

We do **not** build runnable sandboxes or verifiers — that's the hard, separate
job of a sandbox-builder / lab. agentcap hands them clean, provenance-rich raw
material with a value score, and (as a neutral sub-signal) candidate red→green
tests. It collects and grades; it does not manufacture independent verifiers.

## What it captures, per session

```
{ initial git tree, trajectory (steps + tool outputs), final git tree, delta }
```

- **Env snapshots** at start/end — a canonical per-file manifest of git-normalized
  blob hashes, reconstructable and **hash-verified** (`verify` reconstructs into a
  clean dir and compares every hash; tampering is detected, not assumed away).
- **Trajectory join** — pairs the agent's conversation/tool stream to the env
  capture with a *confidence* (session_id / cwd / time-overlap), never a boolean.

## The value score (3 deterministic axes — no model at runtime)

| axis | question | levels |
|---|---|---|
| **A. groundedness** | is the success trustworthy / reproducible? | grounded · weakly_grounded · ungrounded |
| **B. process richness** | how much learning signal (difficulty + failure→recovery)? | rich · moderate · thin |
| **C. focus / coherence** | was it one coherent problem, not sprawl? | focused · diffuse · sprawling |

- A self-authored test that ends green is a **reproducibility anchor** (grounded) —
  proof the session didn't just *claim* success — not an independent judge.
- Richness counts **code files only** (docs / brainstorm artifacts / pid-state
  excluded), so activity volume can't fake it.
- **`high` = grounded + rich + focused.** Diffuse work caps at `medium`; a
  multi-task mega-session is demoted to `low` (it isn't one unit).

The tier is a transparent proxy (all raw signals are exposed in `value.json`), not
a benchmark grade.

### Real distribution (177 real sessions on the author's machine)

`high 9 · medium 19 · low 82` (of 110 with code/test activity) —
`focused 100 · diffuse 7 · sprawling 3`. An honest gradient: a handful of genuinely
high-value trajectories, most low, over half with an outcome we can't even trust
(`ungrounded`). The `high` set was spot-checked by an external reviewer (Codex) and
the focus axis was added specifically to kill the sprawl false-positives it found.

## Pipeline / CLI

```
watch ──▶ snapshot ──▶ verify ──▶ join ──▶ value
```

```
agentcap watch                       # background daemon (launchd template in deploy/)
agentcap mark-start <repo>           # or pin a high-confidence session manually
agentcap mark-end   --repo <repo>
agentcap verify-session <id>         # reconstruct both env snapshots + hash-check
agentcap join                        # pair trajectories (with confidence)
agentcap value                       # score every session -> value.json
agentcap seed                        # neutral candidate red->green signals
```

## Scope / non-goals

- Captures git-normalized working-tree content + a dependency **declaration**
  (lockfile) — **not** a runnable runtime (OS/compiler/GPU/services out of scope).
- Not a sandbox builder, not an independent-verifier factory.
- The watcher is a best-effort collector, **not** a correctness boundary; only
  high-confidence, verified, high-join captures are benchmark-eligible.
- Publish path (secret-scan + HF) is intentionally not built yet.

See `../docs/CAPTURE_v0.2_spec.md` for the full design and trust contract.
