# Claude Code CLI Enhanced — Specification

## Overview

`uplink` is a TUI (Terminal User Interface) wrapper around `claude` (Claude Code CLI) that adds a navigable prompt history panel, allowing users to select any previous prompt and view only the output associated with it — while always being able to jump back to the live conversation tail.

---

## Problem Statement

Claude Code's default output is a linear scrollback buffer. When a session is long, finding the response to a specific earlier prompt requires manual scrolling with no structural anchors. There is no way to jump to "what did Claude say when I asked X?" without scrolling through everything in between.

---

## Goals

- Wrap `claude` transparently — it should behave identically to running `claude` directly.
- Add a left-panel prompt history list that updates live as new prompts are entered.
- Allow selecting any historical prompt to filter the right panel to show only that prompt + its response.
- Provide a clear "jump to live" action to return to the current tail of the conversation.
- Ship as a runnable Python script; optionally packaged into a single `.exe` via PyInstaller.

## Non-Goals

- Replicate or replace Claude Code functionality.
- Persistent storage beyond what Claude Code already writes.
- Multi-session management (out of scope for v1).
- Editing or resending historical prompts.

---

## UI Layout

```
┌──────────────────────┬─────────────────────────────────────────┐
│  PROMPT HISTORY  [8] │  CONVERSATION                           │
│                      │                                         │
│ > [LIVE]             │  User: explain this function            │
│   #8 explain this... │                                         │
│   #7 what does X do  │  Claude: This function iterates over    │
│   #6 refactor the    │  the list and applies a transformation  │
│   #5 add error han.. │  to each element...                     │
│   #4 write tests for │                                         │
│   #3 summarise the   │                                         │
│   #2 what is the p.. │                                         │
│   #1 hello           │                                         │
│                      │                                         │
│                      ├─────────────────────────────────────────┤
│                      │  > [input box — only active in LIVE]    │
└──────────────────────┴─────────────────────────────────────────┘
 [Tab] Switch pane  [↑↓] Navigate  [Enter] Select  [L] Jump live  [Q] Quit
```

### Panel: Prompt History (left)

- Fixed width (configurable, default 26 chars).
- Lists all user prompts in the current session, most recent at the top.
- Each entry shows: `#N <truncated prompt text>`.
- `[LIVE]` is always pinned at the top and represents the current tail.
- Selected item is highlighted.
- Scrollable when history exceeds panel height.
- Updates in real-time as new prompts are submitted.

### Panel: Conversation (right)

- **LIVE mode** (default): shows the full scrollback of the session, auto-scrolls to bottom as new output arrives. Input box is active.
- **Filtered mode**: shows only the selected prompt and its complete response. Input box is disabled with a visual indicator ("press L to return to live").
- Renders with the same formatting claude outputs (ANSI colour passthrough).

### Input Box

- Rendered at the bottom of the right panel.
- Active only in LIVE mode.
- Passes input to the underlying `claude` process via stdin.
- Supports multiline input (Shift+Enter for newline, Enter to submit).

---

## Interaction Model

| Action | Key |
|--------|-----|
| Switch focus between panels | `Tab` |
| Navigate prompt list | `↑` / `↓` |
| Select prompt (filter view) | `Enter` |
| Jump to LIVE mode | `L` or select `[LIVE]` |
| Scroll conversation pane | `PgUp` / `PgDn` |
| Quit | `Q` / `Ctrl+C` |

When the left panel has focus, arrow keys navigate the list. When the right panel has focus, arrow keys scroll the conversation.

---

## Architecture

### Process Model

```
cchat (TUI process)
  ├── pty: runs `claude` as a child process  (via pywinpty on Windows)
  │     ├── stdin  ← input box submissions
  │     └── stdout → output parser → message store
  └── Textual render loop
        ├── HistoryPanel  ← message store (user prompts only)
        └── ConversationPanel ← message store (full or filtered)
```

`uplink` spawns `claude` inside a pseudo-terminal (pty) using `pywinpty` on Windows (ConPTY API) or `ptyprocess` on macOS/Linux. This gives Claude Code a proper terminal environment (colours, width signals, etc.) while allowing `uplink` to intercept stdout for parsing.

A background `asyncio` task reads from the pty fd in a loop and feeds bytes to the output parser. Parsed messages post events back to the Textual app via `app.call_from_thread()`.

### Message Store

An in-memory ordered list of `Message` objects built by parsing the pty output stream in real time.

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

class Role(Enum):
    USER = "user"
    ASSISTANT = "assistant"

@dataclass
class Message:
    id: int
    role: Role
    content: str          # raw ANSI-preserved text
    timestamp: datetime = field(default_factory=datetime.now)
