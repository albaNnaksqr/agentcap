"""Framework-aware test-run recognition and output parsing. One shape out for every
framework: {"failed": set, "passed": set, "counts": {passed/failed/error/skipped}} —
node-level sets where the framework prints them (pytest / go / cargo), counts-only
where it doesn't (js / unittest). No recognized framework in a command means it is
not a test run; an unrecognized framework must yield empty evidence, never a guess.
"""
import re

# command -> framework. Order matters: "python -m pytest" must not fall through to
# a generic matcher. \b keeps pytest_cache / mytest.sh from counting as invocations.
_DETECT = [
    ("pytest", re.compile(r'\bpytest\b|\bpy\.test\b')),
    ("unittest", re.compile(r'\bunittest\b')),
    ("go", re.compile(r'\bgo\s+test\b')),
    ("cargo", re.compile(r'\bcargo\s+(?:test|nextest)\b')),
    ("js", re.compile(r'\bjest\b|\bvitest\b|\bmocha\b'
                      r'|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b')),
]


def detect_framework(cmd):
    if not cmd:
        return None
    for name, rx in _DETECT:
        if rx.search(cmd):
            return name
    return None


# --- pytest (node-level; moved from taskseed, behavior unchanged) ---
_PY_FAILED = re.compile(r'^(?:FAILED|ERROR)\s+(\S+::\S+)', re.M)
_PY_FAILED_V = re.compile(r'^(\S+::\S+)\s+(?:FAILED|ERROR)\b', re.M)
_PY_PASSED_V = re.compile(r'^(\S+::\S+)\s+PASSED\b', re.M)
_PY_PASSED_V2 = re.compile(r'^PASSED\s+(\S+::\S+)', re.M)
_PY_COUNTS = re.compile(r'(\d+)\s+(passed|failed|error|errors|skipped)')


def parse_pytest(output):
    failed = set(_PY_FAILED.findall(output)) | set(_PY_FAILED_V.findall(output))
    passed = set(_PY_PASSED_V.findall(output)) | set(_PY_PASSED_V2.findall(output))
    passed -= failed
    counts = {}
    for n, kind in _PY_COUNTS.findall(output):
        counts[kind.rstrip("s")] = counts.get(kind.rstrip("s"), 0) + int(n)
    return {"failed": failed, "passed": passed, "counts": counts}


# --- unittest (counts-only: Ran N + OK / FAILED (failures=x, errors=y)) ---
_U_RAN = re.compile(r'^Ran (\d+) tests?', re.M)
_U_FAILED = re.compile(r'^FAILED \(([^)]*)\)', re.M)


def parse_unittest(output):
    m = _U_RAN.search(output)
    ran = int(m.group(1)) if m else 0
    failed = errors = 0
    fm = _U_FAILED.search(output)
    if fm:
        for part in fm.group(1).split(","):
            k, _, v = part.strip().partition("=")
            if k == "failures" and v.isdigit():
                failed = int(v)
            elif k == "errors" and v.isdigit():
                errors = int(v)
    counts = {}
    if ran:
        counts["passed"] = max(ran - failed - errors, 0)
    if failed:
        counts["failed"] = failed
    if errors:
        counts["error"] = errors
    return {"failed": set(), "passed": set(), "counts": counts}


# --- go (node-level with -v; package ok/FAIL lines otherwise) ---
_GO_FAIL = re.compile(r'^\s*--- FAIL: (\S+)', re.M)
_GO_PASS = re.compile(r'^\s*--- PASS: (\S+)', re.M)
_GO_PKG_OK = re.compile(r'^ok\s+\S+', re.M)
_GO_PKG_FAIL = re.compile(r'^FAIL\b', re.M)


def parse_go(output):
    failed = set(_GO_FAIL.findall(output))
    passed = set(_GO_PASS.findall(output)) - failed
    counts = {}
    if failed:
        counts["failed"] = len(failed)
    elif _GO_PKG_FAIL.search(output):
        counts["failed"] = 1                      # package failed, nodes not printed
    if passed:
        counts["passed"] = len(passed)
    elif not counts.get("failed") and _GO_PKG_OK.search(output):
        counts["passed"] = 1                      # quiet `ok pkg 0.01s`
    return {"failed": failed, "passed": passed, "counts": counts}


# --- cargo (node lines + `test result:` summary; summaries sum across targets) ---
_C_NODE = re.compile(r'^test (\S+) \.\.\. (ok|FAILED|ignored)', re.M)
_C_RESULT = re.compile(r'^test result: \S+\s+(\d+) passed; (\d+) failed', re.M)


def parse_cargo(output):
    failed, passed = set(), set()
    for name, status in _C_NODE.findall(output):
        if status == "FAILED":
            failed.add(name)
        elif status == "ok":
            passed.add(name)
    counts = {}
    for p, f in _C_RESULT.findall(output):
        counts["passed"] = counts.get("passed", 0) + int(p)
        counts["failed"] = counts.get("failed", 0) + int(f)
    if counts.get("failed") == 0:
        del counts["failed"]
    return {"failed": failed, "passed": passed, "counts": counts}


# --- js (counts-only): jest `Tests: 2 failed, 10 passed`, vitest
# `Tests  1 failed | 3 passed (4)`, mocha `5 passing` / `2 failing` ---
_JS_SUMMARY = re.compile(r'^\s*Tests:?\s+([^\n]*)', re.M)
_JS_PAIR = re.compile(r'(\d+)\s+(passed|failed|skipped|pending|passing|failing|todo)')
_JS_KIND = {"passing": "passed", "failing": "failed", "pending": "skipped",
            "todo": "skipped"}


def parse_js(output):
    m = _JS_SUMMARY.search(output)
    scope = m.group(1) if m else output           # mocha has no summary line
    counts = {}
    for n, kind in _JS_PAIR.findall(scope):
        k = _JS_KIND.get(kind, kind)
        counts[k] = counts.get(k, 0) + int(n)
    return {"failed": set(), "passed": set(), "counts": counts}


_PARSERS = {
    "pytest": parse_pytest,
    "unittest": parse_unittest,
    "go": parse_go,
    "cargo": parse_cargo,
    "js": parse_js,
}


def parse(output, framework):
    parser = _PARSERS.get(framework, parse_pytest)   # legacy runs carry no tag
    return parser(output or "")
