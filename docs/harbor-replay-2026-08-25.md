# Harbor as agentcap's execution plane — first result, 2026-08-25

## What was open

`runtime_portability: same_class` has been written into every record since
2026-08-18 on the strength of one falsification experiment, and that experiment
ran on the machine that produced the captures, driven by agentcap's own replay
code. Two things were therefore unproven: that the export is sufficient without
agentcap in the loop, and that the runtime rebuilds anywhere but here.

## What was run

`tools/harbor_replay.py` turns an export instance into a Harbor 0.21 (schema
1.4) task package. The environment is `environment/Dockerfile`, which installs
`runtime_lock.txt` into `python:3.12-slim` at build time and unpacks the START
tree with the reference tests removed. The captured source patch goes to
`solution/solve.sh`. `tests/test.sh` overlays the reference tests and runs
`fail_to_pass`.

Both ends of the control come from that one package, using Harbor's own agents:

    harbor run -p <pkg> -a oracle   # applies the patch
    harbor run -p <pkg> -a nop      # applies nothing

## Result

14 of the 73 instances in `export-20260824` are packageable. The other 59 are
refused with a reason: 40 `not verified`, 10 not task instances, 9 without a
`runtime_lock.txt` (conda and distro-python captures — `machine_local`, refused
by design rather than shipped as something that cannot start).

All 14 packageable instances:

    oracle -> reward 1     nop -> reward 0     protected_test_integrity 1
    reproduced: 14/14

Per-instance rewards: `harbor-replay-2026-08-25.tsv`.

`protected_test_integrity 1` everywhere is its own finding: it says the source
patch and the test patch are cleanly separable in all 14, which is what
taskseed's `test_only_delta` assumes and had never been enforced.

## What this does and does not establish

Established: the export is sufficient. Harbor rebuilt the runtime from the lock,
ran the tests, and scored them with no agentcap code executing at verification
time and no path to the capturing host's `.venv-litellm`. Red and green were
both re-derived, so the tests discriminate rather than merely pass.

Not established: a physically different machine. The container ran on the
capturing host and shares its kernel and its aarch64 arch. `same_class` remains
a claim about hardware class; what moved is that everything ABOVE the kernel is
now known to come out of the export. The remaining step is one `docker run` of
these same packages on a second Spark.

Population caveat: all 14 are litellm, because litellm is the only captured repo
using a venv. The lock mechanism has not been exercised on a second project.
