"""UsagePoller — polls usage data for the active session.

Per cycle (when an agent is active):
  - ccusage `daily --json` for the global daily totals across all agents (or
    the agent-specific subcommand `ccusage <agent> daily --json`).
  - For claude only: ccusage weekly/blocks (5h billing window + Mon 09:00
    weekly reset are claude-specific concepts).
  - For hermes: read `~/.hermes/state.db` directly — much faster than ccusage
    (~5ms vs ~1.7s) and richer (per-session model/token/cost breakdown).

Results merge into a single UsageSnapshot.

`since_reset_at` is filled in by the app (not the poller) when the user has
pressed the "reset all-time" hotkey; it lets the panel show "Since HH:MM"
instead of "All time".
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
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


def next_daily_reset(now: datetime | None = None) -> datetime:
    """Return tomorrow 00:00 local-time."""
    if now is None:
        now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=1)


@dataclass
class UsageSnapshot:
    agent: str | None = None
    cwd: str | None = None
    project_name: str | None = None

    # Currently configured model + provider for the agent (when discoverable).
    # For hermes: read from ~/.hermes/config.yaml's model.default/model.provider.
    # For other agents: falls back to the latest entry in models_today.
    model: str | None = None
    provider: str | None = None
    # Models used today (extracted from ccusage daily's modelsUsed or codex
    # models dict, deduped).
    models_today: list[str] = field(default_factory=list)
    # Today's message count (ccusage daily's messageCount for hermes/claude;
    # for hermes we also fill this from state.db).
    today_messages: int = 0

    # Active agent session — populated for hermes from state.db, otherwise
    # left at defaults. Lets the panel show "this session" vs "today" totals.
    session_title: str | None = None
    session_started_at: str | None = None
    session_messages: int = 0
    session_api_calls: int = 0
    session_input: int = 0
    session_output: int = 0
    session_cache_read: int = 0
    session_cache_write: int = 0
    session_reasoning: int = 0
    session_cost: float = 0.0

    # Daily reset (00:00 local) — applies to all agents whose quota concepts
    # are calendar-day based. Used by codex/gemini/copilot/hermes; claude uses
    # its 5h block + weekly reset instead.
    daily_remaining_min: int | None = None

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

HERMES_CONFIG_PATH = Path.home() / ".hermes" / "config.yaml"
HERMES_STATE_DB = Path.home() / ".hermes" / "state.db"


@dataclass
class HermesState:
    """A snapshot of hermes' SQLite state — far richer than ccusage daily."""

    # Most recently started session (assumed active if ended_at is NULL)
    active_id: str | None = None
    active_model: str | None = None
    active_provider: str | None = None
    active_title: str | None = None
    active_started_at: float | None = None
    active_is_open: bool = False
    active_messages: int = 0
    active_api_calls: int = 0
    active_input: int = 0
    active_output: int = 0
    active_cache_read: int = 0
    active_cache_write: int = 0
    active_reasoning: int = 0
    active_cost: float = 0.0

    # Aggregates across sessions started today (local time)
    today_input: int = 0
    today_output: int = 0
    today_cache_read: int = 0
    today_messages: int = 0
    today_cost: float = 0.0
    today_models: list[str] = field(default_factory=list)

    # Lifetime totals across every session in state.db.
    total_input: int = 0
    total_output: int = 0
    total_cost: float = 0.0


