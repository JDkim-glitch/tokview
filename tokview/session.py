"""Session model and SessionManager — single source of truth for session state.

The PTY/screen lives in PtyTerminalWidget (one per session); Session here is
just the metadata used by the sidebar and the app to coordinate switching.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from itertools import count

_id_seq = count(1)


@dataclass
class Session:
    id: str
    title: str
    agent: str
    cwd: str
    status: str = "running"  # running | exited


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._order: list[str] = []
        self._active_id: str | None = None

    @property
    def active_id(self) -> str | None:
        return self._active_id

    def active(self) -> Session | None:
        if self._active_id is None:
            return None
        return self._sessions.get(self._active_id)

    def list(self) -> list[Session]:
        return [self._sessions[sid] for sid in self._order]

    def __len__(self) -> int:
        return len(self._order)

    def create(self, agent: str = "claude", cwd: str | None = None) -> Session:
        n = next(_id_seq)
        sid = f"{agent}-{n}"
        title = f"{agent} #{n}"
        session = Session(
            id=sid,
            title=title,
            agent=agent,
            cwd=cwd or os.getcwd(),
        )
        self._sessions[sid] = session
        self._order.append(sid)
        if self._active_id is None:
            self._active_id = sid
        return session

    def close(self, sid: str) -> str | None:
        """Remove a session. Returns the new active_id (or None if empty)."""
        if sid not in self._sessions:
            return self._active_id
        idx = self._order.index(sid)
        del self._sessions[sid]
        self._order.pop(idx)
        if self._active_id == sid:
            if not self._order:
                self._active_id = None
            else:
                self._active_id = self._order[min(idx, len(self._order) - 1)]
        return self._active_id

    def set_active(self, sid: str) -> bool:
        if sid in self._sessions:
            self._active_id = sid
            return True
        return False

    def mark_exited(self, sid: str) -> None:
        session = self._sessions.get(sid)
        if session is not None:
            session.status = "exited"

    def next(self) -> str | None:
        if not self._order or self._active_id is None:
            return self._active_id
        i = self._order.index(self._active_id)
        self._active_id = self._order[(i + 1) % len(self._order)]
        return self._active_id

    def prev(self) -> str | None:
        if not self._order or self._active_id is None:
            return self._active_id
        i = self._order.index(self._active_id)
        self._active_id = self._order[(i - 1) % len(self._order)]
        return self._active_id
