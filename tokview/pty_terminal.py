"""PtyTerminalWidget — Textual widget that hosts a live PTY-backed process.

Spawns a child (default: `claude`) on a pseudo-terminal, feeds its output to a
pyte screen buffer, renders that buffer as the widget body, and forwards
keystrokes back to the PTY. Handles resize by propagating size to both the
pyte screen and the PTY via TIOCSWINSZ.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import shlex
import signal
import struct
import termios

import pyte
from rich.text import Text
from textual.events import Key, Resize
from textual.message import Message
from textual.widget import Widget


def _pyte_color_to_rich(color: str) -> str | None:
    if not color or color == "default":
        return None
    # pyte may emit 6-char hex (no leading '#') for true-color sequences
    if len(color) == 6 and all(c in "0123456789abcdef" for c in color.lower()):
        return f"#{color}"
    return color  # named color, passed through to Rich


class PtyTerminalWidget(Widget, can_focus=True):
    DEFAULT_CSS = """
    PtyTerminalWidget {
        background: $surface;
        color: $foreground;
    }
    """

    # Keys reserved for the host app — NOT forwarded to the PTY
    RESERVED_KEYS = frozenset({
        "f2", "f3", "f4", "f5", "f6", "f7", "f12",
        "shift+left", "shift+right",
    })

    class Exited(Message):
        """Posted when the PTY child process closes (EOF on master read)."""

        def __init__(self, session_id: str | None, exit_code: int | None) -> None:
            super().__init__()
            self.session_id = session_id
            self.exit_code = exit_code

    def __init__(
        self,
        command: str = "claude",
        cwd: str | None = None,
        session_id: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._command = command
        self._cwd = cwd
        self.session_id = session_id
        self._cols = 80
        self._rows = 24
        self._screen = pyte.Screen(self._cols, self._rows)
        self._stream = pyte.ByteStream(self._screen)
        self._master_fd: int | None = None
        self._pid: int | None = None
        self._exited = False

    async def on_mount(self) -> None:
        self._spawn()
        if self._master_fd is not None:
            asyncio.get_running_loop().add_reader(self._master_fd, self._on_pty_readable)

    def _spawn(self) -> None:
        pid, master_fd = pty.fork()
        if pid == 0:
            # Child process
            try:
                if self._cwd:
                    os.chdir(self._cwd)
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                argv = shlex.split(self._command)
                os.execvpe(argv[0], argv, env)
            except Exception:
                os._exit(1)
        # Parent
        self._pid = pid
        self._master_fd = master_fd
        os.set_blocking(master_fd, False)
        self._resize_pty(self._cols, self._rows)

    def _resize_pty(self, cols: int, rows: int) -> None:
        if self._master_fd is None:
            return
        try:
            fcntl.ioctl(
                self._master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

    def _on_pty_readable(self) -> None:
        assert self._master_fd is not None
        try:
            data = os.read(self._master_fd, 4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._handle_eof()
            return
        if not data:
            self._handle_eof()
            return
        self._stream.feed(data)
        self.refresh()

    def _handle_eof(self) -> None:
        if self._exited:
            return
        self._exited = True
        self._detach_reader()
        exit_code = self._try_reap()
        self.post_message(self.Exited(session_id=self.session_id, exit_code=exit_code))

    def _try_reap(self) -> int | None:
        if not self._pid:
            return None
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if pid != self._pid:
            return None
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return None

    def _detach_reader(self) -> None:
        if self._master_fd is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(self._master_fd)
        except (ValueError, OSError, RuntimeError):
            pass

    def render(self) -> Text:
        text = Text()
        cur_x, cur_y = self._screen.cursor.x, self._screen.cursor.y
        buffer = self._screen.buffer
        for y in range(self._rows):
            row = buffer[y]
            for x in range(self._cols):
                cell = row[x]
                ch = cell.data or " "
                parts: list[str] = []
                fg = _pyte_color_to_rich(cell.fg)
                bg = _pyte_color_to_rich(cell.bg)
                if fg:
                    parts.append(fg)
                if bg:
                    parts.append(f"on {bg}")
                if cell.bold:
                    parts.append("bold")
                if cell.italics:
                    parts.append("italic")
                if cell.underscore:
                    parts.append("underline")
                is_cursor = (x == cur_x and y == cur_y)
                if cell.reverse ^ is_cursor:
                    parts.append("reverse")
                style = " ".join(parts) if parts else None
                text.append(ch, style=style)
            if y < self._rows - 1:
                text.append("\n")
        return text

    def on_key(self, event: Key) -> None:
        if self._master_fd is None:
            return
        if event.key in self.RESERVED_KEYS:
            # Let the host app handle it (e.g., quit)
            return
        data = self._key_to_bytes(event)
        if not data:
            return
        event.stop()
        event.prevent_default()
        try:
            os.write(self._master_fd, data)
        except OSError:
            pass

    @staticmethod
    def _key_to_bytes(event: Key) -> bytes:
        key = event.key
        SPECIALS = {
            "enter": b"\r",
            "tab": b"\t",
            "escape": b"\x1b",
            "backspace": b"\x7f",
            "delete": b"\x1b[3~",
            "up": b"\x1b[A",
            "down": b"\x1b[B",
            "right": b"\x1b[C",
            "left": b"\x1b[D",
            "home": b"\x1b[H",
            "end": b"\x1b[F",
            "pageup": b"\x1b[5~",
            "pagedown": b"\x1b[6~",
            "space": b" ",
        }
        if key in SPECIALS:
            return SPECIALS[key]
        if key.startswith("ctrl+") and len(key) == 6:
            letter = key[-1].lower()
            if "a" <= letter <= "z":
                return bytes([ord(letter) - ord("a") + 1])
        if event.character:
            return event.character.encode("utf-8", errors="replace")
        return b""

    def on_resize(self, event: Resize) -> None:
        cols = max(1, event.size.width)
        rows = max(1, event.size.height)
        if cols == self._cols and rows == self._rows:
            return
        self._cols = cols
        self._rows = rows
        self._screen.resize(rows, cols)
        self._resize_pty(cols, rows)
        self.refresh()

    async def on_unmount(self) -> None:
        self._detach_reader()
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._pid = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
