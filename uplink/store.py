"""
Message store — thread-safe ordered list of conversation messages.
"""
import threading
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
    content: str
    timestamp: datetime = field(default_factory=datetime.now)

    def preview(self, max_chars: int = 40) -> str:
        """Return a single-line ANSI-stripped preview of the content."""
        from uplink.parser import strip_ansi
        text = strip_ansi(self.content).replace("\n", " ").strip()
        if len(text) > max_chars:
            return text[:max_chars - 1] + "…"
        return text


class MessageStore:
    """Thread-safe, ordered list of Messages."""

    def __init__(self) -> None:
        self._messages: list[Message] = []
        self._lock = threading.Lock()
        self._next_id = 1

    def add(self, role: Role, content: str) -> Message:
        with self._lock:
            msg = Message(id=self._next_id, role=role, content=content)
            self._next_id += 1
            self._messages.append(msg)
            return msg

    def all(self) -> list[Message]:
        with self._lock:
            return list(self._messages)

    def user_messages(self) -> list[Message]:
        with self._lock:
            return [m for m in self._messages if m.role == Role.USER]

    def for_prompt(self, prompt_id: int) -> list[Message]:
        """Return the user message with the given id and the assistant reply that follows it."""
        with self._lock:
            result: list[Message] = []
            collecting = False
            for msg in self._messages:
                if msg.id == prompt_id:
                    result.append(msg)
                    collecting = True
                elif collecting:
                    if msg.role == Role.ASSISTANT:
                        result.append(msg)
                        break
                    else:
                        # Another user message before an assistant reply — stop.
                        break
            return result

    def append_to_last_assistant(self, chunk: str) -> Message | None:
        """Append a chunk to the last assistant message, or create one if none exists."""
        with self._lock:
            for msg in reversed(self._messages):
                if msg.role == Role.ASSISTANT:
                    msg.content += chunk
                    return msg
            # No assistant message yet — create one.
            msg = Message(id=self._next_id, role=Role.ASSISTANT, content=chunk)
            self._next_id += 1
            self._messages.append(msg)
            return msg
