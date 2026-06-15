from agent_session_resume.cli import Session, choose_title, collect, compact_folder, compact_title, compact_when, display_width, first_text, header_text, pad_display, looks_like_generated_name, resume_command, row_text, codex_sid_from_filename


def test_compact_title_falls_back_to_session_id():
    s = Session(agent="codex", sid="abc123", cwd="/tmp/project", updated=0, title="")
    assert compact_title(s) == "abc123"


def test_compact_folder_home(monkeypatch):
    import agent_session_resume.cli as cli
    monkeypatch.setattr(cli, "HOME", cli.Path("/Users/example"))
    s = Session(agent="claude", sid="abc", cwd="/Users/example/proj", updated=0)
    assert compact_folder(s) == "~/proj"


def test_compact_when_unknown():
    s = Session(agent="pi", sid="abc", cwd="/tmp/project", updated=0)
    assert compact_when(s) == "unknown"


def test_codex_resume_command():
    s = Session(agent="codex", sid="abc", cwd="/tmp/project", updated=0)
    assert resume_command(s) == ["codex", "resume", "--all", "-C", "/tmp/project", "abc"]


def test_codex_resume_command_omits_empty_cwd():
    s = Session(agent="codex", sid="abc", cwd="", updated=0)
    assert resume_command(s) == ["codex", "resume", "--all", "abc"]


def test_codex_sid_from_rollout_filename():
    from pathlib import Path
    p = Path("rollout-2026-06-15T10-00-00-019e245b-687d-7f20-ac51-b8fd4a84d160.jsonl")
    assert codex_sid_from_filename(p) == "019e245b-687d-7f20-ac51-b8fd4a84d160"


def test_collect_shows_real_one_message_sessions_by_default(monkeypatch):
    import agent_session_resume.cli as cli

    monkeypatch.setattr(cli, "claude_sessions", lambda: [
        Session(agent="claude", sid="one", cwd="/tmp/one", updated=3, message_count=1, agent_message_count=1),
        Session(agent="claude", sid="two", cwd="/tmp/two", updated=2, message_count=2, agent_message_count=1),
        Session(agent="claude", sid="three", cwd="/tmp/three", updated=1, message_count=3, agent_message_count=1),
    ])
    monkeypatch.setattr(cli, "codex_sessions", lambda: [])
    monkeypatch.setattr(cli, "cursor_sessions", lambda: [])
    monkeypatch.setattr(cli, "pi_sessions", lambda: [])
    monkeypatch.setattr(cli, "hermes_sessions", lambda: [])
    monkeypatch.setattr(cli, "openclaw_sessions", lambda: [])
    monkeypatch.setattr(cli, "opencode_sessions", lambda: [])

    assert [s.sid for s in collect()] == ["one", "two", "three"]
    assert [s.sid for s in collect(min_user_messages=3)] == ["three"]
    assert [s.sid for s in collect(include_short_sessions=True, min_user_messages=3)] == ["one", "two", "three"]
    assert [s.sid for s in collect(include_one_message=True, min_user_messages=3)] == ["one", "two", "three"]


def test_collect_keeps_opencode_opt_in(monkeypatch):
    import agent_session_resume.cli as cli

    monkeypatch.setattr(cli, "claude_sessions", lambda: [])
    monkeypatch.setattr(cli, "codex_sessions", lambda: [])
    monkeypatch.setattr(cli, "cursor_sessions", lambda: [])
    monkeypatch.setattr(cli, "pi_sessions", lambda: [])
    monkeypatch.setattr(cli, "hermes_sessions", lambda: [])
    monkeypatch.setattr(cli, "openclaw_sessions", lambda: [])
    monkeypatch.setattr(cli, "opencode_sessions", lambda: [
        Session(agent="opencode", sid="open", cwd="/tmp/open", updated=1, message_count=1, agent_message_count=1),
    ])

    assert collect() == []
    assert [s.sid for s in collect(include_opencode=True)] == ["open"]


def test_collect_filters_zero_agent_messages_by_default(monkeypatch):
    import agent_session_resume.cli as cli

    monkeypatch.setattr(cli, "claude_sessions", lambda: [
        Session(agent="claude", sid="no-agent", cwd="/tmp/no-agent", updated=2, message_count=1, agent_message_count=0),
        Session(agent="claude", sid="roundtrip", cwd="/tmp/roundtrip", updated=1, message_count=1, agent_message_count=1),
    ])
    monkeypatch.setattr(cli, "codex_sessions", lambda: [])
    monkeypatch.setattr(cli, "cursor_sessions", lambda: [])
    monkeypatch.setattr(cli, "pi_sessions", lambda: [])
    monkeypatch.setattr(cli, "hermes_sessions", lambda: [])
    monkeypatch.setattr(cli, "openclaw_sessions", lambda: [])
    monkeypatch.setattr(cli, "opencode_sessions", lambda: [])

    assert [s.sid for s in collect()] == ["roundtrip"]
    assert [s.sid for s in collect(min_agent_messages=0)] == ["no-agent", "roundtrip"]
    assert [s.sid for s in collect(include_short_sessions=True)] == ["no-agent", "roundtrip"]


def test_openclaw_resume_command_uses_session_key():
    s = Session(agent="openclaw", sid="uuid", cwd="openclaw:main:direct", updated=0, resume_id="agent:main:main")
    assert resume_command(s) == ["openclaw", "tui", "--session", "agent:main:main"]


def test_generated_names_fall_back_to_last_message():
    uuid_title = "1560afec-a028-41c0-bf4b-01382d6a29c4"
    assert looks_like_generated_name(uuid_title)
    assert looks_like_generated_name(f"agent-{uuid_title}")
    assert choose_title(uuid_title, "different-session-id", "Implemented the resume picker") == "Implemented the resume picker"
    assert choose_title("Human title", "different-session-id", "Implemented the resume picker") == "Human title"


def test_first_text_strips_cursor_timestamp_wrappers():
    wrapped = {
        "content": [{"type": "text", "text": "<timestamp>Sunday</timestamp>\n<user_query>\nReply exactly: ok\n</user_query>"}]
    }
    assert first_text(wrapped) == "Reply exactly: ok"


def test_display_padding_handles_korean_width():
    korean = "한국어세션"
    assert display_width(korean) == 10
    padded = pad_display(korean, 12)
    assert display_width(padded) == 12
    assert padded.endswith("  ")


def test_compact_title_truncates_by_display_width():
    s = Session(agent="claude", sid="abc", cwd="/tmp", updated=0, title="한국어세션이름")
    assert display_width(compact_title(s, 8)) <= 8


def test_header_matches_row_column_positions():
    s = Session(agent="claude", sid="abc", cwd="/tmp/한글프로젝트", updated=0, title="한국어 세션 이름", message_count=3, agent_message_count=2)
    header = header_text(80)
    row = row_text(s, 80)
    assert display_width(header[:header.index("agent")]) == display_width(row[:row.index("claude")])
    assert display_width(header[:header.index("folder")]) == display_width(row[:row.index("/tmp")])
    assert display_width(header[:header.index("u/a")]) == display_width(row[:row.index("3/2")])
