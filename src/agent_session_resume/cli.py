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
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HOME = Path.home()
VERSION = "0.2.2"
DEFAULT_MIN_USER_MESSAGES = 1
DEFAULT_MIN_AGENT_MESSAGES = 1
OLD_SHORT_SESSION_THRESHOLD = 3
DEFAULT_SCAN_BUDGET = 80
UUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


@dataclass
class Session:
    agent: str
    sid: str
    cwd: str
    updated: float
    title: str = ""
    path: str = ""
    message_count: int = 0
    hidden_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    title_source: str = ""
    cwd_source: str = ""
    updated_source: str = ""
    schema: str = ""
    count_exact: bool = True
    resume_id: str = ""
    agent_message_count: int = 0

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
    text = re.sub(r"<environment_context>.*?</environment_context>", "", text, flags=re.I | re.S)
    text = re.sub(r"<timestamp>.*?</timestamp>", "", text, flags=re.I | re.S)
    text = text.replace("<user_query>", "").replace("</user_query>", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]



def raw_text(obj: Any, limit: int = 5000) -> str:
    """Extract nested text without stripping wrappers; useful for schema detection."""
    try:
        if isinstance(obj, str):
            text = obj
        elif isinstance(obj, dict):
            bits = []
            for key in ("content", "text", "message", "summary", "thread_name", "name"):
                if key in obj:
                    bits.append(raw_text(obj[key], limit=limit))
            if not bits:
                for v in obj.values():
                    t = raw_text(v, limit=limit)
                    if t:
                        bits.append(t)
                        break
            text = " ".join(x for x in bits if x)
        elif isinstance(obj, list):
            text = " ".join(raw_text(x, limit=limit) for x in obj[:8])
        else:
            text = ""
    except RecursionError:
        text = ""
    return text[:limit]


