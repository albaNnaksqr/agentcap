"""Which user message becomes the RL task's problem_statement.

Both harnesses open a session with a preamble delivered AS a user message
(plugin recommendations, <environment_context> carrying cwd/shell/date/timezone,
system reminders). Taking the first user_message therefore picked the preamble
and never the task: 45 of 48 instances in export-20260813 had a problem_statement
made entirely of it. Because problem_statement IS the prompt of the RL/DPO
product, those instances were unusable as tasks while looking complete, and the
statement also carried absolute host paths.

Run with:  python3 -m agentcap.tests.test_export_statement
"""
import sys

from agentcap.export import _problem_statement

ENV = ('<environment_context>\n  <cwd>/home/u/wt/repo</cwd>\n  <shell>bash</shell>\n'
       '  <current_date>2026-08-13</current_date>\n  <timezone>Asia/Hong_Kong</timezone>\n'
       '</environment_context>')
PLUGINS = '<recommended_plugins>\nHere is a list of plugins that are available...\n</recommended_plugins>'
TASK = '# Task contract — reproduce-then-fix\n\nYou are fixing one GitHub issue.'


def um(text):
    return {"type": "user_message", "text": text}


def main():
    fails = []
    cases = [
        # (label, steps, expected statement, expected 1-based step)
        ("codex: plugins+env preamble, then contract+pack",
         [um(PLUGINS + "\n" + ENV), um(TASK), um(TASK)], TASK, 2),
        ("claude: env-only preamble, then the ask",
         [um(ENV), um(TASK)], TASK, 2),
        # a real ask in the first message must still be taken as-is, at step 1
        ("no preamble at all",
         [um(TASK)], TASK, 1),
        # returned verbatim: a trailing system-reminder is part of what the agent
        # saw, so it is not stripped from the value -- only ignored when deciding
        ("wrapper present but real content too",
         [um(ENV), um(TASK + "\n<system-reminder>be brief</system-reminder>")],
         TASK + "\n<system-reminder>be brief</system-reminder>", 2),
        # no task anywhere -> None, never a preamble as a consolation prize
        ("preamble only", [um(ENV), um(PLUGINS)], None, None),
        ("whitespace-only ask", [um(ENV), um("   \n\t ")], None, None),
        ("no user messages", [{"type": "assistant_message", "text": "hi"}], None, None),
    ]
    for label, steps, want, want_step in cases:
        got, step = _problem_statement(steps)
        if got != want or step != want_step:
            fails.append("%s -> statement=%r step=%s (wanted step=%s)"
                         % (label, (got or "")[:60], step, want_step))
        else:
            print("[ok] %s (step %s)" % (label, step))

    # the regression this guards: the preamble must never win over a later task
    got, _ = _problem_statement([um(PLUGINS + "\n" + ENV), um(TASK)])
    if got is not None and ("<recommended_plugins>" in got or "<environment_context>" in got):
        fails.append("harness preamble selected as the problem statement")
    else:
        print("[ok] harness preamble never wins over a later real task")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
