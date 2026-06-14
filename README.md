# agent-session-resume

`resume` is a small terminal UI for finding and resuming recent coding-agent sessions across agents and folders.

Claude Code only shows sessions for the current folder. `resume` indexes local session stores globally so you can pick the session first, then jump back into the right folder and agent.

## Supported agents

Default list:

- Claude Code
- Codex CLI
- Cursor Agent
- Pi
- OpenCode, when `opencode session list --format json` is available

Hidden by default:

- One-message sessions, usually smoke tests. Use `--include-one-message`.
- Hermes sessions. Use `--include-hermes` or `--agent hermes`.

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
resume --include-hermes discord
```

Include one-message smoke-test sessions:

```bash
resume --include-one-message
```

Print a concise non-interactive list:

```bash
resume --no-tui -n 20
```

Resume a numbered row from the filtered list:

```bash
resume --exec 3
```

Print the command instead of executing it:

```bash
resume --print-cmd 3
```

Backward-compatible binary:

```bash
agent-resume
```

## Columns

The picker and list show:

- session name
- agent name
- folder
- last modified date/time as `MM-DD HH:MM`

## Notes

`resume` reads local agent session files. It does not upload session content.
