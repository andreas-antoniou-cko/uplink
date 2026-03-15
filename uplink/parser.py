"""
Output parser — state machine that segments raw pty output into conversation messages.

Design notes:
- User messages are captured on the INPUT side (we intercept stdin before forwarding
  to the pty), so we always know exactly when and what the user submitted.
- The parser's job is to delimit assistant responses: everything arriving from the
  pty after a user submission (until we detect the next idle/ready state) is
  treated as the assistant's reply.
- "Ready" detection is heuristic: we look for Claude Code's idle prompt marker in
  the output stream. If that's not reliable, a short idle-timeout flush is used as
  a fallback (implemented in PtyManager, which calls flush() after a quiet period).
"""
import re
from enum import Enum, auto
from typing import Callable


# Matches the most common ANSI escape sequences.
_ANSI_ESCAPE = re.compile(
    r"""
    \x1b        # ESC
    (?:
        \[[0-9;]*[mGKHFJABCDnsuhl]  # CSI sequences (colours, cursor movement, etc.)
      | \][^\x07\x1b]*(?:\x07|\x1b\\)  # OSC sequences
      | [()][A-B012]              # charset designation
      | [DECM78]                  # single-char sequences
    )
    | \r        # bare carriage return (spinner cleanup)
""",
    re.VERBOSE,
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes and bare carriage returns from text."""
    return _ANSI_ESCAPE.sub("", text)


class ParserState(Enum):
    IDLE = auto()             # waiting for user to submit input
    READING_ASSISTANT = auto()  # consuming assistant response


class OutputParser:
    """
    Segments raw pty output into assistant messages.

    Usage:
        parser = OutputParser(
            on_chunk=lambda s: ...,   # called with each raw chunk as it arrives
            on_message=lambda s: ..., # called with the full content when a reply is done
        )
        parser.notify_user_input("hello")   # call before forwarding input to pty
        parser.feed(raw_pty_bytes)          # call for each chunk read from pty
        parser.flush()                      # call on idle timeout to close a reply
    """

    # Claude Code shows this prefix when it's ready for input.
    # The actual prompt may vary — this is a best-effort heuristic.
    _READY_PATTERNS = [
        re.compile(r"^\s*>\s*$", re.MULTILINE),          # bare ">" prompt line
        re.compile(r"✓\s+\w"),                            # completion checkmark
    ]

    def __init__(
        self,
        on_chunk: Callable[[str], None] | None = None,
        on_message: Callable[[str], None] | None = None,
    ) -> None:
        self._state = ParserState.IDLE
        self._buffer: str = ""
        self.on_chunk = on_chunk      # raw streaming chunk callback
        self.on_message = on_message  # completed message callback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify_user_input(self, text: str) -> None:
        """
        Call this BEFORE forwarding user input to the pty.
        Flushes any in-progress assistant message and transitions to READING_ASSISTANT.
        """
        self._flush_if_pending()
        self._state = ParserState.READING_ASSISTANT
        self._buffer = ""

    def feed(self, data: str) -> None:
        """Feed a raw chunk of pty output into the parser."""
        if self._state == ParserState.IDLE:
            # Output before first user message — ignore (it's Claude Code's banner/UI).
            return

        self._buffer += data

        if self.on_chunk:
            self.on_chunk(data)

        # Check whether this chunk signals the end of the assistant turn.
        if self._looks_ready(data):
            self.flush()

    def flush(self) -> None:
        """
        Force-complete the current assistant message.
        Called by PtyManager after an idle timeout, or when a ready-marker is detected.
        """
        if self._state == ParserState.READING_ASSISTANT and self._buffer.strip():
            content = self._buffer
            self._buffer = ""
            self._state = ParserState.IDLE
            if self.on_message:
                self.on_message(content)

    @property
    def state(self) -> ParserState:
        return self._state

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _flush_if_pending(self) -> None:
        if self._state == ParserState.READING_ASSISTANT and self._buffer.strip():
            self.flush()

    def _looks_ready(self, chunk: str) -> bool:
        """Heuristic: does this chunk contain a ready-for-input marker?"""
        clean = strip_ansi(chunk)
        return any(p.search(clean) for p in self._READY_PATTERNS)