def is_synthetic_text(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").strip()).lower()
    if not t:
        return True
    synthetic_prefixes = (
        "# agents.md instructions for",
        "<environment_context>",
        "[system]",
        "# soul.md",
        "the following is the codex agent history whose request action you are assessing",
        "the following is the codex agent history added since your last approval assessment",
    )
    return t.startswith(synthetic_prefixes) or ("<environment_context>" in t and "<cwd>" in t)


def codex_sid_from_filename(path: Path) -> str:
    matches = UUID_RE.findall(path.stem)
    return matches[-1] if matches else path.stem.split("_")[-1]


def extract_cwd_from_text(text: str) -> str:
    for pat in (r"<cwd>\s*([^<]+?)\s*</cwd>", r"Current working directory:\s*([^\n]+)"):
        m = re.search(pat, text or "", flags=re.I | re.S)
        if m:
            return m.group(1).strip()
    return ""


def session_count(s: Session) -> int:
    return s.message_count


def session_agent_count(s: Session) -> int:
    return s.agent_message_count


def looks_like_generated_name(value: str, sid: str = "") -> bool:
    """Return true for UUID/random-ID titles that are worse than a message snippet."""
    text = re.sub(r"\s+", " ", (value or "").strip())
    if not text:
        return True
    if sid and text == sid:
        return True
    uuid = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    if re.fullmatch(uuid, text):
        return True
    if re.fullmatch(rf"(?:agent[-_])?{uuid}", text):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{24,64}", text):
        return True
    return False


def choose_title(title: str, sid: str, fallback: str) -> str:
    fallback = re.sub(r"\s+", " ", (fallback or "").strip())
    if fallback and looks_like_generated_name(title, sid):
        return fallback
    return title


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


def recent_files(root: Path, pattern: str, max_files: int | None = None) -> list[Path]:
    """Return newest matching files first, optionally capped after cheap stat sorting."""
    files = [p for p in root.glob(pattern) if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if max_files is not None:
        return files[:max(0, max_files)]
    return files


def claude_sessions(max_files: int | None = None, full_scan: bool = True) -> list[Session]:
    out: list[Session] = []
    root = HOME / ".claude/projects"
    if not root.exists():
        return out
    for p in recent_files(root, "*/*.jsonl", max_files):
        sid = p.stem
        cwd = ""
        title = ""
        last_message = ""
        updated = p.stat().st_mtime
        message_count = 0
        agent_message_count = 0
        for msg in load_jsonl(p, 1000):
            updated = max(updated, parse_ts(msg.get("timestamp")))
            cwd = cwd or msg.get("cwd") or (msg.get("attachment") or {}).get("cwd") or ""
            if msg.get("type") in {"queue-operation", "user"}:
                candidate = first_text(msg.get("content") or msg)
                message_count += 1
                if candidate:
                    last_message = candidate
                if not title:
                    title = candidate
            elif msg.get("type") == "assistant":
                candidate = first_text(msg.get("content") or msg.get("message") or msg)
                if candidate:
                    agent_message_count += 1
                    last_message = candidate
        if not cwd:
            cwd = decode_claude_slug(p.parent.name)
        title = choose_title(title, sid, last_message)
        out.append(Session("claude", sid, cwd, updated, title, str(p), message_count, agent_message_count=agent_message_count))
    return out


def codex_sessions(max_files: int | None = None, full_scan: bool = True) -> list[Session]:
    out: list[Session] = []
    root = HOME / ".codex/sessions"
    if not root.exists():
        return out

    titles: dict[str, str] = {}
    idx = HOME / ".codex/session_index.jsonl"
    if idx.exists():
        for row in load_jsonl(idx, 100000):
            sid = row.get("id") or row.get("session_id") or row.get("thread_id")
            title = row.get("thread_name") or row.get("title") or row.get("name") or ""
            # session_index is append-like; never erase a useful title with a blank row.
            if sid and title and not is_synthetic_text(title) and not looks_like_generated_name(title, sid):
                titles[sid] = title

    max_lines = 100000 if full_scan else 300

    for p in recent_files(root, "**/*.jsonl", max_files):
        sid = ""
        cwd = ""
        title = ""
        last_message = ""
        updated = p.stat().st_mtime
        user_seen: set[str] = set()
        message_count = 0
        agent_message_count = 0
        schema = "unknown"
        title_source = ""
        warnings: list[str] = []
        for msg in load_jsonl(p, max_lines):
            updated = max(updated, parse_ts(msg.get("timestamp")))
            typ = msg.get("type")
            if typ == "session_meta":
                schema = "current"
                payload = msg.get("payload") or {}
                sid = sid or payload.get("id") or payload.get("session_id") or ""
                cwd = cwd or payload.get("cwd") or ""
                continue

            role = ""
            candidate = ""
            source = ""
            synthetic = False

            if typ == "response_item":
                schema = "current"
                payload = msg.get("payload") or {}
                if payload.get("type") in {"message", None}:
                    role = payload.get("role") or ""
                    candidate_raw = raw_text(payload.get("content"))
                    candidate = first_text(payload.get("content"))
                    synthetic = role == "user" and is_synthetic_text(candidate_raw)
                    if synthetic:
                        cwd = cwd or extract_cwd_from_text(candidate_raw)
                    source = "response_item"
            elif typ == "event_msg":
                schema = "current"
                payload = msg.get("payload") or {}
                ptype = payload.get("type")
                if ptype == "user_message":
                    role = "user"
                    candidate = first_text(payload.get("message") or payload.get("text_elements") or payload)
                    synthetic = is_synthetic_text(raw_text(payload.get("message") or payload.get("text_elements") or payload))
                    source = "event_msg.user_message"
                elif ptype in {"agent_message", "assistant_message"}:
                    role = "assistant"
                    candidate = first_text(payload.get("message") or payload)
                    source = f"event_msg.{ptype}"
            elif typ == "message" and msg.get("role") in {"user", "assistant"}:
                schema = "legacy"
                role = msg.get("role") or ""
                candidate = first_text(msg.get("content") or msg)
                source = "legacy.message"

            if role not in {"user", "assistant"}:
                continue
            if candidate and not synthetic:
                last_message = candidate
            if role == "user" and not synthetic:
                key = re.sub(r"\s+", " ", candidate).strip().lower()
                if key and key not in user_seen:
                    user_seen.add(key)
                    message_count += 1
                if candidate and not title and not looks_like_generated_name(candidate, sid):
                    title = candidate
                    title_source = source
            elif role == "assistant" and candidate:
                agent_message_count += 1
                last_message = candidate

        sid = sid or codex_sid_from_filename(p)
        if titles.get(sid):
            title = titles[sid]
            title_source = "session_index.thread_name"
        title = choose_title(title, sid, last_message)
        out.append(Session("codex", sid, cwd, updated, title, str(p), message_count, (), tuple(warnings), title_source, "session_meta" if cwd else "", "jsonl_or_mtime", schema, full_scan, sid, agent_message_count))
    return out


def cursor_sessions(max_files: int | None = None, full_scan: bool = True) -> list[Session]:
    out: list[Session] = []
    root = HOME / ".cursor/projects"
    if not root.exists():
        return out
    for p in recent_files(root, "*/agent-transcripts/*/*.jsonl", max_files):
        sid = p.stem
        project_slug = p.relative_to(root).parts[0]
        # Cursor project directory names are like Users-qm4-Projects-repo.
        cwd = "/" + project_slug.replace("-", "/")
        title = ""
        last_message = ""
        updated = p.stat().st_mtime
        message_count = 0
        agent_message_count = 0
        for msg in load_jsonl(p, 1000):
            role = msg.get("role")
            if role in {"user", "assistant"}:
                candidate = first_text(msg.get("message") or msg.get("content") or msg)
                # Cursor transcript dumps may prepend synthetic system/context messages.
                synthetic = candidate.lower().startswith(("[system]", "# soul.md", "<timestamp>")) if candidate else False
                if role == "user":
                    message_count += 1
                elif role == "assistant" and candidate and not synthetic:
                    agent_message_count += 1
                if candidate and not synthetic:
                    last_message = candidate
                    if role == "user" and not title:
                        title = candidate
        title = choose_title(title, sid, last_message)
        out.append(Session("cursor", sid, cwd, updated, title, str(p), message_count, agent_message_count=agent_message_count))
    return out



def pi_sessions(max_files: int | None = None, full_scan: bool = True) -> list[Session]:
    out: list[Session] = []
    root = HOME / ".pi/agent/sessions"
    if not root.exists():
        return out
    for p in recent_files(root, "*/*.jsonl", max_files):
        sid = ""
        cwd = ""
        title = ""
        last_message = ""
        updated = p.stat().st_mtime
        message_count = 0
        agent_message_count = 0
        for msg in load_jsonl(p, 1000):
            updated = max(updated, parse_ts(msg.get("timestamp")))
            if msg.get("type") == "session":
                sid = sid or msg.get("id") or ""
                cwd = cwd or msg.get("cwd") or ""
            if msg.get("type") == "message":
                m = msg.get("message") or {}
                role = m.get("role")
                if role in {"user", "assistant"}:
                    candidate = first_text(m.get("content"))
                    if candidate:
                        last_message = candidate
                    if role == "user":
                        message_count += 1
                        if not title:
                            title = candidate
                    elif role == "assistant" and candidate:
                        agent_message_count += 1
        sid = sid or p.stem.split("_")[-1]
        if not cwd:
            cwd = decode_claude_slug(p.parent.name)
        title = choose_title(title, sid, last_message)
        out.append(Session("pi", sid, cwd, updated, title, str(p), message_count, agent_message_count=agent_message_count))
    return out


def hermes_sessions(max_files: int | None = None, full_scan: bool = True) -> list[Session]:
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
    if not full_scan and meta_by_id:
        rows = sorted(meta_by_id.items(), key=lambda item: parse_ts(item[1].get("updated_at")), reverse=True)
        if max_files is not None:
            rows = rows[:max_files]
        for sid, meta in rows:
            platform = meta.get("platform") or "hermes"
            origin = meta.get("origin") or {}
            cwd = f"hermes:{platform}" + (f":{origin.get('chat_type')}" if origin.get("chat_type") else "")
            path = str(root / f"session_{sid}.json")
            out.append(Session(
                "hermes",
                sid,
                cwd,
                parse_ts(meta.get("updated_at") or meta.get("created_at")),
                meta.get("display_name") or "",
                path,
                1,
                count_exact=False,
                agent_message_count=1,
            ))
        return out

    for p in recent_files(root, "session_*.json", max_files):
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
        message_count = 0
        agent_message_count = 0
        last_message = ""
        for m in messages:
            if isinstance(m, dict) and m.get("role") in {"user", "assistant"}:
                candidate = first_text(m.get("content"))
                if candidate:
                    last_message = candidate
                if m.get("role") == "user":
                    message_count += 1
                    if not title:
                        title = candidate
                elif m.get("role") == "assistant" and candidate:
                    agent_message_count += 1
        title = choose_title(title, sid, last_message)
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
        out.append(Session("hermes", sid, cwd, updated, title, str(p), message_count, agent_message_count=agent_message_count))
    # Some active sessions may be in sessions.json before a session_*.json is visible.
    for sid, meta in meta_by_id.items():
        if any(s.sid == sid for s in out):
            continue
        platform = meta.get("platform") or "hermes"
        origin = meta.get("origin") or {}
        cwd = f"hermes:{platform}" + (f":{origin.get('chat_type')}" if origin.get("chat_type") else "")
        out.append(Session("hermes", sid, cwd, parse_ts(meta.get("updated_at")), meta.get("display_name") or "", str(idx), 1, count_exact=False, agent_message_count=1))
    return out

def openclaw_sessions(max_files: int | None = None, full_scan: bool = True) -> list[Session]:
    """Read OpenClaw session indexes without parsing large transcript files."""
    out: list[Session] = []
    root = HOME / ".openclaw" / "agents"
    if not root.exists():
        return out
    stores = recent_files(root, "*/sessions/sessions.json", max_files)
    for idx in stores:
        try:
            data = json.loads(idx.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        parts = idx.relative_to(root).parts
        agent_id = parts[0] if parts else "main"
        for key, row in data.items():
            if not isinstance(row, dict):
                continue
            sid = row.get("sessionId") or row.get("session_id") or ""
            session_key = str(key)
            if not sid and not session_key:
                continue
            label = row.get("label") or row.get("title") or row.get("displayName") or ""
            chat_type = row.get("chatType") or ""
            title = label or session_key
            session_file = row.get("sessionFile") or row.get("session_file") or ""
            cwd = f"openclaw:{agent_id}"
            if chat_type:
                cwd += f":{chat_type}"
            updated = parse_ts(row.get("updatedAt") or row.get("updated_at") or row.get("sessionStartedAt") or row.get("createdAt"))
            if not updated and session_file:
                try:
                    updated = Path(session_file).stat().st_mtime
                except OSError:
                    pass
            path = session_file or str(idx)
            out.append(Session("openclaw", sid or session_key, cwd, updated, title, path, 1, resume_id=session_key, agent_message_count=1))
    return sorted(out, key=lambda s: s.updated, reverse=True)


def opencode_sessions(max_files: int | None = None, full_scan: bool = True, limit: int = 100) -> list[Session]:
    # Prefer the official CLI when explicitly requested; it already knows its DB paths.
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
        raw_count = row.get("messageCount") or row.get("message_count") or row.get("messages") or 2
        message_count = len(raw_count) if isinstance(raw_count, list) else int(raw_count or 2)
        if sid:
            out.append(Session("opencode", sid, cwd, updated, title, "opencode session list", message_count, agent_message_count=message_count))
    return out


def shutil_which(name: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / name
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def call_scanner(fn, max_files: int | None = None, full_scan: bool = True) -> list[Session]:
    """Call scanner with new fast-scan args while keeping tests/old adapters simple."""
    try:
        return fn(max_files=max_files, full_scan=full_scan)
    except TypeError:
        return fn()


def collect(
    include_hermes: bool = True,
    include_short_sessions: bool = False,
    include_one_message: bool = False,
    min_user_messages: int = DEFAULT_MIN_USER_MESSAGES,
    min_agent_messages: int = DEFAULT_MIN_AGENT_MESSAGES,
    max_files_per_agent: int | None = None,
    full_scan: bool = True,
    include_opencode: bool = False,
    include_openclaw: bool = True,
) -> list[Session]:
    if include_short_sessions or include_one_message:
        min_user_messages = 0
        min_agent_messages = 0
    sessions = []
    fns = [claude_sessions, codex_sessions, cursor_sessions, pi_sessions]
    if include_hermes:
        fns.append(hermes_sessions)
    if include_openclaw:
        fns.append(openclaw_sessions)
    if include_opencode:
        fns.append(opencode_sessions)
    for fn in fns:
        try:
            sessions.extend(call_scanner(fn, max_files=max_files_per_agent, full_scan=full_scan))
        except Exception as e:
            print(f"warn: {fn.__name__}: {e}", file=sys.stderr)
    # Deduplicate by agent+id, keeping newest path parse. Fall back to path for missing ids.
    by_key: dict[tuple[str, str], Session] = {}
    for s in sessions:
        hidden = []
        if s.message_count < min_user_messages:
            hidden.append(f"user<{min_user_messages}")
        if s.agent_message_count < min_agent_messages:
            hidden.append(f"agent<{min_agent_messages}")
        if hidden:
            s.hidden_reasons = tuple(hidden)
            continue
        key = (s.agent, s.sid or s.path)
        if key not in by_key or s.updated > by_key[key].updated:
            by_key[key] = s
    return sorted(by_key.values(), key=lambda s: s.updated, reverse=True)


def collect_all(include_hermes: bool = True, include_opencode: bool = True, include_openclaw: bool = True) -> list[Session]:
    return collect(include_hermes=include_hermes, include_short_sessions=True, min_user_messages=0, min_agent_messages=0, full_scan=True, include_opencode=include_opencode, include_openclaw=include_openclaw)


def store_stats(include_hermes: bool = True, min_user_messages: int = DEFAULT_MIN_USER_MESSAGES, min_agent_messages: int = DEFAULT_MIN_AGENT_MESSAGES) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    fns = {
        "claude": claude_sessions,
        "codex": codex_sessions,
        "cursor": cursor_sessions,
        "pi": pi_sessions,
        "opencode": opencode_sessions,
        "openclaw": openclaw_sessions,
    }
    if include_hermes:
        fns["hermes"] = hermes_sessions
    for agent, fn in fns.items():
        try:
            rows = fn()
        except Exception:
            rows = []
        stats[agent] = {
            "parsed": len(rows),
            "visible": sum(1 for s in rows if s.message_count >= min_user_messages and s.agent_message_count >= min_agent_messages),
            "hidden_user": sum(1 for s in rows if s.message_count < min_user_messages),
            "hidden_agent": sum(1 for s in rows if s.agent_message_count < min_agent_messages),
            "hidden_short": sum(1 for s in rows if s.message_count < min_user_messages or s.agent_message_count < min_agent_messages),
            "newest": int(max((s.updated for s in rows), default=0)),
        }
    return stats


def print_doctor(agent: str | None = None, min_user_messages: int = DEFAULT_MIN_USER_MESSAGES, min_agent_messages: int = DEFAULT_MIN_AGENT_MESSAGES, as_json: bool = False) -> int:
    stats = store_stats(include_hermes=True, min_user_messages=min_user_messages, min_agent_messages=min_agent_messages)
    if agent:
        stats = {agent: stats.get(agent, {"parsed": 0, "visible": 0, "hidden_short": 0, "newest": 0})}
    if as_json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0
    print(f"agent-session-resume {VERSION}")
    for name, st in stats.items():
        newest = datetime.fromtimestamp(st["newest"], timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M") if st["newest"] else "unknown"
        print(f"{name:<8} parsed={st['parsed']:<5} visible={st['visible']:<5} hidden_short={st['hidden_short']:<5} hidden_user={st['hidden_user']:<5} hidden_agent={st['hidden_agent']:<5} newest={newest}")
    print(f"filter: min_user_messages={min_user_messages}, min_agent_messages={min_agent_messages}; use --include-short-sessions to show everything")
    return 0


def print_why(term: str, include_hermes: bool = True, min_user_messages: int = DEFAULT_MIN_USER_MESSAGES, min_agent_messages: int = DEFAULT_MIN_AGENT_MESSAGES) -> int:
    rows = collect(include_hermes=include_hermes, include_short_sessions=True, min_user_messages=0, min_agent_messages=0)
    q = term.lower()
    matches = [s for s in rows if q in " ".join([s.agent, s.sid, s.cwd, s.title, s.path]).lower()]
    if not matches:
        print(f"No matching session for {term!r}")
        return 1
    for s in matches[:20]:
        hidden = []
        if s.message_count < min_user_messages:
            hidden.append(f"user<{min_user_messages}")
        if s.agent_message_count < min_agent_messages:
            hidden.append(f"agent<{min_agent_messages}")
        print(f"{s.agent} {s.sid}")
        print(f"  title: {s.title or '(none)'}")
        print(f"  cwd: {s.cwd or '(none)'}")
        print(f"  path: {s.path}")
        print(f"  user_messages: {s.message_count}")
        print(f"  agent_messages: {s.agent_message_count}")
        print(f"  schema: {s.schema or 'unknown'}")
        print(f"  hidden_by_current_filter: {', '.join(hidden) if hidden else 'no'}")
        try:
            print("  command: " + " ".join(shlex.quote(x) for x in resume_command(s)))
        except Exception as e:
            print(f"  command_error: {e}")
    return 0


def resume_command(s: Session) -> list[str]:
    if s.agent == "claude":
        return ["bash", "-lc", f"cd {shlex.quote(s.cwd)} && exec claude --resume {shlex.quote(s.sid)}"]
    if s.agent == "codex":
        cmd = ["codex", "resume", "--all"]
        if s.cwd:
            cmd.extend(["-C", s.cwd])
        cmd.append(s.resume_id or s.sid)
        return cmd
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
    if s.agent == "openclaw":
        return ["openclaw", "tui", "--session", s.resume_id or s.sid]
    raise SystemExit(f"No resume command for {s.agent}")


def display_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
    return width


def truncate_display(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    ellipsis = "…"
    target = max(0, max_width - display_width(ellipsis))
    out = []
    used = 0
    for ch in text:
        ch_w = 0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
        if used + ch_w > target:
            break
        out.append(ch)
        used += ch_w
    return "".join(out) + ellipsis


def pad_display(text: str, width: int, align: str = "<") -> str:
    text = truncate_display(text, width)
    pad = max(0, width - display_width(text))
    if align == ">":
        return " " * pad + text
    return text + " " * pad


def compact_title(s: Session, max_len: int = 42) -> str:
    title = re.sub(r"\s+", " ", (s.title or "").strip())
    if not title:
        title = s.sid
    return truncate_display(title, max_len)


def compact_folder(s: Session, max_len: int = 34) -> str:
    cwd = s.cwd or "?"
    cwd = cwd.replace(str(HOME), "~")
    if cwd.startswith("hermes:"):
        folder = cwd
    else:
        folder = cwd.rstrip("/") or "/"
    return truncate_display(folder, max_len) if display_width(folder) > max_len else folder


def compact_when(s: Session) -> str:
    if not s.updated:
        return "unknown"
    return datetime.fromtimestamp(s.updated, timezone.utc).astimezone().strftime("%m-%d %H:%M")


def row_layout(width: int = 120) -> tuple[int, int]:
    name_w = max(18, min(44, width - 65))
    folder_w = max(16, min(40, width - name_w - 39))
    return name_w, folder_w


def header_text(width: int = 120, include_index: bool = False) -> str:
    name_w, folder_w = row_layout(width)
    prefix = f"{'#':>3}  " if include_index else ""
    return f"{prefix}{pad_display('name', name_w)}  {'agent':<8}  {pad_display('folder', folder_w)}  {'u/a':>7}  modified"


def row_text(s: Session, width: int = 120) -> str:
    name_w, folder_w = row_layout(width)
    name = pad_display(compact_title(s, name_w), name_w)
    folder = pad_display(compact_folder(s, folder_w), folder_w)
    user_count = f"{s.message_count}+" if not s.count_exact else str(s.message_count)
    agent_count = f"{s.agent_message_count}+" if not s.count_exact else str(s.agent_message_count)
    msg_count = f"{user_count}/{agent_count}"
    return f"{name}  {s.agent:<8}  {folder}  {msg_count:>7}  {compact_when(s)}"


def render(rows: list[Session]) -> None:
    print(header_text(120, include_index=True))
    print("-" * display_width(header_text(120, include_index=True)))
    for i, s in enumerate(rows, 1):
        print(f"{i:>3}  {row_text(s, 120)}")


def merge_sessions(rows: list[Session], extra: list[Session]) -> list[Session]:
    by_key: dict[tuple[str, str], Session] = {(s.agent, s.sid or s.path): s for s in rows}
    for s in extra:
        key = (s.agent, s.sid or s.path)
        if key not in by_key or s.updated > by_key[key].updated:
            by_key[key] = s
    return sorted(by_key.values(), key=lambda s: s.updated, reverse=True)


def run_tui(rows: list[Session], initial_limit: int = 40, load_batch: int = 200, lazy_opencode: bool = False, scan_budget: int | None = None) -> int:
    if not rows:
        print("No sessions found")
        return 1
    import curses

    selected = 0
    top = 0
    loaded = min(len(rows), max(1, initial_limit))
    opencode_state = "loading" if lazy_opencode else ""

    def load_opencode() -> None:
        nonlocal rows, loaded, opencode_state
        try:
            extra = opencode_sessions(max_files=scan_budget, full_scan=False)
            if extra:
                rows = merge_sessions(rows, extra)
                loaded = min(max(loaded, initial_limit), len(rows))
            opencode_state = f"opencode +{len(extra)}" if extra else "opencode 0"
        except Exception:
            opencode_state = "opencode failed"

    if lazy_opencode:
        threading.Thread(target=load_opencode, daemon=True).start()

    def load_more(min_needed: int = 1) -> None:
        nonlocal loaded
        if loaded < len(rows) and min_needed >= loaded - 5:
            loaded = min(len(rows), loaded + max(1, load_batch))

    def draw(stdscr):
        nonlocal selected, top, loaded
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(250 if lazy_opencode else -1)
        while True:
            load_more(selected)
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            more = "" if loaded >= len(rows) else f"  {loaded}/{len(rows)} loaded"
            lazy = f"  {opencode_state}" if opencode_state else ""
            header = f"resume  ↑/↓ select  Enter resume  q quit{more}{lazy}"
            stdscr.addnstr(0, 0, header, w - 1, curses.A_BOLD)
            stdscr.addnstr(1, 0, header_text(w), w - 1, curses.A_DIM)
            visible = max(1, h - 3)
            if selected < top:
                top = selected
            if selected >= top + visible:
                top = selected - visible + 1
            for screen_i, row_i in enumerate(range(top, min(loaded, top + visible)), start=2):
                line = row_text(rows[row_i], w)
                attr = curses.A_REVERSE if row_i == selected else curses.A_NORMAL
                stdscr.addnstr(screen_i, 0, line, w - 1, attr)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch == -1:
                if opencode_state and not opencode_state.startswith("loading"):
                    stdscr.timeout(-1)
                continue
            if ch in (ord('q'), 27):
                return None
            if ch in (curses.KEY_UP, ord('k')):
                selected = max(0, selected - 1)
            elif ch in (curses.KEY_DOWN, ord('j')):
                selected = min(loaded - 1, selected + 1)
                load_more(selected)
            elif ch in (curses.KEY_NPAGE,):
                selected = min(loaded - 1, selected + visible)
                load_more(selected)
            elif ch in (curses.KEY_END, ord('G')):
                loaded = len(rows)
                selected = len(rows) - 1
            elif ch in (curses.KEY_PPAGE,):
                selected = max(0, selected - visible)
            elif ch in (10, 13, curses.KEY_ENTER):
                return selected

    try:
        idx = curses.wrapper(draw)
    except KeyboardInterrupt:
        return 130
    if idx is None:
        return 0
    cmd = resume_command(rows[idx])
    os.execvp(cmd[0], cmd)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="List and resume recent coding-agent sessions across agents and folders.")
    ap.add_argument("query", nargs="*", help="case-insensitive filter across agent, cwd, title, session id; subcommands: doctor, why <term>")
    ap.add_argument("-n", "--limit", type=int, default=40)
    ap.add_argument("--version", action="store_true", help="print version and exit")
    ap.add_argument("--min-user-messages", type=int, default=DEFAULT_MIN_USER_MESSAGES, help="minimum real user messages to show; default 1")
    ap.add_argument("--min-agent-messages", type=int, default=DEFAULT_MIN_AGENT_MESSAGES, help="minimum real agent/assistant messages to show; default 1")
    ap.add_argument("--agent", choices=["claude", "codex", "cursor", "pi", "hermes", "openclaw", "opencode"])
    ap.add_argument("--all", action="store_true", help="scan every session file; slower but complete")
    ap.add_argument("--include-hermes", action="store_true", help="compatibility no-op; Hermes is included by default")
    ap.add_argument("--no-hermes", action="store_true", help="exclude Hermes sessions from the default all-agent list")
    ap.add_argument("--include-openclaw", action="store_true", help="compatibility no-op; OpenClaw is included by default when its store exists")
    ap.add_argument("--no-openclaw", action="store_true", help="exclude OpenClaw sessions from the default all-agent list")
    ap.add_argument("--include-opencode", action="store_true", help="include OpenCode immediately via its CLI; default TUI loads it lazily after first draw")
    ap.add_argument("--include-short-sessions", action="store_true", help="include sessions below --min-user-messages or --min-agent-messages")
    ap.add_argument("--include-one-message", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--exec", dest="exec_index", type=int, help="resume the numbered row from the filtered list")
    ap.add_argument("--print-cmd", type=int, metavar="N", help="print resume command for row N instead of executing")
    ap.add_argument("--json", action="store_true", help="print machine-readable sessions")
    ap.add_argument("--tui", action="store_true", help="force interactive arrow-key picker")
    ap.add_argument("--no-tui", action="store_true", help="print concise list instead of opening the picker")
    args = ap.parse_args()

    if args.version:
        print(VERSION)
        return 0
    if args.query and args.query[0] == "doctor":
        return print_doctor(agent=args.agent, min_user_messages=args.min_user_messages, min_agent_messages=args.min_agent_messages, as_json=args.json)
    if args.query and args.query[0] == "why":
        if len(args.query) < 2:
            raise SystemExit("usage: resume why <id|path|query>")
        return print_why(" ".join(args.query[1:]), include_hermes=True, min_user_messages=args.min_user_messages, min_agent_messages=args.min_agent_messages)

    include_hermes = (not args.no_hermes) or args.agent == "hermes"
    include_openclaw = (not args.no_openclaw) or args.agent == "openclaw"
    want_tui = args.tui or (sys.stdin.isatty() and sys.stdout.isatty() and not args.no_tui)
    lazy_opencode = want_tui and not args.include_opencode and args.agent is None
    include_opencode = args.include_opencode or args.agent == "opencode"
    full_scan = args.all or bool(args.query)
    scan_budget = None if full_scan else max(DEFAULT_SCAN_BUDGET, args.limit + 40)
    rows = collect(
        include_hermes=include_hermes,
        include_short_sessions=args.include_short_sessions,
        include_one_message=args.include_one_message,
        min_user_messages=args.min_user_messages,
        min_agent_messages=args.min_agent_messages,
        max_files_per_agent=scan_budget,
        full_scan=full_scan,
        include_opencode=include_opencode,
        include_openclaw=include_openclaw,
    )
    if args.agent:
        rows = [s for s in rows if s.agent == args.agent]
    if args.query:
        q = " ".join(args.query).lower()
        rows = [s for s in rows if q in " ".join([s.agent, s.sid, s.cwd, s.title, s.path]).lower()]
    limit = max(1, args.limit)

    if args.json:
        print(json.dumps([s.__dict__ for s in rows[:limit]], indent=2, ensure_ascii=False))
        return 0

    if args.print_cmd or args.exec_index:
        idx = (args.print_cmd or args.exec_index) - 1
        limited_rows = rows[:limit]
        if idx < 0 or idx >= len(limited_rows):
            raise SystemExit(f"index out of range: {idx+1}; filtered rows={len(limited_rows)}")
        cmd = resume_command(limited_rows[idx])
        if args.print_cmd:
            print(" ".join(shlex.quote(x) for x in cmd))
            return 0
        os.execvp(cmd[0], cmd)

    if want_tui:
        return run_tui(rows, initial_limit=limit, lazy_opencode=lazy_opencode, scan_budget=scan_budget)

    render(rows[:limit])
    print(f"\nResume: resume [filter...] --exec N  |  TUI: run in a terminal  |  Full scan: --all  |  Filter: --min-user-messages {args.min_user_messages} --min-agent-messages {args.min_agent_messages}  |  Doctor: resume doctor")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
