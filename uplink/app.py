"""
UplinkApp — main Textual application.

Wires together:
  PtyManager  → ConversationPanel  (raw output streaming)
  InputBar    → PtyManager         (user input forwarded to claude)
  OutputParser → MessageStore      (message segmentation)
  MessageStore → HistoryPanel      (prompt list updates)
  HistoryPanel → ConversationPanel (filtered/live mode switching)
"""
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal

from uplink.parser import OutputParser
from uplink.pty_manager import PtyManager
from uplink.store import MessageStore, Role
from uplink.widgets.conversation_panel import ConversationPanel
from uplink.widgets.history_panel import HistoryPanel
from uplink.widgets.input_bar import InputBar


class UplinkApp(App[int]):
    """Claude Code CLI Enhanced — TUI wrapper with navigable prompt history."""

    TITLE = "Uplink"
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
        Binding("l", "jump_live", "Live", show=True),
        Binding("tab", "focus_next", "Switch pane", show=True),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }
    #main-split {
        layout: horizontal;
        height: 1fr;
    }
    """

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, extra_args: list[str], history_width: int) -> None:
        super().__init__()
        self._extra_args = extra_args
        self._history_width = history_width
        self._store = MessageStore()
        self._parser: OutputParser | None = None
        self._pty: PtyManager | None = None

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-split"):
            yield HistoryPanel(history_width=self._history_width, id="history")
            yield ConversationPanel(id="conversation")
        yield InputBar(id="input-bar")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self._parser = OutputParser(
            on_message=self._on_assistant_message_complete,
        )
        self._pty = PtyManager(
            extra_args=self._extra_args,
            on_output=lambda data: self.call_from_thread(self._on_pty_output, data),
            on_idle=lambda: self.call_from_thread(self._parser.flush),
            on_exit=lambda code: self.call_from_thread(self._on_pty_exit, code),
        )
        self._pty.start()

    def on_unmount(self) -> None:
        if self._pty:
            self._pty.terminate()

    # ------------------------------------------------------------------
    # Resize — propagate terminal size to the pty
    # ------------------------------------------------------------------

    def on_resize(self, event) -> None:
        if self._pty:
            self._pty.resize(event.size.height, event.size.width)

    # ------------------------------------------------------------------
    # Pty callbacks (called via call_from_thread — run on Textual loop)
    # ------------------------------------------------------------------

    def _on_pty_output(self, data: str) -> None:
        assert self._parser is not None
        self._parser.feed(data)
        self.query_one(ConversationPanel).append_raw(data)

    def _on_assistant_message_complete(self, content: str) -> None:
        self._store.add(Role.ASSISTANT, content)

    def _on_pty_exit(self, code: int) -> None:
        self.exit(code)

    # ------------------------------------------------------------------
    # Input — forwarded to pty
    # ------------------------------------------------------------------

    def on_input_bar_submitted(self, event: InputBar.Submitted) -> None:
        assert self._parser is not None
        assert self._pty is not None
        text = event.text
        self._parser.notify_user_input(text)
        msg = self._store.add(Role.USER, text)
        self.query_one(HistoryPanel).add_prompt(msg)
        self._pty.write(text + "\n")

    # ------------------------------------------------------------------
    # History panel — prompt selection
    # ------------------------------------------------------------------

    def on_history_panel_prompt_selected(self, event: HistoryPanel.PromptSelected) -> None:
        conv = self.query_one(ConversationPanel)
        input_bar = self.query_one(InputBar)
        if event.prompt_id is None:
            # [LIVE] selected
            conv.show_live(self._store)
            input_bar.set_active(True)
        else:
            conv.show_prompt(event.prompt_id, self._store)
            input_bar.set_active(False)

    # ------------------------------------------------------------------
    # Keybinding actions
    # ------------------------------------------------------------------

    def action_jump_live(self) -> None:
        """Jump back to LIVE mode from any filtered view."""
        conv = self.query_one(ConversationPanel)
        input_bar = self.query_one(InputBar)
        history = self.query_one(HistoryPanel)
        if conv.mode != "live":
            conv.show_live(self._store)
            input_bar.set_active(True)
            history.jump_to_live()
