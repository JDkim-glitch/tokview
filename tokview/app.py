"""TokviewApp — Phase 3: empty-start, multi-session host with usage polling.

The app boots with no sessions; the middle pane shows an EmptyState until the
user opens a session via F2. One PtyTerminalWidget per session lives inside
#terminal-host; switching toggles their `display` so only the active widget is
on screen. SessionManager is the single source of truth for active session.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import ListView, Static

from tokview.new_session_screen import NewSessionScreen
from tokview.pty_terminal import PtyTerminalWidget
from tokview.session import SessionManager
from tokview.sidebar import Sidebar, SessionListItem
from tokview.usage_panel import UsagePanel
from tokview.usage_poller import UsagePoller, UsageSnapshot


class EmptyState(Static):
    DEFAULT_CSS = """
    EmptyState {
        background: $surface;
        color: $text-muted;
        content-align: center middle;
        width: 100%;
        height: 100%;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            "[bold]No active session[/bold]\n\n"
            "Press [bold]F2[/bold] to start a session\n"
            "(claude · hermes · codex · gemini · copilot · bash)",
            **kwargs,
        )


class TokviewApp(App):
    CSS_PATH = Path(__file__).resolve().parent / "tokview.tcss"
    TITLE = "tokview"
    BINDINGS = [
        ("f2", "new_session", "New (F2)"),
        ("f3", "close_session", "Close (F3)"),
        ("f4", "next_session", "Next (F4)"),
        ("f5", "prev_session", "Prev (F5)"),
        ("shift+right", "next_session", "Next (Shift+→)"),
        ("shift+left", "prev_session", "Prev (Shift+←)"),
        ("f6", "toggle_usage", "Toggle Usage (F6)"),
        ("f7", "reset_all_time", "Reset (F7)"),
        ("f12", "quit", "Quit (F12)"),
    ]

    def __init__(self, default_agent: str = "claude") -> None:
        super().__init__()
        self._default_agent = default_agent
        self._manager = SessionManager()
        self._widgets: dict[str, PtyTerminalWidget] = {}
        self._poller: UsagePoller | None = None
        # session_id → (baseline_input, baseline_output, baseline_cost, reset_label)
        self._baselines: dict[str, tuple[int, int, float, str]] = {}
        self._last_snapshot: UsageSnapshot | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield Sidebar()
            with Container(id="terminal-host"):
                yield EmptyState(id="empty-state")
            yield UsagePanel()

    def on_mount(self) -> None:
        self._poller = UsagePoller(
            self._manager,
            on_update=self._on_usage_update,
            interval=5.0,
        )
        self._poller.start()
        self.call_after_refresh(self.action_new_session)

    def _on_usage_update(self, snap: UsageSnapshot) -> None:
        # Remember the most recent raw snapshot so F7 can capture a baseline.
        self._last_snapshot = snap

        sid = self._manager.active_id
        if sid and sid in self._baselines:
            bi, bo, bc, label = self._baselines[sid]
            snap = replace(
                snap,
                total_input=max(0, snap.total_input - bi),
                total_output=max(0, snap.total_output - bo),
                total_cost=max(0.0, snap.total_cost - bc),
                since_reset_at=label,
            )

        try:
            panel = self.query_one(UsagePanel)
        except Exception:
            return
        panel.update_snapshot(snap)

    def action_toggle_usage(self) -> None:
        try:
            panel = self.query_one(UsagePanel)
        except Exception:
            return
        panel.display = not panel.display

    def action_reset_all_time(self) -> None:
        sid = self._manager.active_id
        last = self._last_snapshot
        if sid is None or last is None:
            return
        label = datetime.now().strftime("%H:%M")
        self._baselines[sid] = (
            int(last.total_input),
            int(last.total_output),
            float(last.total_cost),
            label,
        )
        # Refresh panel immediately by re-applying baseline to the cached snapshot.
        self._on_usage_update(last)

    async def on_unmount(self) -> None:
        if self._poller is not None:
            self._poller.stop()

    async def _spawn_session(self, agent: str) -> None:
        session = self._manager.create(agent=agent)
        widget = PtyTerminalWidget(
            command=agent,
            cwd=session.cwd,
            session_id=session.id,
        )
        self._widgets[session.id] = widget
        host = self.query_one("#terminal-host", Container)
        await host.mount(widget)
        self._hide_empty_state()
        self._switch_to(session.id)

    def on_pty_terminal_widget_exited(
        self, event: PtyTerminalWidget.Exited
    ) -> None:
        sid = event.session_id
        if not sid:
            return
        self._manager.mark_exited(sid)
        self._refresh_sidebar()

    def on_pty_terminal_widget_activity(
        self, event: PtyTerminalWidget.Activity
    ) -> None:
        # Poke the poller so usage refreshes the moment the user submits a
        # prompt or the agent finishes a burst, instead of waiting up to the
        # 5s tick. Only fire for the active session — background sessions
        # don't need to interrupt the panel that's not showing them.
        if event.session_id != self._manager.active_id:
            return
        if self._poller is not None:
            self._poller.poke()

    def _hide_empty_state(self) -> None:
        try:
            self.query_one("#empty-state", EmptyState).display = False
        except Exception:
            pass

    def _show_empty_state(self) -> None:
        try:
            self.query_one("#empty-state", EmptyState).display = True
        except Exception:
            pass

    def _switch_to(self, sid: str) -> None:
        if not self._manager.set_active(sid):
            return
        for w_sid, w in self._widgets.items():
            w.display = (w_sid == sid)
        self._refresh_sidebar()
        active_widget = self._widgets.get(sid)
        if active_widget is not None:
            active_widget.focus()
        if self._poller is not None:
            self._poller.poke()

    def _refresh_sidebar(self) -> None:
        sidebar = self.query_one(Sidebar)
        sidebar.update_sessions(self._manager.list(), self._manager.active_id)

    def action_new_session(self) -> None:
        self.push_screen(
            NewSessionScreen(default=self._default_agent),
            self._on_session_chosen,
        )

    def _on_session_chosen(self, agent: str | None) -> None:
        if agent:
            self.run_worker(self._spawn_session(agent=agent), exclusive=False)

    async def action_close_session(self) -> None:
        sid = self._manager.active_id
        if sid is None:
            return
        widget = self._widgets.pop(sid, None)
        if widget is not None:
            await widget.remove()
        self._baselines.pop(sid, None)
        new_active = self._manager.close(sid)
        if new_active is not None:
            self._switch_to(new_active)
        else:
            self._show_empty_state()
            self._refresh_sidebar()
            if self._poller is not None:
                self._poller.poke()

    def action_next_session(self) -> None:
        new = self._manager.next()
        if new is not None:
            self._switch_to(new)

    def action_prev_session(self) -> None:
        new = self._manager.prev()
        if new is not None:
            self._switch_to(new)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, SessionListItem):
            self._switch_to(item.session_id)


def main() -> None:
    default_agent = sys.argv[1] if len(sys.argv) > 1 else "claude"
    TokviewApp(default_agent=default_agent).run()


if __name__ == "__main__":
    main()
