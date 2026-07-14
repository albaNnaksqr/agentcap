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


def claude_runs(path):
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
    return _pair(calls, outputs, order)


def codex_runs(path):
    calls, outputs, order = {}, {}, []
    for line in _lines(path):
        d = _loads(line)
        if not d:
            continue
        ts = d.get("timestamp")
        p = d.get("payload") if isinstance(d.get("payload"), dict) else d
        pt = p.get("type")
        if pt == "function_call":
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
            outputs[p.get("call_id")] = out if isinstance(out, str) else _text(out)
    return _pair(calls, outputs, order)


def _pair(calls, outputs, order):
    runs = []
    for i, cid in enumerate(order):
        cmd, ts = calls.get(cid, (None, None))
        fw = testparse.detect_framework(cmd)
        if fw is None:
            continue
        runs.append({"cmd": cmd, "output": outputs.get(cid, ""), "ts": ts, "idx": i,
                     "framework": fw})
    return runs


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
