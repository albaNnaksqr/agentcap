"""Extract shell/test-run events (command + output + time) from a raw agent
trajectory log. Per-agent shapes, verified against real logs:

  Claude: assistant.message.content[] -> tool_use{name:Bash, input.command, id}
          paired with a later tool_result{tool_use_id, content}
  Codex:  response_item.payload -> function_call{arguments(JSON: cmd), call_id}
          paired with function_call_output{call_id, output}
          (>=0.144) custom_tool_call{name:"exec", input: JS with
          tools.exec_command({cmd:...})} paired with custom_tool_call_output

We only surface runs whose command a known test framework claims (testparse) —
each run carries its framework tag. If a log can't be parsed, it yields nothing
(no event beats a wrong event)."""
import json
import re

from . import testparse

# codex exec `input` may quote the key or not: {cmd:...} or {"cmd":...}
_CODEX_CMD_KEY = re.compile(r'["\']?cmd["\']?\s*:\s*')

# A codex exec cell that outlives its `yield_time_ms` does NOT return its output.
# It returns a placeholder naming the still-running cell, and the agent collects
# the real output with a later `wait({cell_id})`. Left unstitched, every slow test
# run looks like a run that produced nothing -- which is not the same as a run that
# produced no passes, but downstream (value.ended_green, taskseed._timeline) cannot
# tell the difference and reads it as "not green". Seen on litellm#36487, where a
# 13s directory run ending `57 passed` was scored ungrounded.
_CODEX_YIELD_STUB = re.compile(r'Script running with cell ID\s+(\S+)')
_CODEX_WAIT_CELL = re.compile(r'["\']?cell_id["\']?\s*:\s*["\']?([^"\',}\s]+)')

# Newer codex exec returns a script's result as a JSON envelope instead of raw
# stdout -- either flat, {"chunk_id":..,"exit_code":..,"output":"<escaped>"}, or
# keyed by cell, {"plan":{},"red":{"chunk_id":..,"output":"<escaped>"}}. The
# stdout inside is JSON-escaped, so the text we end up with has literal "\n"
# and no real line breaks.
#
# That is worse than it looks. testparse's summary regexes still match ("6 failed
# in 3.62s"), so the run is not obviously broken -- but every PER-LINE extraction
# silently returns nothing, and the failing/passing test NAMES are exactly what is
# extracted per line. No names -> taskseed._timeline sees no red->green flip ->
# seed writes nothing -> replay reports no_runnable_tests -> `verified` can never
# be earned, for a session that demonstrably went red then green.
# Seen on litellm#35428, whose RED run (`6 failed`) and full-suite run both came
# back enveloped while the small green run came back as raw text.
#
# `chunk_id` is the envelope's fingerprint: we only unwrap `output` values that
# sit in a dict carrying it, so a test that legitimately prints JSON with an
# "output" key is left alone.
_CODEX_ENVELOPE_HINT = "chunk_id"


