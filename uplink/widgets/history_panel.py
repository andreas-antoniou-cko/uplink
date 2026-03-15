"""
HistoryPanel — left panel showing the list of user prompts.

Phase 2: renders the list and [LIVE] item. Selection triggers nothing yet (Phase 3).
Phase 3: selecting an item posts PromptSelected to the app.
"""
from textual.app import ComposeResult
from textual.message import Message as TxtMessage
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView, Static

from uplink.store import Message


class HistoryPanel(Widget):
    DEFAULT_CSS = """
    HistoryPanel {
        height: 100%;
        border-right: tall $panel-lighten-1;
        background: $panel;
    }
    HistoryPanel .hp-header {
        background: $panel-darken-1;
        color: $text-muted;
        text-style: bold;
        height: 1;
        padding: 0 1;
        width: 100%;
    }
    HistoryPanel ListView {
        height: 1fr;
        background: $panel;
        border: none;
    }
    HistoryPanel ListItem {
        padding: 0 1;
    }
    HistoryPanel ListItem.--highlight {
        background: $accent 30%;
    }
    HistoryPanel .live-item Label {
        color: $success;
        text-style: bold;
    }
    """

    # ------------------------------------------------------------------
    # Custom messages
    # ------------------------------------------------------------------

    class PromptSelected(TxtMessage):
        """Posted when the user selects a historical prompt."""
        def __init__(self, prompt_id: int | None) -> None:
            super().__init__()
            self.prompt_id = prompt_id  # None means LIVE

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, history_width: int = 28, **kwargs) -> None:
        super().__init__(**kwargs)
        self._history_width = history_width
        self._prompt_count = 0

    def on_mount(self) -> None:
        self.styles.width = self._history_width

    def compose(self) -> ComposeResult:
        yield Static("PROMPT HISTORY [0]", classes="hp-header")
        live = ListItem(Label("▶  [LIVE]"), classes="live-item")
        live.data = None  # sentinel for LIVE
        yield ListView(live)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_prompt(self, msg: Message) -> None:
        """Prepend a new user prompt entry below [LIVE]."""
        self._prompt_count += 1
        self.query_one(".hp-header", Static).update(
            f"PROMPT HISTORY [{self._prompt_count}]"
        )
        max_label = max(self._history_width - 6, 10)
        preview = msg.preview(max_chars=max_label)
        label_text = f"#{msg.id} {preview}"

        new_item = ListItem(Label(label_text))
        new_item.data = msg.id  # type: ignore[attr-defined]

        lv = self.query_one(ListView)
        # Insert right after [LIVE] (index 0) so newest is always second.
        lv.mount(new_item, after=lv.children[0])

    def jump_to_live(self) -> None:
        """Highlight the [LIVE] entry."""
        lv = self.query_one(ListView)
        lv.index = 0

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        prompt_id = getattr(event.item, "data", None)
        self.post_message(self.PromptSelected(prompt_id))
