"""NewSessionScreen — modal prompt asking which agent to spawn.

Pushed by TokviewApp on F2. Dismisses with the chosen agent name (or None on
cancel). The chosen name is used both as the PTY command and as the
`ccusage <agent>` subcommand for the right-pane usage poller.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static


class NewSessionScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    NewSessionScreen {
        align: center middle;
    }
    NewSessionScreen > Vertical {
        background: $panel;
        border: thick $primary;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    NewSessionScreen #title {
        text-style: bold;
        color: $accent;
    }
    NewSessionScreen Input {
        margin-top: 1;
    }
    NewSessionScreen #hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, default: str = "claude") -> None:
        super().__init__()
        self._default = default

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("New session — which agent?", id="title")
            yield Input(
                placeholder=f"agent name (default: {self._default})",
                id="agent-input",
            )
            yield Static(
                "claude · hermes · codex · gemini · copilot · bash\n"
                "Enter to confirm · Esc to cancel",
                id="hint",
            )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip() or self._default
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)
