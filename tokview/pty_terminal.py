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
import subprocess
import sys
import termios

import pyte
from rich.text import Text
from textual.events import (
    Focus,
    Key,
    MouseDown,
    MouseMove,
    MouseScrollDown,
    MouseScrollUp,
    MouseUp,
    Paste,
    Resize,
    Show,
)
from textual.geometry import Offset
from textual.message import Message
from textual.widget import Widget


def _in_linear_selection(
    x: int,
    y: int,
    bounds: tuple[int, int, int, int],
    cols: int,
) -> bool:
    """Return True if (x, y) falls inside a stream-style selection.

    Stream selection covers from (x0, y0) → end of row y0, every full row in
    between, and start of row y1 → (x1, y1). Matches how most terminals
    select text.
    """
    x0, y0, x1, y1 = bounds
    if y < y0 or y > y1:
        return False
    if y == y0 and y == y1:
        return x0 <= x <= x1
    if y == y0:
        return x >= x0
    if y == y1:
        return x <= x1
    return True


# pyte names ANSI 3 as "brown" (Rich rejects it as MissingStyle) and emits
# bright variants without underscores ("brightred" vs Rich's "bright_red").
# Unmapped names fall through to Rich, which handles standard ANSI fine.
_PYTE_NAME_TO_RICH = {
    "brown": "yellow",
    "brightblack": "bright_black",
    "brightred": "bright_red",
    "brightgreen": "bright_green",
    "brightbrown": "bright_yellow",
    "brightblue": "bright_blue",
    "brightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan",
    "brightwhite": "bright_white",
}