def read_hermes_state(path: Path = HERMES_STATE_DB) -> HermesState | None:
    """Read hermes' SQLite state. Returns None if file missing/unreadable.

    Uses a read-only URI so we can't accidentally lock the DB while hermes is
    writing. SQLite reads against a busy writer are cheap (~ms).
    """
    if not path.exists():
        return None
    uri = f"file:{path}?mode=ro&immutable=0"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
    except sqlite3.Error:
        return None

    state = HermesState()
    try:
        # Most-recent session by started_at — covers "currently running" and
        # "just finished" so the panel doesn't blank between turns.
        row = conn.execute(
            """
            SELECT id, model, billing_provider, title, started_at, ended_at,
                   message_count, api_call_count,
                   input_tokens, output_tokens, cache_read_tokens,
                   cache_write_tokens, reasoning_tokens,
                   COALESCE(actual_cost_usd, estimated_cost_usd, 0)
              FROM sessions
             ORDER BY started_at DESC
             LIMIT 1
            """
        ).fetchone()
        if row is not None:
            (
                state.active_id,
                state.active_model,
                state.active_provider,
                state.active_title,
                state.active_started_at,
                ended_at,
                state.active_messages,
                state.active_api_calls,
                state.active_input,
                state.active_output,
                state.active_cache_read,
                state.active_cache_write,
                state.active_reasoning,
                state.active_cost,
            ) = (
                row[0], row[1], row[2], row[3], row[4],
                row[5],
                int(row[6] or 0), int(row[7] or 0),
                int(row[8] or 0), int(row[9] or 0), int(row[10] or 0),
                int(row[11] or 0), int(row[12] or 0),
                float(row[13] or 0.0),
            )
            state.active_is_open = ended_at is None

        # Today's aggregates — buckets by local midnight.
        midnight = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        agg = conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(cache_read_tokens), 0),
                   COALESCE(SUM(message_count), 0),
                   COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd, 0)), 0)
              FROM sessions
             WHERE started_at >= ?
            """,
            (midnight,),
        ).fetchone()
        if agg is not None:
            state.today_input = int(agg[0] or 0)
            state.today_output = int(agg[1] or 0)
            state.today_cache_read = int(agg[2] or 0)
            state.today_messages = int(agg[3] or 0)
            state.today_cost = float(agg[4] or 0.0)

        models = conn.execute(
            """
            SELECT DISTINCT model
              FROM sessions
             WHERE started_at >= ? AND model IS NOT NULL
            """,
            (midnight,),
        ).fetchall()
        state.today_models = [m[0] for m in models if m[0]]

        lifetime = conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd, 0)), 0)
              FROM sessions
            """
        ).fetchone()
        if lifetime is not None:
            state.total_input = int(lifetime[0] or 0)
            state.total_output = int(lifetime[1] or 0)
            state.total_cost = float(lifetime[2] or 0.0)
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return state


def cwd_to_project_name(cwd: str) -> str:
    """ccusage normalizes a project path by replacing '/' with '-'."""
    return cwd.replace("/", "-")


def read_hermes_model(path: Path = HERMES_CONFIG_PATH) -> tuple[str | None, str | None]:
    """Read (model.default, model.provider) from a hermes config.yaml.

    Minimal YAML scan — only handles the top-level `model:` mapping with
    `default:` / `provider:` keys. Avoids a PyYAML dependency for one field.
    """
    if not path.exists():
        return None, None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None

    in_model = False
    default: str | None = None
    provider: str | None = None
    for raw in lines:
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_model = line.strip().rstrip(":") == "model"
            continue
        if in_model and indent >= 1:
            stripped = line.strip()
            if stripped.startswith("default:"):
                default = stripped.split(":", 1)[1].strip().strip("'\"") or None
            elif stripped.startswith("provider:"):
                provider = stripped.split(":", 1)[1].strip().strip("'\"") or None
            if default and provider:
                break
    return default, provider


