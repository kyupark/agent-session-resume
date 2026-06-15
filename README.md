# agent-session-resume

`resume` is a small terminal UI for finding and resuming recent coding-agent sessions across agents and folders.

Claude Code only shows sessions for the current folder. `resume` indexes local session stores globally so you can pick the session first, then jump back into the right folder and agent.

## Supported agents

Default list:

- Claude Code
- Codex CLI
- Cursor Agent
- Pi
- Hermes
- OpenClaw

Hidden by default:

- Sessions without at least one user message and one agent/assistant message. Use `--include-short-sessions` to include everything, or tune `--min-user-messages` / `--min-agent-messages`.
- OpenCode sessions in non-interactive output. The TUI starts without OpenCode, then loads it in the background after the first draw. Use `--include-opencode` or `--agent opencode` to include it immediately.

## Install

From GitHub:

```bash
uv tool install git+https://github.com/kyupark/agent-session-resume.git
```

or:

```bash
pipx install git+https://github.com/kyupark/agent-session-resume.git
```

Once the PyPI project is published, this will also work:

```bash
uv tool install agent-session-resume
```

## Usage

Open the picker:

```bash
resume
```

Use arrow keys, `j`/`k`, page up/down, and `Enter` to resume. `q` or `Esc` quits.

Filter sessions:

```bash
resume trip-plan
resume --agent codex ss-french
resume --agent pi hermes-agent
resume --agent hermes discord
resume --agent openclaw
resume --include-opencode
```

Filter by user-message count:

```bash
resume --min-user-messages 3
resume --min-agent-messages 0
resume --include-short-sessions
```

Print a concise non-interactive list. Default limit is 40:

```bash
resume --no-tui
resume --no-tui -n 500
resume --no-tui --all
```

The default picker uses a recent-first bounded scan for speed and requires at least one user message plus one agent/assistant message. Hermes and OpenClaw are included by default; OpenCode is loaded after the first TUI screen so it does not block startup. Use `--all` when you need a complete scan of every historical transcript.

Resume a numbered row from the filtered list:

```bash
resume --exec 3
```

Print the command instead of executing it:

```bash
resume --print-cmd 3
```

Diagnose scanner coverage and hidden filters:

```bash
resume doctor
resume doctor --agent codex
resume why <session-id-or-query>
```

Backward-compatible binary:

```bash
agent-resume
```

## Columns

The picker and list show:

- session name, falling back from UUID/random generated names to the latest user/assistant message snippet
- agent name
- folder
- user/agent message counts
- last modified date/time as `MM-DD HH:MM`

Korean/CJK text is measured by terminal display width, not Python string length, so columns stay aligned.

## Notes

`resume` reads local agent session files. It does not upload session content.
