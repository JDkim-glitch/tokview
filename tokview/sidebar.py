"""Sidebar — session list with active marker and click-to-switch.

Emits ListView.Selected events whose `item.session_id` carries the chosen
session ID; TokviewApp listens and switches the active session.
"""

from __future__ import annotations

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
        height: 6;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Sessions", id="sidebar-title")
        yield ListView(id="session-list")
        yield Static(
            "F2 new\nF3 close\nF4/⇧→ next\nF5/⇧← prev\nF6 panel\nF7 reset",
            id="sidebar-hint",
        )

    def update_sessions(
        self,
        sessions: list[Session],
        active_id: str | None,
    ) -> None:
        listview = self.query_one("#session-list", ListView)
        listview.clear()
        active_index: int | None = None
        for i, s in enumerate(sessions):
            listview.append(SessionListItem(s, is_active=(s.id == active_id)))
            if s.id == active_id:
                active_index = i
        if active_index is not None:
            listview.index = active_index
