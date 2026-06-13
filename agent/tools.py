"""Tool implementations + JSON schemas exposed to the LLM.

Nine tools total. Every tool returns a string (truncated if large). The
`finish` tool returns the sentinel FINISH_SIGNAL so loop.py can detect it.

Path safety: every filesystem-touching tool funnels through `_resolve`, which
rejects paths that escape the repo directory.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

FINISH_SIGNAL = "FINISH_SIGNALED"

MAX_OUTPUT_BYTES = 12 * 1024          # cap of tool output sent back to LLM
MAX_READ_BYTES = 80 * 1024            # cap on read_file content
MAX_GREP_HITS = 200                   # cap on grep result lines
GO_TIMEOUT_SECS = 180                 # subprocess timeout for go commands


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _resolve(repo_dir: Path, rel: str) -> Path:
    if rel in ("", "."):
        return repo_dir.resolve()
    p = (repo_dir / rel).resolve()
    root = repo_dir.resolve()
    if root != p and root not in p.parents:
        raise ValueError(f"path escapes repo: {rel!r}")
    return p


def _truncate(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    head = data[: limit // 2].decode("utf-8", errors="replace")
    tail = data[-limit // 2 :].decode("utf-8", errors="replace")
    skipped = len(data) - limit
    return f"{head}\n\n[... {skipped} bytes truncated ...]\n\n{tail}"


# ---------------------------------------------------------------------------
# Go toolchain discovery (Windows-friendly)
# ---------------------------------------------------------------------------

def _find_go() -> str:
    found = shutil.which("go")
    if found:
        return found
    if sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\Go\bin\go.exe",
            r"C:\Go\bin\go.exe",
            os.path.expanduser(r"~\go\bin\go.exe"),
        ):
            if os.path.isfile(candidate):
                return candidate
    raise RuntimeError("Could not locate the `go` executable on PATH.")


def _run_go(args: list[str], cwd: Path) -> str:
    go = _find_go()
    proc = subprocess.run(
        [go, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=GO_TIMEOUT_SECS,
    )
    out = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
    status = "ok" if proc.returncode == 0 else f"FAILED (exit {proc.returncode})"
    return f"$ go {' '.join(args)}  [{status}]\n{out}"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def t_list_dir(repo_dir: Path, *, path: str = ".") -> str:
    p = _resolve(repo_dir, path)
    if not p.exists():
        return f"ERROR: not found: {path}"
    if not p.is_dir():
        return f"ERROR: not a directory: {path}"
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = []
    for e in entries:
        if e.name.startswith(".git"):
            continue
        suffix = "/" if e.is_dir() else ""
        lines.append(f"{e.name}{suffix}")
    return "\n".join(lines) if lines else "(empty)"


def t_read_file(
    repo_dir: Path,
    *,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    p = _resolve(repo_dir, path)
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    data = p.read_bytes()
    if len(data) > MAX_READ_BYTES and start_line is None and end_line is None:
        return (
            f"ERROR: file is {len(data)} bytes (cap {MAX_READ_BYTES}). "
            f"Pass start_line/end_line to read a slice."
        )
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    s = max(1, start_line or 1)
    e = min(len(lines), end_line or len(lines))
    out = [f"{i:>6}  {lines[i - 1]}" for i in range(s, e + 1)]
    header = f"# {path} (lines {s}-{e} of {len(lines)})\n"
    return _truncate(header + "\n".join(out))


def t_grep(
    repo_dir: Path,
    *,
    pattern: str,
    path: str = ".",
    case_insensitive: bool = False,
) -> str:
    try:
        flags = re.IGNORECASE if case_insensitive else 0
        rx = re.compile(pattern, flags)
    except re.error as e:
        return f"ERROR: bad regex: {e}"

    root = _resolve(repo_dir, path)
    if not root.exists():
        return f"ERROR: not found: {path}"

    hits: list[str] = []
    targets = [root] if root.is_file() else root.rglob("*")
    for f in targets:
        if not f.is_file():
            continue
        if ".git" in f.parts:
            continue
        try:
            data = f.read_bytes()
            if b"\x00" in data[:4096]:
                continue
            text = data.decode("utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                rel = f.relative_to(repo_dir).as_posix()
                hits.append(f"{rel}:{i}: {line}")
                if len(hits) >= MAX_GREP_HITS:
                    hits.append(f"... (hit cap of {MAX_GREP_HITS})")
                    return _truncate("\n".join(hits))
    return _truncate("\n".join(hits)) if hits else "(no matches)"


def t_edit_file(
    repo_dir: Path,
    *,
    path: str,
    old_str: str,
    new_str: str,
) -> str:
    p = _resolve(repo_dir, path)
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_str)
    if count == 0:
        return (
            f"ERROR: old_str not found in {path}. "
            f"Re-read the file and copy exact text including whitespace."
        )
    if count > 1:
        return (
            f"ERROR: old_str matches {count} times in {path}. "
            f"Add surrounding context until it matches exactly once."
        )
    new_text = text.replace(old_str, new_str, 1)
    p.write_text(new_text, encoding="utf-8")
    return f"OK: edited {path} (1 replacement)"


def t_write_file(repo_dir: Path, *, path: str, content: str) -> str:
    p = _resolve(repo_dir, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    return f"OK: {'overwrote' if existed else 'created'} {path} ({len(content)} bytes)"


def t_go_build(repo_dir: Path, *, package: str = "./...") -> str:
    return _truncate(_run_go(["build", package], repo_dir))


def t_go_vet(repo_dir: Path, *, package: str = "./...") -> str:
    return _truncate(_run_go(["vet", package], repo_dir))


def t_go_test(
    repo_dir: Path,
    *,
    package: str = "./...",
    run: str | None = None,
) -> str:
    args = ["test", package, "-count=1"]
    if run:
        args += ["-run", run]
    return _truncate(_run_go(args, repo_dir))


def t_gofmt(repo_dir: Path, *, path: str = ".") -> str:
    p = _resolve(repo_dir, path)
    gofmt = shutil.which("gofmt")
    if not gofmt:
        # gofmt ships in the same bin dir as go; try alongside _find_go()
        go = _find_go()
        candidate = Path(go).with_name("gofmt" + (".exe" if sys.platform == "win32" else ""))
        if candidate.exists():
            gofmt = str(candidate)
    if not gofmt:
        return "ERROR: gofmt not found"
    proc = subprocess.run(
        [gofmt, "-l", str(p)],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = proc.stdout.strip()
    if not out:
        return "OK: all files formatted"
    return _truncate("Unformatted files:\n" + out)


def t_finish(repo_dir: Path, *, pr_title: str, pr_body: str) -> str:
    # Sentinel — loop.py inspects this and pulls the title/body from the args.
    return FINISH_SIGNAL


# ---------------------------------------------------------------------------
# Registry + JSON schemas exposed to the API
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    name: str
    fn: Callable[..., str]
    schema: dict[str, Any]


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOLS: dict[str, Tool] = {
    "list_dir": Tool(
        "list_dir", t_list_dir,
        _schema(
            "list_dir",
            "List entries (files and subdirectories) at a path inside the repo.",
            {"path": {"type": "string", "description": "Path relative to the repo root. Defaults to '.'."}},
            [],
        ),
    ),
    "read_file": Tool(
        "read_file", t_read_file,
        _schema(
            "read_file",
            "Read a file with line numbers. Pass start_line/end_line to read a slice; required for files >80KB.",
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            ["path"],
        ),
    ),
    "grep": Tool(
        "grep", t_grep,
        _schema(
            "grep",
            "Recursive regex search. Returns up to 200 'path:line: text' hits.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Restrict search to this subpath. Defaults to '.'."},
                "case_insensitive": {"type": "boolean"},
            },
            ["pattern"],
        ),
    ),
    "edit_file": Tool(
        "edit_file", t_edit_file,
        _schema(
            "edit_file",
            "Replace EXACTLY ONE occurrence of old_str with new_str in the given file. "
            "If old_str is absent or matches >1 times, the call fails and you must include more context.",
            {
                "path": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
            },
            ["path", "old_str", "new_str"],
        ),
    ),
    "write_file": Tool(
        "write_file", t_write_file,
        _schema(
            "write_file",
            "Overwrite or create a whole file. Prefer edit_file when modifying existing files.",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
    ),
    "go_build": Tool(
        "go_build", t_go_build,
        _schema(
            "go_build",
            "Run `go build` against a package pattern. Defaults to ./...",
            {"package": {"type": "string"}},
            [],
        ),
    ),
    "go_vet": Tool(
        "go_vet", t_go_vet,
        _schema(
            "go_vet",
            "Run `go vet` against a package pattern. Defaults to ./...",
            {"package": {"type": "string"}},
            [],
        ),
    ),
    "go_test": Tool(
        "go_test", t_go_test,
        _schema(
            "go_test",
            "Run `go test` against a package pattern. Pass `run` to scope by regex (-run).",
            {"package": {"type": "string"}, "run": {"type": "string"}},
            [],
        ),
    ),
    "gofmt": Tool(
        "gofmt", t_gofmt,
        _schema(
            "gofmt",
            "List files that are not gofmt-formatted under a path.",
            {"path": {"type": "string"}},
            [],
        ),
    ),
    "finish": Tool(
        "finish", t_finish,
        _schema(
            "finish",
            "Signal completion. Provide pr_title and pr_body. Call this only after build + vet pass.",
            {"pr_title": {"type": "string"}, "pr_body": {"type": "string"}},
            ["pr_title", "pr_body"],
        ),
    ),
}


def schemas() -> list[dict[str, Any]]:
    return [t.schema for t in TOOLS.values()]


def dispatch(name: str, args_json: str, repo_dir: Path) -> str:
    """Execute a tool by name with JSON-encoded arguments."""
    if name not in TOOLS:
        return f"ERROR: unknown tool {name!r}"
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as e:
        return f"ERROR: invalid JSON arguments: {e}"
    if not isinstance(args, dict):
        return "ERROR: arguments must be a JSON object"
    try:
        return TOOLS[name].fn(repo_dir, **args)
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
    except ValueError as e:
        return f"ERROR: {e}"
    except subprocess.TimeoutExpired:
        return f"ERROR: {name} timed out after {GO_TIMEOUT_SECS}s"
    except Exception as e:  # pragma: no cover — last-resort guard
        return f"ERROR: {name} crashed: {e!r}"
