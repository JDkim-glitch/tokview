"""UsagePanel — renders the active session's usage from UsagePoller snapshots.

Sections (top→bottom):
  - Block: active 5-hour billing window (global) with countdown, quota bar,
    burn rate, projection.
  - Today: that calendar day's totals, scoped to the session's project.
  - Week: the latest week's totals, scoped to the session's project.
  - All time / Since HH:MM: project-scoped cumulative; label switches to
    "Since" when the user has pressed the reset hotkey.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from tokview.usage_poller import UsageSnapshot


BAR_WIDTH = 12


class UsagePanel(Vertical):
    DEFAULT_CSS = """
    UsagePanel {
        background: $panel;
        color: $foreground;
        border-left: solid $primary;
        padding: 0 1;
    }
    UsagePanel > #usage-body {
        background: $panel;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._snapshot: UsageSnapshot | None = None

    def compose(self) -> ComposeResult:
        yield Static(self._build_body(), id="usage-body")

    def update_snapshot(self, snap: UsageSnapshot) -> None:
        self._snapshot = snap
        try:
            body = self.query_one("#usage-body", Static)
        except Exception:
            return
        body.update(self._build_body())

    def _build_body(self) -> Text:
        snap = self._snapshot
        text = Text()
        if snap is None:
            text.append("Usage\n", style="bold cyan")
            text.append("\n(waiting...)", style="dim")
            return text

        agent = snap.agent or "—"
        text.append(f"Usage · {agent}\n", style="bold cyan")
        text.append("─" * (BAR_WIDTH + 8) + "\n", style="dim")

        if (
            snap.error
            and snap.block_end is None
            and snap.today_date is None
            and snap.week_start is None
        ):
            text.append("⚠ ", style="yellow")
            text.append(snap.error + "\n", style="red")
        else:
            self._render_block(text, snap)
            self._render_today(text, snap)
            self._render_week(text, snap)
            self._render_total(text, snap)

        if snap.last_polled:
            text.append(f"\n⟳ {snap.last_polled}", style="dim")
        return text

    def _render_block(self, text: Text, snap: UsageSnapshot) -> None:
        if snap.block_end is None:
            return
        remaining = _fmt_remaining(snap.block_remaining_min)
        text.append("Block · ", style="bold")
        text.append(f"{remaining} left\n", style="yellow")
        text.append("In:    ")
        text.append(f"{snap.block_input:,}\n", style="blue")
        text.append("Out:   ")
        text.append(f"{snap.block_output:,}\n", style="green")
        if snap.block_cache_read:
            text.append("Cache: ")
            text.append(f"{snap.block_cache_read:,}\n", style="magenta")
        text.append("Total: ")
        text.append(f"{snap.block_total_tokens:,}\n", style="white")
        text.append("Cost:  ")
        text.append(f"${snap.block_cost:.2f}\n", style="bold green")

        if snap.block_token_limit:
            _render_bar(text, used=snap.block_total_tokens, limit=snap.block_token_limit)
            text.append("\n")

        if snap.block_burn_per_min:
            text.append("Burn:  ")
            text.append(f"{_fmt_k(snap.block_burn_per_min)}/min\n", style="cyan")
        if snap.block_proj_cost is not None and snap.block_proj_tokens is not None:
            text.append("Proj:  ")
            text.append(
                f"${snap.block_proj_cost:.2f} · {_fmt_k(snap.block_proj_tokens)}\n",
                style="dim",
            )
        text.append("\n")

    def _render_today(self, text: Text, snap: UsageSnapshot) -> None:
        if snap.today_date is None:
            return
        text.append(f"Today {snap.today_date[5:]}\n", style="bold")
        text.append("In:    ")
        text.append(f"{snap.today_input:,}\n", style="blue")
        text.append("Out:   ")
        text.append(f"{snap.today_output:,}\n", style="green")
        text.append("Cost:  ")
        text.append(f"${snap.today_cost:.2f}\n", style="green")
        text.append("\n")

    def _render_week(self, text: Text, snap: UsageSnapshot) -> None:
        show_reset = (
            snap.agent == "claude" and snap.weekly_remaining_min is not None
        )
        if snap.week_start is None and not show_reset:
            return

        if show_reset:
            text.append("Week · ", style="bold")
            text.append(
                f"{_fmt_long_remaining(snap.weekly_remaining_min)} left\n",
                style="yellow",
            )
            text.append("Resets Mon 09:00\n", style="dim")
        else:
            text.append(f"Week {snap.week_start[5:]}\n", style="bold")

        if snap.week_start is not None:
            text.append("In:    ")
            text.append(f"{snap.week_input:,}\n", style="blue")
            text.append("Out:   ")
            text.append(f"{snap.week_output:,}\n", style="green")
            text.append("Cost:  ")
            text.append(f"${snap.week_cost:.2f}\n", style="green")
        elif show_reset:
            text.append("(no usage)\n", style="dim")
        text.append("\n")

    def _render_total(self, text: Text, snap: UsageSnapshot) -> None:
        if (
            snap.total_input == 0
            and snap.total_output == 0
            and snap.total_cost == 0
            and snap.since_reset_at is None
        ):
            return
        if snap.since_reset_at:
            text.append(f"Since {snap.since_reset_at}\n", style="bold yellow")
        else:
            text.append("All time\n", style="bold")
        text.append("In:    ")
        text.append(f"{snap.total_input:,}\n", style="blue")
        text.append("Out:   ")
        text.append(f"{snap.total_output:,}\n", style="green")
        text.append("Cost:  ")
        text.append(f"${snap.total_cost:.2f}\n", style="green")


def _short_project(name: str) -> str:
    """Shorten a project name to fit a narrow panel (~18 chars budget)."""
    parts = [p for p in name.split("-") if p]
    if not parts:
        return name
    if len(parts) >= 2:
        candidate = ".../" + "/".join(parts[-2:])
        if len(candidate) <= 18:
            return candidate
    return parts[-1]


def _fmt_remaining(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    m = max(0, int(minutes))
    h, mm = divmod(m, 60)
    if h:
        return f"{h}h {mm}m"
    return f"{mm}m"


def _fmt_long_remaining(minutes: int | None) -> str:
    """Format a longer span (up to ~1 week) as 'Xd Yh' or 'Yh Zm'."""
    if minutes is None:
        return "?"
    m = max(0, int(minutes))
    days, rem = divmod(m, 1440)
    hours, mm = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mm}m"
    return f"{mm}m"


def _fmt_k(n: float) -> str:
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{int(n)}"


def _render_bar(text: Text, used: int, limit: int) -> None:
    if limit <= 0:
        return
    ratio = used / limit
    pct = min(1.0, ratio)
    filled = int(round(pct * BAR_WIDTH))
    if ratio >= 0.85:
        color = "red"
    elif ratio >= 0.6:
        color = "yellow"
    else:
        color = "green"
    text.append("[", style="dim")
    text.append("█" * filled, style=color)
    text.append("░" * (BAR_WIDTH - filled), style="dim")
    text.append("] ", style="dim")
    pct_label = f"{int(round(ratio * 100))}%"
    text.append(pct_label, style="bold red" if ratio > 1.0 else color)
