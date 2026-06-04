# tokview

A terminal workspace that hosts multiple AI agents (Claude Code, Hermes, ...) in one screen with a live token-usage panel on the side.

```
┌──────────────┬──────────────────────────────┬────────────────────┐
│  Sessions    │     Active agent terminal    │   Usage · claude   │
│              │                              │  ────────────────  │
│ ▸ claude #1  │  ❯ How do I refactor this?  │  Block · 2h left   │
│   hermes #2  │                              │  In:    1,234      │
│              │  (live PTY rendering of      │  Out:   45,678     │
│              │   the agent's TUI)           │  [████░░░░░░] 38%  │
│              │                              │  Burn:  120k/min   │
│ F2 new       │                              │                    │
│ F3 close     │                              │  Today  06-04      │
│ F4/⇧→ next   │                              │  Week · 3d 17h     │
│ F5/⇧← prev   │                              │  Resets Mon 09:00  │
│ F6 panel     │                              │                    │
│ F7 reset     │                              │  All time          │
└──────────────┴──────────────────────────────┴────────────────────┘
 tokview · F2 new · F3 close · F4/F5 cycle · F6 panel · F7 reset · F12 quit
```

## What it does

- Run several AI agent CLIs (Claude Code, Hermes, Codex, Gemini, etc.) inside one terminal app, each in its own tab on the sidebar.
- Watch the active session's token usage update live on the right panel:
  - Active 5-hour billing block (claude only): countdown to reset, tokens, cost, burn rate, projection, and a quota bar against your historical peak.
  - Today's totals, weekly totals with a countdown to next reset (Monday 09:00 local), and all-time totals.
- Switch sessions instantly with keys or sidebar clicks.
- Hide the usage panel when you want the agent fullscreen.
- Reset the "all time" counter to track from now.

## Requirements

- macOS (1차) or Linux. Windows native is not supported.
- Python 3.10 or newer.
- [bun](https://bun.sh) — needed because tokview shells out to `bunx --bun ccusage <agent>` to read usage data.
- [pipx](https://pipx.pypa.io) for installation.
- The agent CLIs you want to host (e.g. `claude`, `hermes`) installed and on your PATH.

## Install

```bash
pipx install git+https://github.com/JDkim-glitch/tokview.git
```

This creates an isolated venv under `~/.local/pipx/venvs/tokview/` and exposes the `tokview` command on your PATH (same shape as `claude` is installed).

To upgrade later:

```bash
pipx upgrade tokview
```

## Use

```bash
tokview
```

You'll see an empty 3-pane layout. Press **F2** to spawn a session — type the agent name (default `claude`) in the modal and Enter.

### Keys

| Key | Action |
|---|---|
| `F2` | New session (prompts for agent name) |
| `F3` | Close current session |
| `F4` / `Shift+→` | Next session |
| `F5` / `Shift+←` | Previous session |
| `F6` | Toggle the usage panel |
| `F7` | Reset the "all time" counter (panel shows "Since HH:MM") |
| `F12` | Quit tokview |

Everything else goes to the active agent — including arrow keys, `Tab`, `Ctrl+C`, etc.

### Sidebar status

- `▸ name` — active session
- `name ✗` — session has exited (the PTY child terminated)

## How it works

- Each session runs the agent inside a `pty.fork()`'d pseudo-terminal. Output goes through [pyte](https://github.com/selectel/pyte) which parses ANSI escapes into a screen buffer; tokview renders that buffer to the middle pane.
- The right panel calls `bunx --bun ccusage <agent> {daily,weekly,blocks} --json` on a 5-second loop. Results are parsed and rendered to a snapshot. The weekly reset countdown is calculated locally (next Monday 09:00 in your timezone).
- Session state is kept in a single `SessionManager`. Switching sessions toggles widget visibility — every session's PTY keeps running in the background.

## Limitations

- The 5-hour block, weekly reset, and quota bar are claude-specific (ccusage exposes those only for Claude Code).
- The "weekly reset" is hardcoded to Monday 09:00 local time, which matches Claude.ai's current behavior but may not reflect your plan's exact reset schedule.
- Cmd+Tab and Cmd+number can never reach a terminal app on macOS — that's why session switching uses F-keys / Shift+arrows.
- Some terminal emulators don't pass certain key combos (e.g. macOS Terminal.app blocks distinct `Ctrl+Tab` by default). iTerm2, Warp, kitty, ghostty all work fine.

## Project layout

```
tokview/
├── tokview/
│   ├── app.py              # TokviewApp (Textual)
│   ├── pty_terminal.py     # PTY widget + key forwarding + pyte rendering
│   ├── session.py          # Session + SessionManager
│   ├── sidebar.py          # Sessions list
│   ├── usage_panel.py      # Right-panel renderer
│   ├── usage_poller.py     # ccusage subprocess + JSON normalization
│   ├── new_session_screen.py
│   ├── status_bar.py
│   └── tokview.tcss        # Textual CSS
├── docs/                   # PRD / IA design docs (Korean)
├── pyproject.toml
└── LICENSE
```

## License

MIT — see [LICENSE](LICENSE).

## Credits

- Heavy lifting on usage data: [ryoppippi/ccusage](https://github.com/ryoppippi/ccusage).
- TUI framework: [Textualize/textual](https://github.com/Textualize/textual).
- Terminal screen buffer: [selectel/pyte](https://github.com/selectel/pyte).
