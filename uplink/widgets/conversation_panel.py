"""
ConversationPanel — right panel displaying the conversation output.

Modes:
  LIVE     — streams all pty output as it arrives; auto-scrolls to bottom.
  FILTERED — shows only a specific prompt/response pair; input is disabled.
"""
import re

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static

from uplink.store import Message, MessageStore, Role


# ANSI sequences that imply cursor movement or screen manipulation.
# These are meaningless in an append-only log and can corrupt the display.
_CURSOR_OPS = re.compile(
    r"""
    \x1b\[
    (?:
        \d*[ABCDEFGHJKMPST]   # cursor move / erase / scroll
      | \d*;\d*[Hf]           # cursor position
      | \?(?:25[lh]|47[lh]|1049[lh])  # alt screen / hide cursor
      | \d+[lh]               # private mode set/reset
    )
    | \x1b[78]                # save / restore cursor (ESC 7 / ESC 8)
    """,
    re.VERBOSE,
)


def sanitize_for_log(data: str) -> str:
    """
    Strip cursor-movement and screen-control sequences from raw pty output,
    while preserving SGR colour/style codes that RichLog can render.
    Also collapses bare \\r (spinner resets) to avoid double-printing.
    """
    data = _CURSOR_OPS.sub("", data)
    # Replace bare \r (not followed by \n) with nothing — spinner overwrite artefacts.
    data = re.sub(r"\r(?!\n)", "", data)
    return data


class ConversationPanel(Widget):
    DEFAULT_CSS = """
    ConversationPanel {
        height: 100%;
        width: 1fr;
    }
    ConversationPanel RichLog {
        height: 1fr;
        padding: 0 1;
        scrollbar-gutter: stable;
    }
    ConversationPanel .cv-status-bar {
        height: 1;
        background: $warning 30%;
        color: $text;
        padding: 0 1;
        display: none;
    }
    ConversationPanel .cv-status-bar.visible {
        display: block;
    }
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mode: str = "live"         # "live" | "filtered"
        self._filtered_id: int | None = None

    def compose(self) -> ComposeResult:
        yield Static(
            "Viewing historical prompt — press [L] to return to live",
            classes="cv-status-bar",
        )
        yield RichLog(highlight=False, markup=False, wrap=True, id="conv-log")

    # ------------------------------------------------------------------
    # LIVE mode — streaming output
    # ------------------------------------------------------------------

    def append_raw(self, data: str) -> None:
        """Append raw pty output (LIVE mode only)."""
        if self._mode != "live":
            return
        clean = sanitize_for_log(data)
        if not clean:
            return
        log = self.query_one(RichLog)
        log.write(Text.from_ansi(clean), scroll_end=True)

    # ------------------------------------------------------------------
    # FILTERED mode — show a specific prompt/response pair
    # ------------------------------------------------------------------

    def show_prompt(self, prompt_id: int, store: MessageStore) -> None:
        """Switch to filtered mode and display the selected prompt + reply."""
        self._mode = "filtered"
        self._filtered_id = prompt_id

        messages = store.for_prompt(prompt_id)
        log = self.query_one(RichLog)
        log.clear()

        for msg in messages:
            prefix = "You: " if msg.role == Role.USER else "Claude: "
            header = Text(prefix, style="bold cyan" if msg.role == Role.USER else "bold green")
            log.write(header)
            log.write(Text.from_ansi(sanitize_for_log(msg.content)))
            log.write(Text(""))  # blank line between messages

        # Show the status bar
        self.query_one(".cv-status-bar", Static).add_class("visible")

    def show_live(self, store: MessageStore) -> None:
        """Switch back to LIVE mode, rebuilding the full history from the store."""
        self._mode = "live"
        self._filtered_id = None

        log = self.query_one(RichLog)
        log.clear()

        for msg in store.all():
            prefix = "You: " if msg.role == Role.USER else "Claude: "
            style = "bold cyan" if msg.role == Role.USER else "bold green"
            log.write(Text(prefix, style=style))
            log.write(Text.from_ansi(sanitize_for_log(msg.content)))
            log.write(Text(""))

        log.scroll_end(animate=False)
        self.query_one(".cv-status-bar", Static).remove_class("visible")

    @property
    def mode(self) -> str:
        return self._mode