class UsagePoller:
    # How often we let ourselves re-run the slow ccusage call for the same
    # agent. Pokes still fire fast updates (state.db); ccusage cost data is
    # refreshed at most once per this window.
    CCUSAGE_REFRESH_SECONDS = 30.0

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
        # agent → (monotonic_ts, daily_data) cache so repeated pokes don't
        # re-run ccusage every time.
        self._ccusage_cache: dict[str, tuple[float, dict]] = {}

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
        )

        # Calendar resets: claude has its own weekly window; everyone else
        # falls back to daily 00:00 local (which is how ccusage's daily
        # buckets are aligned).
        if session.agent == "claude":
            snap.weekly_remaining_min = int(
                (next_weekly_reset(now) - now).total_seconds() // 60
            )
        else:
            snap.daily_remaining_min = int(
                (next_daily_reset(now) - now).total_seconds() // 60
            )
            # Always render a Today row for non-claude agents so the reset
            # countdown is visible even before any usage hits ccusage.
            snap.today_date = now.strftime("%Y-%m-%d")

        # Hermes: SQLite read is ~ms and provides live session/today data.
        # ccusage is still needed for accurate cost (hermes' billing leaves
        # estimated_cost_usd=0 for most models), but we cache it for
        # CCUSAGE_REFRESH_SECONDS so pokes update tokens instantly without
        # paying ccusage's ~1.7s startup every time.
        if session.agent == "hermes":
            model, provider = read_hermes_model()
            snap.model = model
            snap.provider = provider
            state = await asyncio.to_thread(read_hermes_state)
            if state is not None:
                _merge_hermes_state(snap, state)
            daily = await self._get_ccusage_cached("hermes", ("daily", "--json"))
            if isinstance(daily, dict):
                # state.db already filled in_/out_/messages — only let
                # ccusage overwrite the cost fields.
                _merge_cost_only(snap, daily, "hermes")
            return snap

        daily = await self._run_ccusage(session.agent, ("daily", "--json"))

        # weekly / blocks are claude-only subcommands in ccusage.
        if session.agent == "claude":
            weekly = await self._run_ccusage(session.agent, ("weekly", "--json"))
            blocks = await self._run_ccusage(session.agent, ("blocks", "--json"))
        else:
            weekly = {}
            blocks = {}

        if isinstance(daily, dict):
            _merge_daily(snap, daily, session.agent)
        if isinstance(weekly, dict):
            _merge_weekly(snap, weekly)
        if isinstance(blocks, dict):
            _merge_blocks(snap, blocks)

        # Only surface an error if the daily call itself failed.
        if not isinstance(daily, dict):
            snap.error = daily if isinstance(daily, str) else "ccusage call failed"
        return snap

    async def _get_ccusage_cached(
        self, agent: str, subcommand: tuple[str, ...]
    ) -> dict | str:
        """Like _run_ccusage but reuses a result younger than the refresh window.

        Only caches successful dict results — errors fall through to a fresh
        retry on the next call.
        """
        now = time.monotonic()
        cached = self._ccusage_cache.get(agent)
        if cached is not None:
            ts, data = cached
            if now - ts < self.CCUSAGE_REFRESH_SECONDS:
                return data
        fresh = await self._run_ccusage(agent, subcommand)
        if isinstance(fresh, dict):
            self._ccusage_cache[agent] = (now, fresh)
        return fresh

    async def _run_ccusage(
        self, agent: str, subcommand: tuple[str, ...]
    ) -> dict | str:
        """Run ccusage; return parsed JSON dict, or an error string.

        Empty stdout (which ccusage emits when a project filter has no data)
        is treated as an empty dict {} rather than an error.
        """
        cmd = (*CCUSAGE_CMD_PREFIX, agent, *subcommand)
        try:
            # start_new_session detaches from the controlling TTY so bun/node
            # can't write OSC 2 title sequences to /dev/tty and fight the
            # host terminal's "tokview" title. stdin=DEVNULL stops tools from
            # treating us as interactive and emitting progress chrome.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
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