```

### Output Parser

Claude Code output follows recognisable patterns:

- User input echo: line(s) starting with `> ` (the prompt prefix claude prints after submission)
- Assistant response: everything between user echo and the next prompt or idle state
- Thinking indicators, tool use blocks, etc. are included in the assistant message they belong to

The parser is a simple state machine:

```
IDLE → (sees "> ") → READING_USER → (newline) → READING_ASSISTANT → (sees next "> " or prompt) → READING_USER
```

Parsed messages are appended to the message store, which triggers a Textual reactive re-render.

**Edge cases to handle:**
- Streaming output (assistant response arrives token by token)
- Multi-line user input
- Tool use output blocks (file edits, bash output, etc.) — treated as part of the assistant message
- ANSI escape codes must be preserved for rendering but stripped for truncation in the left panel

### Session Persistence

Claude Code already writes session files to `~/.claude/projects/<path-hash>/`. `uplink` does **not** read or write these files — the message store is built purely from the live pty stream. This avoids any coupling to Claude Code's internal file format.

---

## Component Breakdown (Textual)

```
CchatApp  (textual.App)
├── HistoryPanel  (textual.widget.Widget)
│     ├── ListView of prompt entries
│     └── Always-pinned [LIVE] item at top
├── ConversationPanel  (textual.widget.Widget)
│     ├── RichLog (ANSI-capable scrollable log)
│     ├── mode: Literal["live", "filtered"]
│     └── filtered_id: int | None
├── InputBar  (textual.widget.Widget)
│     └── Input widget (disabled in filtered mode)
└── MessageStore
      ├── messages: list[Message]
      └── thread-safe append via asyncio.Queue
```

Textual messages (events):

- `NewMessage` — parser has completed a new Message, triggers left panel refresh
- `PtyOutput` — raw bytes from pty stdout, triggers right panel append in live mode
- `Resize` — terminal resized, propagated to pty via `TIOCSWINSZ` / ConPTY resize API

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `textual` | TUI framework (widgets, layout, events, ANSI support) |
| `pywinpty` | Windows ConPTY pseudo-terminal support |
| `ptyprocess` | macOS/Linux pty support |
| `rich` | ANSI/markup rendering (used internally by Textual) |
| `click` | CLI argument parsing |

All are pip-installable, MIT/Apache licensed.

Optional for distribution:
| Package | Purpose |
|---------|---------|
| `pyinstaller` | Package into a standalone `.exe` / binary |

---

## Configuration

Via CLI flags (v1, no config file needed):

| Flag | Default | Description |
|------|---------|-------------|
| `--history-width` | `26` | Width of the left panel in characters |
| `--claude-args` | _(none)_ | Extra arguments passed through to `claude` |

Example:
```
python uplink.py --history-width 30 --claude-args "--model claude-opus-4-6"
```

---

## Project Structure

```
claude-code-cli-enhanced/
├── SPEC.md
├── pyproject.toml          # dependencies + entry point
├── uplink/
│   ├── __init__.py
│   ├── __main__.py         # entry point: python -m uplink
│   ├── app.py              # Textual CchatApp definition
│   ├── widgets/
│   │   ├── history_panel.py
│   │   ├── conversation_panel.py
│   │   └── input_bar.py
│   ├── pty_manager.py      # spawns claude, reads pty, platform abstraction
│   ├── parser.py           # state machine output parser
│   └── store.py            # MessageStore dataclass
└── tests/
    ├── test_parser.py
    └── test_store.py
```

---

## Build & Distribution

```bash
# Install dependencies
pip install -e ".[dev]"

# Run directly
python -m uplink

# Run with args
python -m uplink --history-width 30

# Package to standalone exe (Windows)
pyinstaller --onefile --name uplink uplink/__main__.py
```

End users need only:
1. Python 3.11+ (or the packaged `.exe`)
2. `claude` (Claude Code) installed and authenticated

---

## Phased Delivery

### Phase 1 — Foundation
- Project scaffolding: `pyproject.toml`, package structure, `click` CLI entry point
- `pty_manager.py`: spawn `claude` in a pty, passthrough all I/O (no TUI yet)
- `parser.py`: state machine parser with unit tests
- `store.py`: MessageStore

### Phase 2 — TUI Shell
- Textual app layout: HistoryPanel + ConversationPanel + InputBar
- Wire pty stdout → ConversationPanel (LIVE mode only)
- Wire InputBar → pty stdin
- Correct terminal resize propagation

### Phase 3 — Filtered Mode
- Selecting a prompt in HistoryPanel filters ConversationPanel
- `[LIVE]` item and `L` keybinding to return to live
- InputBar disabled state in filtered mode

### Phase 4 — Polish
- `--history-width` and `--claude-args` flags
- Graceful quit (SIGTERM to claude child, clean pty teardown)
- PyInstaller packaging instructions
- README and install instructions

---

## Open Questions / Known Risks

1. **Parser fragility**: Detecting message boundaries from raw pty output is heuristic. If Claude Code changes its output format, the parser breaks. A future improvement would be to use Claude Code's `--output-format json` flag if/when it supports streaming JSON — eliminating the need for heuristic parsing entirely.

2. **ANSI passthrough fidelity**: Some Claude Code output uses complex ANSI sequences (cursor movement, clearing lines for spinners). Textual's `RichLog` handles most of this well, but cursor-movement sequences (used by spinners) may need to be filtered. Mitigation: strip `\r` and cursor-up/clear-line sequences before appending to the log.

3. **Input echo**: Claude Code echoes user input back to stdout. The parser must not double-count echoed input as a separate message. Mitigation: track last-submitted text and suppress matching echo lines in the parser.

4. **asyncio + Textual threading**: Textual runs its own event loop. The pty reader runs as an asyncio task within that loop using `asyncio.create_task`. Care is needed to avoid blocking the render loop — all pty reads must be non-blocking or run in a thread via `asyncio.to_thread`.
