#!/usr/bin/env python3
"""Cross-agent recent-session picker/resumer."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HOME = Path.home()

@dataclass
class Session:
    agent: str
    sid: str
    cwd: str
    updated: float
    title: str = ""
    path: str = ""

    @property
    def when(self) -> str:
        if not self.updated:
            return "unknown"
        return datetime.fromtimestamp(self.updated, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def parse_ts(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        # Cursor stores ms.
        return value / 1000 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def first_text(obj: Any, limit: int = 90) -> str:
    """Pull a compact human-ish snippet out of nested agent JSON."""
    try:
        if isinstance(obj, str):
            text = obj
        elif isinstance(obj, dict):
            bits = []
            for key in ("content", "text", "summary", "thread_name", "name"):
                if key in obj:
                    bits.append(first_text(obj[key], limit=limit))
            if not bits:
                for v in obj.values():
                    t = first_text(v, limit=limit)
                    if t:
                        bits.append(t)
                        break
            text = " ".join(x for x in bits if x)
        elif isinstance(obj, list):
            text = " ".join(first_text(x, limit=limit) for x in obj[:4])
        else:
            text = ""
    except RecursionError:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"<environment_context>.*?</environment_context>", "", text, flags=re.I)
    text = text.replace("<user_query>", "").replace("</user_query>", "")
    return text[:limit]


def decode_claude_slug(slug: str) -> str:
    # Claude project slugs are usually absolute paths with '/' replaced by '-'.
    # This is lossy for literal hyphens, so use only as fallback when cwd is absent.
    if slug.startswith("-"):
        return "/" + slug[1:].replace("-", "/")
    return slug.replace("-", "/")


def load_jsonl(path: Path, max_lines: int = 80) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except OSError:
        return


def claude_sessions() -> list[Session]:
    out: list[Session] = []
    root = HOME / ".claude/projects"
    if not root.exists():
        return out
    for p in root.glob("*/*.jsonl"):
        sid = p.stem
        cwd = ""
        title = ""
        updated = p.stat().st_mtime
        for msg in load_jsonl(p, 120):
            updated = max(updated, parse_ts(msg.get("timestamp")))
            cwd = cwd or msg.get("cwd") or (msg.get("attachment") or {}).get("cwd") or ""
            if not title and msg.get("type") in {"queue-operation", "user"}:
                title = first_text(msg.get("content") or msg)
            if cwd and title:
                break
        if not cwd:
            cwd = decode_claude_slug(p.parent.name)
        out.append(Session("claude", sid, cwd, updated, title, str(p)))
    return out


def codex_sessions() -> list[Session]:
    out: list[Session] = []
    root = HOME / ".codex/sessions"
    if not root.exists():
        return out
    titles: dict[str, str] = {}
    idx = HOME / ".codex/session_index.jsonl"
    if idx.exists():
        for row in load_jsonl(idx, 5000):
            if row.get("id"):
                titles[row["id"]] = row.get("thread_name") or ""
    for p in root.glob("**/*.jsonl"):
        sid = ""
        cwd = ""
        title = ""
        updated = p.stat().st_mtime
        for msg in load_jsonl(p, 120):
            updated = max(updated, parse_ts(msg.get("timestamp")))
            if msg.get("type") == "session_meta":
                payload = msg.get("payload") or {}
                sid = sid or payload.get("id") or ""
                cwd = cwd or payload.get("cwd") or ""
            if not title and msg.get("type") == "response_item":
                payload = msg.get("payload") or {}
                if payload.get("role") == "user":
                    title = first_text(payload.get("content"))
            if sid and cwd and title:
                break
        sid = sid or p.stem.split("-")[-1]
        title = titles.get(sid) or title
        out.append(Session("codex", sid, cwd, updated, title, str(p)))
    return out


def cursor_sessions() -> list[Session]:
    out: list[Session] = []
    root = HOME / ".cursor/projects"
    if not root.exists():
        return out
    for p in root.glob("*/agent-transcripts/*/*.jsonl"):
        sid = p.stem
        project_slug = p.relative_to(root).parts[0]
        # Cursor project directory names are like Users-qm4-Projects-repo.
        cwd = "/" + project_slug.replace("-", "/")
        title = ""
        updated = p.stat().st_mtime
        for msg in load_jsonl(p, 120):
            if not title and msg.get("role") == "user":
                candidate = first_text(msg.get("message"))
                # Cursor transcript dumps may prepend synthetic system/context messages.
                if candidate and not candidate.lower().startswith(("[system]", "# soul.md", "<timestamp>")):
                    title = candidate
            if title:
                break
        out.append(Session("cursor", sid, cwd, updated, title, str(p)))
    return out



def pi_sessions() -> list[Session]:
    out: list[Session] = []
    root = HOME / ".pi/agent/sessions"
    if not root.exists():
        return out
    for p in root.glob("*/*.jsonl"):
        sid = ""
        cwd = ""
        title = ""
        updated = p.stat().st_mtime
        for msg in load_jsonl(p, 120):
            updated = max(updated, parse_ts(msg.get("timestamp")))
            if msg.get("type") == "session":
                sid = sid or msg.get("id") or ""
                cwd = cwd or msg.get("cwd") or ""
            if not title and msg.get("type") == "message":
                m = msg.get("message") or {}
                if m.get("role") == "user":
                    title = first_text(m.get("content"))
            if sid and cwd and title:
                break
        sid = sid or p.stem.split("_")[-1]
        if not cwd:
            cwd = decode_claude_slug(p.parent.name.strip("-"))
        out.append(Session("pi", sid, cwd, updated, title, str(p)))
    return out


def hermes_sessions() -> list[Session]:
    out: list[Session] = []
    root = HOME / ".hermes/sessions"
    if not root.exists():
        return out
    meta_by_id: dict[str, dict[str, Any]] = {}
    idx = root / "sessions.json"
    if idx.exists():
        try:
            data = json.loads(idx.read_text())
            for row in data.values() if isinstance(data, dict) else []:
                if isinstance(row, dict) and row.get("session_id"):
                    meta_by_id[row["session_id"]] = row
        except Exception:
            pass
    seen_paths: set[Path] = set()
    for p in root.glob("session_*.json"):
        seen_paths.add(p)
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        sid = data.get("session_id") or p.stem.removeprefix("session_")
        meta = meta_by_id.get(sid, {})
        platform = data.get("platform") or meta.get("platform") or "hermes"
        display = meta.get("display_name") or ""
        title = display if display and display != "—" else ""
        messages = data.get("messages") or []
        if not title:
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "user":
                    title = first_text(m.get("content"))
                    break
        cwd = ""
        sp = data.get("system_prompt") or ""
        m = re.search(r"Current working directory:\s*([^\n]+)", sp)
        if m:
            cwd = m.group(1).strip()
        if not cwd:
            # Hermes sessions are resumable by id even when no project cwd is recoverable.
            origin = meta.get("origin") or {}
            cwd = f"hermes:{platform}" + (f":{origin.get('chat_type')}" if origin.get("chat_type") else "")
        updated = parse_ts(data.get("last_updated") or meta.get("updated_at") or data.get("session_start")) or p.stat().st_mtime
        out.append(Session("hermes", sid, cwd, updated, title, str(p)))
    # Some active sessions may be in sessions.json before a session_*.json is visible.
    for sid, meta in meta_by_id.items():
        if any(s.sid == sid for s in out):
            continue
        platform = meta.get("platform") or "hermes"
        origin = meta.get("origin") or {}
        cwd = f"hermes:{platform}" + (f":{origin.get('chat_type')}" if origin.get("chat_type") else "")
        out.append(Session("hermes", sid, cwd, parse_ts(meta.get("updated_at")), meta.get("display_name") or "", str(idx)))
    return out

def opencode_sessions(limit: int = 100) -> list[Session]:
    # Prefer the official CLI when present; it already knows its DB paths.
    exe = shutil_which("opencode")
    if not exe:
        return []
    try:
        cp = subprocess.run([exe, "session", "list", "--format", "json", "-n", str(limit)], text=True, capture_output=True, timeout=20)
    except Exception:
        return []
    if cp.returncode != 0 or not cp.stdout.strip():
        return []
    try:
        data = json.loads(cp.stdout)
    except Exception:
        return []
    rows = data if isinstance(data, list) else data.get("sessions", []) if isinstance(data, dict) else []
    out: list[Session] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("id") or row.get("sessionID") or row.get("sessionId") or ""
        cwd = row.get("cwd") or row.get("project") or row.get("path") or ""
        updated = parse_ts(row.get("updated") or row.get("updatedAt") or row.get("time"))
        title = row.get("title") or row.get("name") or ""
        if sid:
            out.append(Session("opencode", sid, cwd, updated, title, "opencode session list"))
    return out


def shutil_which(name: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / name
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def collect(include_hermes: bool = False) -> list[Session]:
    sessions = []
    fns = [claude_sessions, codex_sessions, cursor_sessions, pi_sessions, opencode_sessions]
    if include_hermes:
        fns.append(hermes_sessions)
    for fn in fns:
        try:
            sessions.extend(fn())
        except Exception as e:
            print(f"warn: {fn.__name__}: {e}", file=sys.stderr)
    # Deduplicate by agent+id, keeping newest path parse.
    by_key: dict[tuple[str, str], Session] = {}
    for s in sessions:
        key = (s.agent, s.sid)
        if key not in by_key or s.updated > by_key[key].updated:
            by_key[key] = s
    return sorted(by_key.values(), key=lambda s: s.updated, reverse=True)


def resume_command(s: Session) -> list[str]:
    if s.agent == "claude":
        return ["bash", "-lc", f"cd {shlex.quote(s.cwd)} && exec claude --resume {shlex.quote(s.sid)}"]
    if s.agent == "codex":
        return ["codex", "resume", "--all", "-C", s.cwd, s.sid]
    if s.agent == "cursor":
        return ["cursor-agent", "--workspace", s.cwd, "--resume", s.sid]
    if s.agent == "pi":
        cwd = s.cwd if s.cwd and not s.cwd.startswith("hermes:") else str(HOME)
        return ["bash", "-lc", f"cd {shlex.quote(cwd)} && exec pi --session {shlex.quote(s.path or s.sid)}"]
    if s.agent == "hermes":
        if s.cwd and s.cwd.startswith("/"):
            return ["bash", "-lc", f"cd {shlex.quote(s.cwd)} && exec hermes --resume {shlex.quote(s.sid)}"]
        return ["hermes", "--resume", s.sid]
    if s.agent == "opencode":
        return ["opencode", s.cwd or ".", "--session", s.sid]
    raise SystemExit(f"No resume command for {s.agent}")


def compact_title(s: Session, max_len: int = 42) -> str:
    title = re.sub(r"\s+", " ", (s.title or "").strip())
    if not title:
        title = s.sid
    return title[: max_len - 1] + "…" if len(title) > max_len else title


def compact_folder(s: Session, max_len: int = 34) -> str:
    cwd = s.cwd or "?"
    cwd = cwd.replace(str(HOME), "~")
    if cwd.startswith("hermes:"):
        folder = cwd
    else:
        folder = cwd.rstrip("/") or "/"
    return "…" + folder[-(max_len - 1):] if len(folder) > max_len else folder


def compact_when(s: Session) -> str:
    if not s.updated:
        return "unknown"
    return datetime.fromtimestamp(s.updated, timezone.utc).astimezone().strftime("%m-%d %H:%M")


def row_text(s: Session, width: int = 120) -> str:
    name_w = max(18, min(44, width - 58))
    folder_w = max(16, min(40, width - name_w - 32))
    name = compact_title(s, name_w)
    folder = compact_folder(s, folder_w)
    return f"{name:<{name_w}}  {s.agent:<8}  {folder:<{folder_w}}  {compact_when(s)}"


def render(rows: list[Session]) -> None:
    print(f"{'#':>3}  {'name':<42}  {'agent':<8}  {'folder':<34}  modified")
    print("-" * 100)
    for i, s in enumerate(rows, 1):
        print(f"{i:>3}  {compact_title(s, 42):<42}  {s.agent:<8}  {compact_folder(s, 34):<34}  {compact_when(s)}")


def run_tui(rows: list[Session]) -> int:
    if not rows:
        print("No sessions found")
        return 1
    import curses

    selected = 0
    top = 0

    def draw(stdscr):
        nonlocal selected, top
        curses.curs_set(0)
        stdscr.keypad(True)
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            header = "resume  ↑/↓ select  Enter resume  q quit"
            stdscr.addnstr(0, 0, header, w - 1, curses.A_BOLD)
            stdscr.addnstr(1, 0, f"{'name':<42}  {'agent':<8}  {'folder':<34}  modified", w - 1, curses.A_DIM)
            visible = max(1, h - 3)
            if selected < top:
                top = selected
            if selected >= top + visible:
                top = selected - visible + 1
            for screen_i, row_i in enumerate(range(top, min(len(rows), top + visible)), start=2):
                line = row_text(rows[row_i], w)
                attr = curses.A_REVERSE if row_i == selected else curses.A_NORMAL
                stdscr.addnstr(screen_i, 0, line, w - 1, attr)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord('q'), 27):
                return None
            if ch in (curses.KEY_UP, ord('k')):
                selected = max(0, selected - 1)
            elif ch in (curses.KEY_DOWN, ord('j')):
                selected = min(len(rows) - 1, selected + 1)
            elif ch in (curses.KEY_NPAGE,):
                selected = min(len(rows) - 1, selected + visible)
            elif ch in (curses.KEY_PPAGE,):
                selected = max(0, selected - visible)
            elif ch in (10, 13, curses.KEY_ENTER):
                return selected

    idx = curses.wrapper(draw)
    if idx is None:
        return 0
    cmd = resume_command(rows[idx])
    os.execvp(cmd[0], cmd)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="List and resume recent coding-agent sessions across agents and folders.")
    ap.add_argument("query", nargs="*", help="case-insensitive filter across agent, cwd, title, session id")
    ap.add_argument("-n", "--limit", type=int, default=40)
    ap.add_argument("--agent", choices=["claude", "codex", "cursor", "pi", "hermes", "opencode"])
    ap.add_argument("--include-hermes", action="store_true", help="include Hermes sessions in the default all-agent list")
    ap.add_argument("--exec", dest="exec_index", type=int, help="resume the numbered row from the filtered list")
    ap.add_argument("--print-cmd", type=int, metavar="N", help="print resume command for row N instead of executing")
    ap.add_argument("--json", action="store_true", help="print machine-readable sessions")
    ap.add_argument("--tui", action="store_true", help="force interactive arrow-key picker")
    ap.add_argument("--no-tui", action="store_true", help="print concise list instead of opening the picker")
    args = ap.parse_args()

    include_hermes = args.include_hermes or args.agent == "hermes"
    rows = collect(include_hermes=include_hermes)
    if args.agent:
        rows = [s for s in rows if s.agent == args.agent]
    if args.query:
        q = " ".join(args.query).lower()
        rows = [s for s in rows if q in " ".join([s.agent, s.sid, s.cwd, s.title, s.path]).lower()]
    rows = rows[: max(1, args.limit)]

    if args.json:
        print(json.dumps([s.__dict__ for s in rows], indent=2, ensure_ascii=False))
        return 0

    if args.print_cmd or args.exec_index:
        idx = (args.print_cmd or args.exec_index) - 1
        if idx < 0 or idx >= len(rows):
            raise SystemExit(f"index out of range: {idx+1}; filtered rows={len(rows)}")
        cmd = resume_command(rows[idx])
        if args.print_cmd:
            print(" ".join(shlex.quote(x) for x in cmd))
            return 0
        os.execvp(cmd[0], cmd)

    if args.tui or (sys.stdin.isatty() and sys.stdout.isatty() and not args.no_tui):
        return run_tui(rows)

    render(rows)
    print("\nResume: resume [filter...] --exec N  |  TUI: run in a terminal  |  Hermes: --include-hermes")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