def _merge_daily(snap: UsageSnapshot, data: dict, agent: str) -> None:
    """Merge ccusage daily JSON into the snapshot.

    Schemas differ across agents:
      - claude/hermes: totalCost, cacheReadTokens, modelsUsed[], messageCount
      - codex:         costUSD,   cachedInputTokens, models{name: {...}}
    """
    daily = data.get("daily") or []
    totals = data.get("totals") or {}
    today = datetime.now().strftime("%Y-%m-%d")

    today_entry: dict | None = None
    for entry in daily:
        if entry.get("date") == today:
            today_entry = entry
            break
    # Fall back to the most recent entry so Model section can still show.
    if today_entry is None and daily:
        today_entry = daily[-1]

    is_codex = agent == "codex"

    def cost_of(d: dict) -> float:
        return float(d.get("costUSD", d.get("totalCost", 0.0)) or 0.0)

    def cache_read_of(d: dict) -> int:
        return int(d.get("cacheReadTokens", d.get("cachedInputTokens", 0)) or 0)

    def models_of(d: dict) -> list[str]:
        mu = d.get("modelsUsed")
        if isinstance(mu, list):
            return [str(m) for m in mu if m]
        models = d.get("models")
        if isinstance(models, dict):
            return list(models.keys())
        return []

    if today_entry and today_entry.get("date") == today:
        # Hermes state.db already filled today_* with finer-grained data —
        # don't clobber it with ccusage's view (which can lag).
        keep_hermes = (
            agent == "hermes" and snap.today_messages > 0
        )
        snap.today_date = today_entry.get("date")
        if not keep_hermes:
            snap.today_input = int(today_entry.get("inputTokens", 0))
            snap.today_output = int(today_entry.get("outputTokens", 0))
            snap.today_cache_read = cache_read_of(today_entry)
            snap.today_cache_write = int(
                today_entry.get("cacheCreationTokens", 0) or 0
            )
            snap.today_cost = cost_of(today_entry)
            snap.today_messages = int(today_entry.get("messageCount", 0) or 0)
        models = models_of(today_entry)
        if models:
            # Dedupe while preserving first-seen order.
            seen: dict[str, None] = {}
            for m in (*snap.models_today, *models):
                seen.setdefault(m, None)
            snap.models_today = list(seen.keys())
        if not snap.model and snap.models_today:
            snap.model = snap.models_today[-1]
    elif today_entry:
        # Latest day isn't today — leave today_* at zero but flag "today" so
        # the panel renders the row at zero rather than hiding it entirely.
        # Also surface the most recent model so Model section still shows.
        snap.today_date = today
        recent_models = models_of(today_entry)
        if recent_models and not snap.model:
            snap.model = recent_models[-1]

    snap.total_input = int(totals.get("inputTokens", 0) or 0)
    snap.total_output = int(totals.get("outputTokens", 0) or 0)
    snap.total_cost = cost_of(totals) if (is_codex or totals) else 0.0


def _merge_cost_only(snap: UsageSnapshot, data: dict, agent: str) -> None:
    """Pull only the cost fields from a ccusage daily payload.

    Used for hermes, where state.db is the source of truth for tokens but
    ccusage knows model prices.
    """
    daily = data.get("daily") or []
    totals = data.get("totals") or {}
    today = datetime.now().strftime("%Y-%m-%d")

    cost_key = "costUSD" if agent == "codex" else "totalCost"

    for entry in daily:
        if entry.get("date") == today:
            snap.today_cost = float(entry.get(cost_key, 0.0) or 0.0)
            break
    snap.total_cost = float(totals.get(cost_key, totals.get("totalCost", 0.0)) or 0.0)


def _merge_hermes_state(snap: UsageSnapshot, state: HermesState) -> None:
    # Active-session row (lets the panel show "this session" vs "today").
    if state.active_id is not None:
        snap.session_title = state.active_title
        snap.session_messages = state.active_messages
        snap.session_api_calls = state.active_api_calls
        snap.session_input = state.active_input
        snap.session_output = state.active_output
        snap.session_cache_read = state.active_cache_read
        snap.session_cache_write = state.active_cache_write
        snap.session_reasoning = state.active_reasoning
        snap.session_cost = state.active_cost
        if state.active_started_at:
            snap.session_started_at = datetime.fromtimestamp(
                state.active_started_at
            ).strftime("%H:%M")
        # Prefer state.db's view of the actually-running model over config.yaml
        # (config is what hermes would use on the next launch; state.db is what
        # is loaded right now).
        if state.active_model:
            snap.model = state.active_model
        if state.active_provider and not snap.provider:
            snap.provider = state.active_provider

    # Today aggregates from SQLite — more accurate / lower-latency than ccusage.
    today = datetime.now().strftime("%Y-%m-%d")
    snap.today_date = today
    snap.today_input = state.today_input
    snap.today_output = state.today_output
    snap.today_cache_read = state.today_cache_read
    snap.today_messages = state.today_messages
    snap.today_cost = state.today_cost
    if state.today_models:
        snap.models_today = state.today_models

    # Lifetime totals from SQLite — let the panel show "All time" without
    # waiting on ccusage.
    snap.total_input = state.total_input
    snap.total_output = state.total_output
    snap.total_cost = state.total_cost


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
