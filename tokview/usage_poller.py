"""UsagePoller — polls ccusage daily/weekly/blocks for the active session.

Three subcommands per cycle (when an agent is active):
  1. `daily   --json -p <project>`  — Today + project-scoped totals.
  2. `weekly  --json -p <project>`  — last week's bucket for the project.
  3. `blocks  --json`               — global active 5-hour block + history.

Results merge into a single UsageSnapshot.

`since_reset_at` is filled in by the app (not the poller) when the user has
pressed the "reset all-time" hotkey; it lets the panel show "Since HH:MM"
instead of "All time".
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from tokview.session import Session, SessionManager


# Claude's weekly quota reset is documented as Monday 09:00 local time.
WEEKLY_RESET_WEEKDAY = 0  # Monday (0=Mon, 6=Sun per datetime.weekday())
WEEKLY_RESET_HOUR = 9


def next_weekly_reset(now: datetime | None = None) -> datetime:
    """Return the next Monday 09:00 local-time datetime after `now`."""
    if now is None:
        now = datetime.now()
    days_ahead = (WEEKLY_RESET_WEEKDAY - now.weekday()) % 7
    candidate = now.replace(
        hour=WEEKLY_RESET_HOUR, minute=0, second=0, microsecond=0
    ) + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


@dataclass
class UsageSnapshot:
    agent: str | None = None
    cwd: str | None = None
    project_name: str | None = None

    # Active 5-hour block (global, NOT project-filtered)
    block_start: str | None = None
    block_end: str | None = None
    block_remaining_min: int | None = None
    block_input: int = 0
    block_output: int = 0
    block_cache_read: int = 0
    block_cache_write: int = 0
    block_total_tokens: int = 0
    block_cost: float = 0.0
    block_burn_per_min: float | None = None
    block_proj_tokens: int | None = None
    block_proj_cost: float | None = None
    block_token_limit: int | None = None

    # Today (project-scoped)
    today_date: str | None = None
    today_input: int = 0
    today_output: int = 0
    today_cache_read: int = 0
    today_cache_write: int = 0
    today_cost: float = 0.0

    # Latest week (project-scoped). week_start is the ISO date of week beginning.
    week_start: str | None = None
    week_input: int = 0
    week_output: int = 0
    week_cost: float = 0.0
    # Calendar-based weekly reset (Mon 09:00 local) — minutes until next reset.
    weekly_remaining_min: int | None = None

    # All time (project-scoped totals)
    total_input: int = 0
    total_output: int = 0
    total_cost: float = 0.0

    # Set by the app when reset is active; panel shows "Since HH:MM"
    since_reset_at: str | None = None

    last_polled: str = ""
    error: str | None = None


CCUSAGE_CMD_PREFIX = ("bunx", "--bun", "ccusage")


def cwd_to_project_name(cwd: str) -> str:
    """ccusage normalizes a project path by replacing '/' with '-'."""
    return cwd.replace("/", "-")


class UsagePoller:
    def __init__(
        self,
        manager: SessionManager,
        on_update: Callable[[UsageSnapshot], None],
        interval: float = 5.0,
    ) -> None:
        self._manager = manager
        self._on_update = on_update
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._poke_event = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def poke(self) -> None:
        self._poke_event.set()

    async def _run(self) -> None:
        try:
            while True:
                snap = await self._poll_once()
                self._on_update(snap)
                try:
                    await asyncio.wait_for(
                        self._poke_event.wait(), timeout=self._interval
                    )
                except asyncio.TimeoutError:
                    pass
                self._poke_event.clear()
        except asyncio.CancelledError:
            pass

    async def _poll_once(self) -> UsageSnapshot:
        session = self._manager.active()
        if session is None:
            return UsageSnapshot(error="no active session", last_polled=_now_str())
        return await self._poll_agent(session)

    async def _poll_agent(self, session: Session) -> UsageSnapshot:
        now = datetime.now()
        snap = UsageSnapshot(
            agent=session.agent,
            cwd=session.cwd,
            last_polled=now.strftime("%H:%M:%S"),
            weekly_remaining_min=int(
                (next_weekly_reset(now) - now).total_seconds() // 60
            ),
        )

        # All sections are GLOBAL (no --project filter). The weekly quota and
        # 5-hour block are account-level concepts, not per-project.
        daily = await self._run_ccusage(session.agent, ("daily", "--json"))

        # weekly / blocks are claude-only subcommands in ccusage.
        if session.agent == "claude":
            weekly = await self._run_ccusage(session.agent, ("weekly", "--json"))
            blocks = await self._run_ccusage(session.agent, ("blocks", "--json"))
        else:
            weekly = {}
            blocks = {}

        if isinstance(daily, dict):
            _merge_daily(snap, daily)
        if isinstance(weekly, dict):
            _merge_weekly(snap, weekly)
        if isinstance(blocks, dict):
            _merge_blocks(snap, blocks)

        # Only surface an error if the daily call itself failed.
        if not isinstance(daily, dict):
            snap.error = daily if isinstance(daily, str) else "ccusage call failed"
        return snap

    async def _run_ccusage(
        self, agent: str, subcommand: tuple[str, ...]
    ) -> dict | str:
        """Run ccusage; return parsed JSON dict, or an error string.

        Empty stdout (which ccusage emits when a project filter has no data)
        is treated as an empty dict {} rather than an error.
        """
        cmd = (*CCUSAGE_CMD_PREFIX, agent, *subcommand)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except FileNotFoundError:
            return "bunx not found"

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip().splitlines()
            msg = err[-1] if err else f"exit {proc.returncode}"
            return f"ccusage: {msg[:80]}"

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            return {}

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            return f"bad JSON: {exc}"


def _merge_daily(snap: UsageSnapshot, data: dict) -> None:
    daily = data.get("daily") or []
    totals = data.get("totals") or {}
    today = datetime.now().strftime("%Y-%m-%d")

    today_entry: dict | None = None
    for entry in daily:
        if entry.get("date") == today:
            today_entry = entry
            break
    if today_entry is None and daily:
        today_entry = daily[-1]

    if today_entry and today_entry.get("date") == today:
        snap.today_date = today_entry.get("date")
        snap.today_input = int(today_entry.get("inputTokens", 0))
        snap.today_output = int(today_entry.get("outputTokens", 0))
        snap.today_cache_read = int(today_entry.get("cacheReadTokens", 0))
        snap.today_cache_write = int(today_entry.get("cacheCreationTokens", 0))
        snap.today_cost = float(today_entry.get("totalCost", 0.0))
    elif today_entry:
        # Latest day in this project isn't today — leave today_* at zero
        snap.today_date = today  # but mark "today" so panel shows zero, not "no data"

    snap.total_input = int(totals.get("inputTokens", 0))
    snap.total_output = int(totals.get("outputTokens", 0))
    snap.total_cost = float(totals.get("totalCost", 0.0))


def _merge_weekly(snap: UsageSnapshot, data: dict) -> None:
    weekly = data.get("weekly") or []
    if not weekly:
        return
    latest = weekly[-1]
    snap.week_start = latest.get("week")
    snap.week_input = int(latest.get("inputTokens", 0))
    snap.week_output = int(latest.get("outputTokens", 0))
    snap.week_cost = float(latest.get("totalCost", 0.0))


def _merge_blocks(snap: UsageSnapshot, data: dict) -> None:
    blocks = data.get("blocks") or []
    active: dict | None = None
    historical_max = 0
    for b in blocks:
        if b.get("isGap"):
            continue
        if b.get("isActive"):
            active = b
        else:
            tt = int(b.get("totalTokens", 0))
            if tt > historical_max:
                historical_max = tt

    if active is None:
        return

    tc = active.get("tokenCounts") or {}
    proj = active.get("projection") or {}
    burn = active.get("burnRate") or {}

    snap.block_start = active.get("startTime")
    snap.block_end = active.get("endTime")
    snap.block_input = int(tc.get("inputTokens", 0))
    snap.block_output = int(tc.get("outputTokens", 0))
    snap.block_cache_read = int(tc.get("cacheReadInputTokens", 0))
    snap.block_cache_write = int(tc.get("cacheCreationInputTokens", 0))
    snap.block_total_tokens = int(active.get("totalTokens", 0))
    snap.block_cost = float(active.get("costUSD", 0.0))
    rem = proj.get("remainingMinutes")
    snap.block_remaining_min = int(rem) if rem is not None else None
    pt = proj.get("totalTokens")
    snap.block_proj_tokens = int(pt) if pt is not None else None
    pc = proj.get("totalCost")
    snap.block_proj_cost = float(pc) if pc is not None else None
    bpm = burn.get("tokensPerMinute")
    snap.block_burn_per_min = float(bpm) if bpm is not None else None
    snap.block_token_limit = historical_max if historical_max > 0 else None


def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")