def _text(content):
    """Flatten a tool_result 'content' (str | list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                out.append(b.get("text") or b.get("content") or "")
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(x for x in out if x)
    return ""


def _is_test_cmd(cmd):
    return testparse.detect_framework(cmd) is not None


def _codex_exec_cmd(input_str):
    """codex >=0.144 runs shell via a `custom_tool_call` (name="exec") whose
    `input` is a JS snippet, e.g.
        const r = await tools.exec_command({cmd:"pytest ...",workdir:"..."});
    Pull the shell command out of the first exec_command({cmd:...}) call. The
    value after `cmd:` is valid JSON (a string, or a list of argv), so decode it
    directly. Non-shell tools (update_plan, ...) have no exec_command -> None."""
    if not isinstance(input_str, str):
        return None
    i = input_str.find("exec_command")
    if i == -1:
        return None
    m = _CODEX_CMD_KEY.search(input_str, i)
    if not m:
        return None
    try:
        val, _ = json.JSONDecoder().raw_decode(input_str, m.end())
    except ValueError:
        return None
    if isinstance(val, list):
        return " ".join(str(x) for x in val)
    return val if isinstance(val, str) else None


def _claude_raw(path):
    """Parse a claude log into (calls, outputs, order) -- shared by the test-run
    view and the full command view, so both see exactly the same events."""
    calls, outputs, order = {}, {}, []
    for line in _lines(path):
        d = _loads(line)
        if not d:
            continue
        ts = d.get("timestamp")
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "Bash":
                cmd = (b.get("input") or {}).get("command")
                calls[b.get("id")] = (cmd, ts)
                order.append(b.get("id"))
            elif b.get("type") == "tool_result":
                outputs[b.get("tool_use_id")] = _text(b.get("content"))
    return calls, outputs, order


def claude_runs(path):
    return _pair(*_claude_raw(path))


def _codex_raw(path):
    """Parse a codex log into (calls, outputs, order, waits). See _claude_raw."""
    calls, outputs, order = {}, {}, []
    waits = {}   # call_id of a `wait` -> the cell id it is collecting
    for line in _lines(path):
        d = _loads(line)
        if not d:
            continue
        ts = d.get("timestamp")
        p = d.get("payload") if isinstance(d.get("payload"), dict) else d
        pt = p.get("type")
        if pt in ("function_call", "custom_tool_call") and p.get("name") == "wait":
            # `wait` arrives as a function_call (args in `arguments`) or a
            # custom_tool_call (args in `input`) depending on codex version.
            src = p.get("input") if isinstance(p.get("input"), str) else p.get("arguments")
            m = _CODEX_WAIT_CELL.search(src or "")
            if m:
                waits[p.get("call_id")] = m.group(1)
            # A wait is not itself a run, but its OUTPUT is the tail of one, so
            # it still has to occupy a slot in `order`/`outputs`. cmd=None keeps
            # `_pair` from ever surfacing it as a run of its own.
            calls[p.get("call_id")] = (None, ts)
            order.append(p.get("call_id"))
        elif pt == "function_call":
            args = p.get("arguments")
            if isinstance(args, str):
                a = _loads(args) or {}
            else:
                a = args or {}
            cmd = a.get("cmd") or a.get("command")
            if isinstance(cmd, list):
                cmd = " ".join(cmd)
            cid = p.get("call_id")
            calls[cid] = (cmd, ts)
            order.append(cid)
        elif pt == "custom_tool_call" and p.get("name") == "exec":
            cmd = _codex_exec_cmd(p.get("input"))
            cid = p.get("call_id")
            calls[cid] = (cmd, ts)
            order.append(cid)
        elif pt in ("function_call_output", "custom_tool_call_output"):
            out = p.get("output")
            txt = out if isinstance(out, str) else _text(out)
            outputs[p.get("call_id")] = _unwrap_codex_envelopes(txt)
    return calls, outputs, order, waits


def codex_runs(path):
    calls, outputs, order, waits = _codex_raw(path)
    _stitch_yielded_cells(outputs, order, waits)
    return _pair(calls, outputs, order)


def _unwrap_codex_envelopes(text):
    """Replace each codex JSON result envelope in `text` with the stdout it carries.

    Text around the envelope is kept -- the preamble ("Script completed / Wall
    time ... / Output:", and codex's own "Warning: truncated output (original
    token count: N)") is real information, and the truncation warning is the only
    signal that what follows is incomplete. So this rewrites the envelope's span
    in place rather than returning the payload alone.

    A dict qualifies only if it carries `chunk_id`; `output` values are collected
    from any depth in traversal order, so both the flat and the keyed-by-cell
    shapes work, and a multi-cell script's cells stay in the order they ran."""
    if not text or _CODEX_ENVELOPE_HINT not in text:
        return text
    dec = json.JSONDecoder()
    parts, i = [], 0
    while True:
        j = text.find("{", i)
        if j == -1:
            break
        try:
            obj, end = dec.raw_decode(text, j)
        except ValueError:
            # not the start of a JSON value -- step past this brace, don't give up:
            # a later brace in the same blob may still be the envelope.
            parts.append(text[i:j + 1])
            i = j + 1
            continue
        payload = _collect_envelope_output(obj)
        if payload is None:
            parts.append(text[i:end])       # real JSON, but not ours: leave verbatim
        else:
            parts.append(text[i:j])
            parts.append(payload)
        i = end
    parts.append(text[i:])
    return "".join(parts)


def _collect_envelope_output(obj):
    """stdout carried by a codex envelope, or None if `obj` isn't one.

    Returns "" for an envelope whose output is empty (a cell that ran and printed
    nothing) -- distinct from None, so an empty cell still consumes its span
    instead of leaving the raw JSON in the text."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            is_env = _CODEX_ENVELOPE_HINT in o
            for k, v in o.items():
                if is_env and k == "output" and isinstance(v, str):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    if not found:
        return None
    return "\n".join(found)


def _stitch_yielded_cells(outputs, order, waits):
    """Append each `wait`'s output back onto the exec cell it was collecting.

    Matching is by cell id, not by adjacency: the agent may interleave other
    tool calls between the yield and the wait, and a single cell can yield more
    than once (each `wait` can itself time out). We therefore walk forward from
    the stub and take every later wait on that same cell id, in order.

    Appending, never replacing -- a yield can carry partial stdout before the
    placeholder, and dropping it would trade one lossy read for another."""
    pending = {}   # cell id -> call_id of the exec that yielded on it
    for cid in order:
        out = outputs.get(cid) or ""
        if cid in waits:
            owner = pending.get(waits[cid])
            if owner is not None and out:
                outputs[owner] = (outputs.get(owner) or "") + "\n" + out
                # A wait can time out too and hand back another placeholder;
                # keep the cell pending so the next wait on it also lands.
                if not _CODEX_YIELD_STUB.search(out):
                    pending.pop(waits[cid], None)
            continue
        m = _CODEX_YIELD_STUB.search(out)
        if m:
            pending[m.group(1)] = cid


def _pair(calls, outputs, order, framework_only=True):
    runs = []
    for i, cid in enumerate(order):
        cmd, ts = calls.get(cid, (None, None))
        if cmd is None:
            continue
        fw = testparse.detect_framework(cmd)
        if framework_only and fw is None:
            continue
        runs.append({"cmd": cmd, "output": outputs.get(cid, ""), "ts": ts, "idx": i,
                     "framework": fw})
    return runs


def shell_commands(path, agent):
    """EVERY shell command the agent ran, not just the test runs.

    The task contract forbids some commands outright (git commit/add/stash/
    checkout) because they destroy the uncommitted diff the harness reads to
    attribute authorship. Until now the contract only STATED the rule -- nothing
    detected a violation, and one happened: sglang#35564's headless run used
    `git stash push` three times to check its test went red. No damage that time
    (the pops all succeeded), but a failed pop, or a mark-end landing mid-stash,
    would have left an empty or partial delta.

    Checking the git state afterwards cannot find this -- a popped stash leaves
    nothing behind. The trajectory is the only record of what was actually run."""
    if agent == "claude":
        return _all_claude(path)
    if agent == "codex":
        return _all_codex(path)
    c, x = _all_claude(path), _all_codex(path)
    return c if len(c) >= len(x) else x


def _all_claude(path):
    calls, outputs, order = _claude_raw(path)
    return _pair(calls, outputs, order, framework_only=False)


def _all_codex(path):
    calls, outputs, order, waits = _codex_raw(path)
    _stitch_yielded_cells(outputs, order, waits)
    return _pair(calls, outputs, order, framework_only=False)


def test_runs(path, agent):
    if agent == "claude":
        return claude_runs(path)
    if agent == "codex":
        return codex_runs(path)
    # unknown agent: try both, keep whichever finds more
    c, x = claude_runs(path), codex_runs(path)
    return c if len(c) >= len(x) else x


_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_PATCH_KW = ("*** Add File:", "*** Update File:", "*** Delete File:")


def edit_events(path, agent=None):
    """Files the agent edited during the session, WITH repeats (so re-editing the
    same file shows up as churn). Works across both agent shapes: Claude
    Edit/Write/MultiEdit tool_use, Codex apply_patch custom_tool_call."""
    files = []
    for line in _lines(path):
        d = _loads(line)
        if not d:
            continue
        p = d.get("payload") if isinstance(d.get("payload"), dict) else d
        if p.get("type") == "custom_tool_call" and p.get("name") == "apply_patch":
            for ln in (p.get("input") or "").splitlines():
                s = ln.strip()
                for kw in _PATCH_KW:
                    if s.startswith(kw):
                        files.append(s.split(":", 1)[1].strip())
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" \
                        and b.get("name") in _EDIT_TOOLS:
                    fp = (b.get("input") or {}).get("file_path")
                    if fp:
                        files.append(fp)
    return files


def _lines(path):
    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                yield line
    except OSError:
        return


def _loads(s):
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None
