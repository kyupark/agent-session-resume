from agent_session_resume.cli import Session, compact_folder, compact_title, compact_when, resume_command


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
