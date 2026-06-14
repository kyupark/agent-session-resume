from agent_session_resume.cli import Session, collect, compact_folder, compact_title, compact_when, resume_command


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


def test_collect_hides_one_message_sessions_by_default(monkeypatch):
    import agent_session_resume.cli as cli

    monkeypatch.setattr(cli, "claude_sessions", lambda: [
        Session(agent="claude", sid="one", cwd="/tmp/one", updated=2, message_count=1),
        Session(agent="claude", sid="two", cwd="/tmp/two", updated=1, message_count=2),
    ])
    monkeypatch.setattr(cli, "codex_sessions", lambda: [])
    monkeypatch.setattr(cli, "cursor_sessions", lambda: [])
    monkeypatch.setattr(cli, "pi_sessions", lambda: [])
    monkeypatch.setattr(cli, "opencode_sessions", lambda: [])

    assert [s.sid for s in collect()] == ["two"]
    assert [s.sid for s in collect(include_one_message=True)] == ["one", "two"]
