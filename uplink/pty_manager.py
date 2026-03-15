"""
PtyManager — spawns `claude` in a pseudo-terminal and provides a clean I/O interface.

Platform abstraction:
  Windows : pywinpty  (wraps the Windows ConPTY API)
  macOS / Linux : ptyprocess

The manager runs a background thread that continuously reads from the pty and
calls the registered output callback. Callers write to the pty via write().
An idle-flush timer detects when the assistant has gone quiet and tells the
parser to close the current message.
"""
import shutil
import sys
import threading
import time
from typing import Callable


# How long (seconds) with no new output before we consider an assistant turn complete.
_IDLE_FLUSH_SECS = 0.4


class PtyManager:
    def __init__(
        self,
        extra_args: list[str] | None = None,
        on_output: Callable[[str], None] | None = None,
        on_idle: Callable[[], None] | None = None,
        on_exit: Callable[[int], None] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        extra_args : extra CLI arguments forwarded to `claude`
        on_output  : called with each str chunk read from the pty
        on_idle    : called after _IDLE_FLUSH_SECS of silence (use to flush parser)
        on_exit    : called with the exit code when the child process exits
        """
        self._extra_args = extra_args or []
        self.on_output = on_output
        self.on_idle = on_idle
        self.on_exit = on_exit

        self._proc = None
        self._read_thread: threading.Thread | None = None
        self._last_output_time: float = 0.0
        self._idle_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        cols, rows = shutil.get_terminal_size(fallback=(220, 50))
        cmd = ["claude"] + self._extra_args

        if sys.platform == "win32":
            self._proc = self._spawn_windows(cmd, rows, cols)
        else:
            self._proc = self._spawn_unix(cmd, rows, cols)

        self._last_output_time = time.monotonic()
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True, name="pty-reader")
        self._idle_thread = threading.Thread(target=self._idle_loop, daemon=True, name="pty-idle")
        self._read_thread.start()
        self._idle_thread.start()

    def terminate(self) -> None:
        self._stop_event.set()
        if self._proc is not None:
            try:
                self._proc.terminate(force=True)
            except Exception:
                pass

    def wait(self, timeout: float | None = None) -> None:
        if self._read_thread:
            self._read_thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def write(self, text: str) -> None:
        """Send text to the pty stdin (i.e. to claude)."""
        if self._proc is None or not self._proc.isalive():
            return
        if sys.platform == "win32":
            self._proc.write(text)
        else:
            self._proc.write(text.encode("utf-8"))

    def resize(self, rows: int, cols: int) -> None:
        """Inform the pty of a terminal size change."""
        if self._proc and self._proc.isalive():
            self._proc.setwinsize(rows, cols)

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.isalive()

    # ------------------------------------------------------------------
    # Internal threads
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._proc is None or not self._proc.isalive():
                break
            try:
                data = self._proc.read(4096)
            except EOFError:
                break
            except Exception:
                break

            if not data:
                continue

            self._last_output_time = time.monotonic()

            # Normalise to str
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")

            if self.on_output:
                self.on_output(data)

        # Child exited — report exit code
        if self.on_exit:
            code = 0
            try:
                if hasattr(self._proc, "exitstatus"):
                    code = self._proc.exitstatus or 0
            except Exception:
                pass
            self.on_exit(code)

    def _idle_loop(self) -> None:
        """Fires on_idle when no output has been received for _IDLE_FLUSH_SECS."""
        last_fired: float = 0.0
        while not self._stop_event.is_set():
            time.sleep(0.05)
            now = time.monotonic()
            quiet_for = now - self._last_output_time
            # Only fire once per quiet period (reset when new output arrives)
            if quiet_for >= _IDLE_FLUSH_SECS and self._last_output_time > last_fired:
                last_fired = self._last_output_time
                if self.on_idle:
                    self.on_idle()

    # ------------------------------------------------------------------
    # Platform-specific spawn
    # ------------------------------------------------------------------

    @staticmethod
    def _spawn_windows(cmd: list[str], rows: int, cols: int):
        from winpty import PtyProcess  # type: ignore[import]
        # pywinpty expects a single command string on Windows
        return PtyProcess.spawn(" ".join(cmd), dimensions=(rows, cols))

    @staticmethod
    def _spawn_unix(cmd: list[str], rows: int, cols: int):
        from ptyprocess import PtyProcess  # type: ignore[import]
        return PtyProcess.spawn(cmd, dimensions=(rows, cols))
