"""
InputBar — bottom input widget.

Active only in LIVE mode. Disabled in FILTERED mode with a visual hint.
Submits text to the pty on Enter.
"""
from textual.app import ComposeResult
from textual.message import Message as TxtMessage
from textual.widget import Widget
from textual.widgets import Input


class InputBar(Widget):
    DEFAULT_CSS = """
    InputBar {
        height: 3;
        padding: 0 1;
        border-top: tall $panel-lighten-1;
        dock: bottom;
        background: $panel;
    }
    InputBar Input {
        width: 1fr;
        background: $surface;
    }
    InputBar Input:disabled {
        background: $panel-darken-1;
        color: $text-disabled;
    }
    """

    # ------------------------------------------------------------------
    # Custom messages
    # ------------------------------------------------------------------

    class Submitted(TxtMessage):
        """Posted when the user presses Enter with non-empty input."""
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type a message and press Enter to send…")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.post_message(self.Submitted(text))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_active(self, active: bool) -> None:
        """Enable or disable the input widget."""
        inp = self.query_one(Input)
        if active:
            inp.disabled = False
            inp.placeholder = "Type a message and press Enter to send…"
            inp.focus()
        else:
            inp.disabled = True
            inp.placeholder = "Input disabled — press L to return to live"
