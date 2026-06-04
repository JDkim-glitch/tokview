"""StatusBar — global one-line footer with active session and key hints."""

from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        background: $accent;
        color: $foreground;
        height: 1;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            "tokview · F2 new · F3 close · F4/F5 cycle · F6 panel · F7 reset · F12 quit",
            **kwargs,
        )
