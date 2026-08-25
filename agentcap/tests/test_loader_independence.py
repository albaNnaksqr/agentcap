"""The export loader must stay independent of the producer.

tools/load_export.py is the only thing that checks an export from OUTSIDE:
`agentcap replay` reads the session store (~/.agentcap/sessions, ~/.agentcap/blobs)
and never the export directory, so without this loader "is the export
self-sufficient" is a question nobody asks. It earned its place by finding three
defects that passed every internal check (a guard listed in both fail_to_pass and
pass_to_pass, a conda lock that does not install, a bundle nobody can clone).

Its value depends entirely on NOT sharing the producer's assumptions. Importing
agentcap would let it reach for cas_root exactly as replay does and discover
nothing. This test pins that, plus the path guard, so a later refactor cannot
quietly undo either.

Run with:  python3 -m agentcap.tests.test_loader_independence
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOADER = os.path.join(HERE, "tools", "load_export.py")


def main():
    fails = []
    if not os.path.exists(LOADER):
        print("FAIL: tools/load_export.py is missing — the export has no outside check")
        sys.exit(1)

    tree = ast.parse(open(LOADER).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level:
            fails.append("relative import found: the loader must not couple to this package")

    if "agentcap" in imported:
        fails.append("the loader imports agentcap — it would inherit the assumptions "
                     "it exists to test")
    else:
        print("[ok] the loader imports nothing from agentcap")

    third_party = imported - set(sys.stdlib_module_names) - {"__future__"}
    if third_party:
        fails.append("non-stdlib imports would make the check depend on an install: %s"
                     % sorted(third_party))
    else:
        print("[ok] stdlib only — it runs anywhere the export lands")

    src = open(LOADER).read()
    for guard in ("/.agentcap", "/osmind-repos"):
        if guard not in src:
            fails.append("path guard lost its %r entry" % guard)
    if 'e["HOME"] = home' not in src and '"HOME"' not in src:
        fails.append("HOME is no longer redirected for subprocesses")
    if not any(f.startswith(("path guard", "HOME")) for f in fails):
        print("[ok] the path guard and the HOME redirect are both still in place")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
