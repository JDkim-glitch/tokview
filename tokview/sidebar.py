"""Sidebar — session list with active marker + active agent logo at the bottom.

Emits ListView.Selected events whose `item.session_id` carries the chosen
session ID; TokviewApp listens and switches the active session.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, ListItem, ListView, Static

from tokview.session import Session


class SessionListItem(ListItem):
    """ListItem tagged with its session ID."""

    def __init__(self, session: Session, is_active: bool) -> None:
        marker = "▸" if is_active else " "
        if session.status == "exited":
            markup = f"[dim]{marker} {session.title}[/dim] [red]✗[/]"
        else:
            markup = f"{marker} {session.title}"
        super().__init__(Label(markup))
        self.session_id: str = session.id
        if is_active:
            self.add_class("active")
        if session.status == "exited":
            self.add_class("exited")


# (color, lines). Compact block-letter logos themed roughly to each agent's
# brand color. Last line is a dim tagline.
_LOGOS: dict[str, tuple[str, tuple[str, ...]]] = {
    "claude": (
        "#D97757",
        (
            " ▗▆▆▆▆▆▆▆▆▖ ",
            "▂▐█▂████▎█▌▂",
            "▀▜████████▛▀",
            "  ▐▕▌▔▔▐▎▊  ",
            "Claude Code · Opus 4.7",
        ),
    ),
    "hermes": (
        "medium_purple",
        (
            "⠀⠀⣀⣤⣶⣶⠮⠙⢲⣶⠄⠀⠀⠀",
            "⠀⣴⣶⣶⣶⢶⣾⣿⣦⣿⣿⣧⠀⠀",
            "⠀⠈⢹⠂⠈⠃⠀⣿⣿⣿⣿⣿⡆⠀",
            "⢀⠀⣸⣮⣀⣀⢠⡿⢽⣿⣿⣿⢿⣀",
            "⠈⠘⠾⢽⣿⡯⠁⠍⠻⠛⠛⠻⡿⠊",
            "Hermes Agent · LobeHub",
        ),
    ),
    "codex": (
        "spring_green3",
        (
            "╔═╗╔═╗╔╦╗╔═╗═╗ ╦",
            "║  ║ ║ ║║║╣ ╔╩╦╝",
            "╚═╝╚═╝═╩╝╚═╝╩ ╚═",
            "    · OpenAI ·",
        ),
    ),
    "gemini": (
        "deep_sky_blue1",
        (
            "╔═╗╔═╗╔╦╗╦╔╗╔╦",
            "║ ╦║╣ ║║║║║║║║",
            "╚═╝╚═╝╩ ╩╩╝╚╝╩",
            "   · Google ·",
        ),
    ),
    "copilot": (
        "magenta2",
        (
            "╔═╗╔═╗╔═╗╦╦  ╔═╗╔╦╗",
            "║  ║ ║╠═╝║║  ║ ║ ║ ",
            "╚═╝╚═╝╩  ╩╩═╝╚═╝ ╩ ",
            "    · GitHub ·",
        ),
    ),
    "bash": (
        "green",
        (
            "╔╗ ╔═╗╔═╗╦ ╦",
            "╠╩╗╠═╣╚═╗╠═╣",
            "╚═╝╩ ╩╚═╝╩ ╩",
            "  · shell ·",
        ),
    ),
}

_NO_SESSION_HINT = Text.from_markup(
    "[dim]no active session[/]\n[dim]press F2 to start[/]"
)


class Sidebar(Vertical):
    DEFAULT_CSS = """
    Sidebar {
        background: $panel;
        border-right: solid $primary;
    }

    Sidebar > #sidebar-title {
        padding: 0 1;
        background: $boost;
        color: $foreground;
        height: 1;
    }

    Sidebar > ListView {
        height: 1fr;
        background: $panel;
    }

    Sidebar SessionListItem.active {
        background: $accent 30%;
    }

    Sidebar > #sidebar-hint {
        padding: 0 1;
        height: 7;
        color: $text-muted;
    }

    Sidebar > #sidebar-logo {
        padding: 1 1 0 1;
        height: 7;
        content-align: center middle;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Sessions", id="sidebar-title")
        yield ListView(id="session-list")
        yield Static(
            "F2 new\nF3 close\nF4/⇧→ next\nF5/⇧← prev\nF6 panel\nF7 reset\nF12 quit",
            id="sidebar-hint",
        )
        yield Static(_NO_SESSION_HINT, id="sidebar-logo")

    def update_sessions(
        self,
        sessions: list[Session],
        active_id: str | None,
    ) -> None:
        listview = self.query_one("#session-list", ListView)
        listview.clear()
        active_index: int | None = None
        active_agent: str | None = None
        for i, s in enumerate(sessions):
            listview.append(SessionListItem(s, is_active=(s.id == active_id)))
            if s.id == active_id:
                active_index = i
                active_agent = s.agent
        if active_index is not None:
            listview.index = active_index
        self._update_logo(active_agent)

    def _update_logo(self, agent: str | None) -> None:
        try:
            logo = self.query_one("#sidebar-logo", Static)
        except Exception:
            return
        logo.update(self._build_logo(agent))

    @staticmethod
    def _build_logo(agent: str | None) -> Text:
        if agent is None or agent not in _LOGOS:
            return _NO_SESSION_HINT
        color, lines = _LOGOS[agent]
        text = Text()
        for i, line in enumerate(lines):
            style = f"dim {color}" if i == len(lines) - 1 else f"bold {color}"
            text.append(line, style=style)
            if i < len(lines) - 1:
                text.append("\n")
        return text