def _pyte_color_to_rich(color: str) -> str | None:
    if not color or color == "default":
        return None
    # pyte may emit 6-char hex (no leading '#') for true-color sequences
    if len(color) == 6 and all(c in "0123456789abcdef" for c in color.lower()):
        return f"#{color}"
    return _PYTE_NAME_TO_RICH.get(color, color)


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
        # Selection-copy hotkeys — handled by us, not the child.
        "ctrl+shift+c",
    })

    # Scrollback navigation keys — handled by us, never forwarded.
    SCROLL_KEYS = frozenset({"shift+up", "shift+down"})

    # Lines kept in the scrollback buffer above the live screen.
    HISTORY_LINES = 5000

    class Exited(Message):
        """Posted when the PTY child process closes (EOF on master read)."""

        def __init__(self, session_id: str | None, exit_code: int | None) -> None:
            super().__init__()
            self.session_id = session_id
            self.exit_code = exit_code

    class Activity(Message):
        """Posted when the user sends a prompt or the agent finishes a burst
        of output. Lets the app poke any pollers that should refresh now
        rather than waiting for the next tick.

        kind:
          - "submit": user pressed Enter (or pasted with a trailing newline)
          - "output": PTY went idle after a burst (500 ms quiet window)
        """

        def __init__(self, session_id: str | None, kind: str) -> None:
            super().__init__()
            self.session_id = session_id
            self.kind = kind

    # Quiet window before "the agent stopped talking, refresh usage now".
    OUTPUT_IDLE_DELAY = 0.5

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
        self._screen = pyte.HistoryScreen(
            self._cols, self._rows, history=self.HISTORY_LINES, ratio=0.5
        )
        self._stream = pyte.ByteStream(self._screen)
        self._master_fd: int | None = None
        self._pid: int | None = None
        self._exited = False
        self._output_idle_handle: asyncio.TimerHandle | None = None
        # Mouse-drag selection state. Both are (col, row) in screen cells,
        # or None when no selection is active. Cleared on the next mouse-down.
        self._sel_anchor: tuple[int, int] | None = None
        self._sel_cursor: tuple[int, int] | None = None
        # Scrollback offset in lines above the live bottom row.
        # 0 = live view; N = view N lines into history.top.
        self._scroll_offset = 0

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
        # If the user is scrolled into history, new linefeeds will push more
        # rows onto history.top. Bump the offset by the same amount so the
        # visible content stays anchored instead of sliding away under them.
        history_before = len(self._screen.history.top)
        self._stream.feed(data)
        if self._scroll_offset > 0:
            growth = len(self._screen.history.top) - history_before
            if growth > 0:
                max_offset = len(self._screen.history.top)
                self._scroll_offset = min(self._scroll_offset + growth, max_offset)
        self.refresh()
        self._update_terminal_cursor()
        self._schedule_output_idle()

    def _update_terminal_cursor(self) -> None:
        """Anchor the host terminal's hardware cursor to the pyte cursor so OS
        IME composition (Hangul, kana, pinyin, …) pops up at the agent's
        input position instead of floating wherever Textual last left the
        cursor. Also lets the host terminal redraw the cells the IME overlay
        clobbers — otherwise dropped frames show as black rectangles."""
        if not self.has_focus:
            return
        try:
            region = self.content_region
        except Exception:
            return
        cur_x = self._screen.cursor.x
        cur_y = self._screen.cursor.y
        offset = Offset(region.x + cur_x, region.y + cur_y)
        try:
            self.app.cursor_position = offset
        except Exception:
            pass

    def _schedule_output_idle(self) -> None:
        """Reset the 'agent went quiet' timer. Fires Activity('output') when
        the PTY hasn't emitted for OUTPUT_IDLE_DELAY seconds."""
        if self._output_idle_handle is not None:
            self._output_idle_handle.cancel()
        loop = asyncio.get_running_loop()
        self._output_idle_handle = loop.call_later(
            self.OUTPUT_IDLE_DELAY, self._fire_output_idle
        )

    def _fire_output_idle(self) -> None:
        self._output_idle_handle = None
        if self._exited:
            return
        self.post_message(self.Activity(session_id=self.session_id, kind="output"))

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
        history = self._screen.history.top
        sel = self._selection_bounds()
        offset = self._scroll_offset
        # Hide the blinking cursor while scrolled back — it would mark a
        # position on a frozen view, which is confusing.
        show_cursor = offset == 0
        # Virtual rows = history.top (oldest → newest) ++ screen.buffer.
        # View window starts at virtual index (len(history) - offset).
        history_len = len(history)
        view_start = history_len - offset
        for view_y in range(self._rows):
            v_idx = view_start + view_y
            if 0 <= v_idx < history_len:
                row = history[v_idx]
                in_history = True
            else:
                screen_y = v_idx - history_len
                # Clamp in case of edge resize race; treat out-of-range as blank.
                if 0 <= screen_y < self._rows:
                    row = buffer[screen_y]
                else:
                    row = None
                in_history = False
            if row is None:
                if view_y < self._rows - 1:
                    text.append("\n")
                continue
            for x in range(self._cols):
                cell = row[x]
                ch = cell.data
                # Wide chars (e.g. Hangul/CJK) occupy two grid cells in pyte:
                # the first holds the glyph, the second is a stub with data="".
                # Skip the stub so Rich's own width handling lines up with pyte.
                if ch == "":
                    continue
                if not ch:
                    ch = " "
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
                is_cursor = (
                    show_cursor
                    and not in_history
                    and x == cur_x
                    and (v_idx - history_len) == cur_y
                )
                is_sel = sel is not None and _in_linear_selection(
                    x, view_y, sel, self._cols
                )
                if cell.reverse ^ is_cursor ^ is_sel:
                    parts.append("reverse")
                style = " ".join(parts) if parts else None
                text.append(ch, style=style)
            if view_y < self._rows - 1:
                text.append("\n")
        return text

    def _selection_bounds(self) -> tuple[int, int, int, int] | None:
        if self._sel_anchor is None or self._sel_cursor is None:
            return None
        ax, ay = self._sel_anchor
        cx, cy = self._sel_cursor
        # Order anchor / cursor by (row, col) so callers can ignore drag dir.
        if (ay, ax) > (cy, cx):
            ax, ay, cx, cy = cx, cy, ax, ay
        return ax, ay, cx, cy

    def on_key(self, event: Key) -> None:
        if event.key == "ctrl+shift+c":
            event.stop()
            event.prevent_default()
            self._copy_selection()
            return
        if event.key in self.SCROLL_KEYS:
            event.stop()
            event.prevent_default()
            if event.key == "shift+up":
                self._scroll_by(1)
            else:
                self._scroll_by(-1)
            return
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
        # Typing into a scrolled-back view is confusing — drop back to the
        # live bottom first so the user sees their own input land.
        if self._scroll_offset != 0:
            self._scroll_offset = 0
            self.refresh()
        try:
            os.write(self._master_fd, data)
        except OSError:
            pass
        self._update_terminal_cursor()
        if event.key == "enter":
            self.post_message(
                self.Activity(session_id=self.session_id, kind="submit")
            )

    def on_focus(self, event: Focus) -> None:
        self._update_terminal_cursor()

    def on_show(self, event: Show) -> None:
        self._update_terminal_cursor()

    def on_mouse_down(self, event: MouseDown) -> None:
        # Left-button drag starts a selection. Right-button or modifier
        # combinations are reserved for future use.
        if event.button != 1:
            return
        event.stop()
        event.prevent_default()
        col = max(0, min(event.x, self._cols - 1))
        row = max(0, min(event.y, self._rows - 1))
        self._sel_anchor = (col, row)
        self._sel_cursor = (col, row)
        self.capture_mouse()
        self.refresh()

    def on_mouse_move(self, event: MouseMove) -> None:
        if self._sel_anchor is None:
            return
        col = max(0, min(event.x, self._cols - 1))
        row = max(0, min(event.y, self._rows - 1))
        if self._sel_cursor != (col, row):
            self._sel_cursor = (col, row)
            self.refresh()

    def on_mouse_up(self, event: MouseUp) -> None:
        if self._sel_anchor is None:
            return
        event.stop()
        event.prevent_default()
        self.release_mouse()
        # If the user just clicked without dragging, treat it as "clear
        # selection" rather than copying a single cell.
        if self._sel_anchor == self._sel_cursor:
            self._sel_anchor = None
            self._sel_cursor = None
            self.refresh()
            return
        self._copy_selection()

    # How many lines a single wheel notch travels. Three matches the default
    # behavior of most desktop terminals.
    _WHEEL_LINES = 3

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        event.stop()
        event.prevent_default()
        self._scroll_by(self._WHEEL_LINES)

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        event.stop()
        event.prevent_default()
        self._scroll_by(-self._WHEEL_LINES)

    def _scroll_by(self, delta_lines: int) -> None:
        """Move the scrollback view by delta_lines (positive = back into
        history, negative = toward live). Clamped to the available history."""
        max_offset = len(self._screen.history.top)
        new_offset = max(0, min(self._scroll_offset + delta_lines, max_offset))
        if new_offset == self._scroll_offset:
            return
        self._scroll_offset = new_offset
        self.refresh()

    def _copy_selection(self) -> None:
        text = self._selection_text()
        if not text:
            return
        copied = False
        # 1) Textual's clipboard API uses OSC 52 — works in iTerm2,
        # WezTerm, kitty, and Terminal.app (when "Allow apps on remote
        # hosts to set clipboard" is enabled).
        try:
            app = self.app
            if hasattr(app, "copy_to_clipboard"):
                app.copy_to_clipboard(text)
                copied = True
        except Exception:
            pass
        # 2) On macOS, pbcopy is always available and bypasses OSC 52
        # gating. Used as a belt-and-suspenders fallback.
        if sys.platform == "darwin":
            try:
                subprocess.run(
                    ["pbcopy"], input=text, text=True, check=False, timeout=2
                )
                copied = True
            except (OSError, subprocess.SubprocessError):
                pass
        if copied:
            self.app.bell()  # audible confirmation; non-fatal if disabled

    def _selection_text(self) -> str:
        bounds = self._selection_bounds()
        if bounds is None:
            return ""
        x0, y0, x1, y1 = bounds
        buffer = self._screen.buffer
        history = self._screen.history.top
        history_len = len(history)
        view_start = history_len - self._scroll_offset
        lines: list[str] = []
        for view_y in range(y0, y1 + 1):
            v_idx = view_start + view_y
            if 0 <= v_idx < history_len:
                row = history[v_idx]
            else:
                screen_y = v_idx - history_len
                if not (0 <= screen_y < self._rows):
                    lines.append("")
                    continue
                row = buffer[screen_y]
            if view_y == y0:
                start = x0
            else:
                start = 0
            if view_y == y1:
                end = x1 + 1
            else:
                end = self._cols
            chunk = "".join(
                (row[x].data or " ") for x in range(start, end)
            )
            # Trim trailing spaces per line — they're padding, not content.
            lines.append(chunk.rstrip())
        return "\n".join(lines)

    def on_paste(self, event: Paste) -> None:
        # Cmd+V / right-click paste arrives here: the host terminal sends
        # bracketed paste; Textual strips the wrapper and gives us the inner
        # text. Forward it to the PTY so the child agent actually receives it.
        if self._master_fd is None or not event.text:
            return
        event.stop()
        event.prevent_default()
        # Same rationale as typing: drop back to the live view so pasted
        # input doesn't land on a frozen scrollback frame.
        if self._scroll_offset != 0:
            self._scroll_offset = 0
            self.refresh()
        data = event.text.encode("utf-8", errors="replace")
        # If the child enabled DEC private mode 2004 (bracketed paste),
        # re-wrap so it can distinguish pasted content from typed input.
        # pyte stores private modes shifted by 5 bits.
        if (2004 << 5) in self._screen.mode:
            data = b"\x1b[200~" + data + b"\x1b[201~"
        try:
            os.write(self._master_fd, data)
        except OSError:
            pass
        if "\n" in event.text or "\r" in event.text:
            self.post_message(
                self.Activity(session_id=self.session_id, kind="submit")
            )

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
        # Resize can shrink or rebuild history.top; clamp offset so a stale
        # value doesn't leave the view rendering blank rows above content.
        max_offset = len(self._screen.history.top)
        if self._scroll_offset > max_offset:
            self._scroll_offset = max_offset
        self._resize_pty(cols, rows)
        self.refresh()

    async def on_unmount(self) -> None:
        if self._output_idle_handle is not None:
            self._output_idle_handle.cancel()
            self._output_idle_handle = None
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
