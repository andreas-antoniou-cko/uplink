"""
Session reader — parses Claude Code's JSONL session files.

Claude Code writes one JSONL file per session under:
    ~/.claude/projects/<encoded-path>/<session-id>.jsonl

Each line is a JSON record. The records we care about have:
    type      : "user" | "assistant"
    message   : { role, content }
    timestamp : ISO 8601 string
    cwd       : absolute path of the project directory
    uuid      : unique message id

The message.content field can be:
    - A plain string
    - A list of content blocks: text | tool_use | tool_result
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
UPLINK_IMPORTS_DIR = Path.home() / ".uplink" / "imports"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ContentBlock:
    type: str  # "text" | "tool_use" | "tool_result"
    text: str = ""
    tool_name: str = ""


@dataclass
class Message:
    uuid: str
    role: str  # "user" | "assistant"
    content: list[ContentBlock]
    timestamp: datetime
    is_user_prompt: bool = False  # True for real user turns (not tool results)
    model: str = ""
    usage: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Plain text content — joins all text blocks."""
        return "\n".join(b.text for b in self.content if b.type == "text").strip()


@dataclass
class Session:
    id: str
    filepath: Path
    cwd: str
    messages: list[Message] = field(default_factory=list)
    is_imported: bool = False
    imported_from: str = ""  # original cwd recorded at export time
    name: str | None = None

    @property
    def start_time(self) -> datetime | None:
        return self.messages[0].timestamp if self.messages else None

    @property
    def user_prompts(self) -> list[Message]:
        return [m for m in self.messages if m.is_user_prompt]

    @property
    def preview(self) -> str:
        prompts = self.user_prompts
        return prompts[0].text[:80] if prompts else ""


@dataclass
class SidechainInfo:
    """
    A /btw aside-conversation spawned from a prompt inside a parent session.

    Claude Code stores these under:
        ~/.claude/projects/<encoded-cwd>/<parent-session-uuid>/subagents/<agent-id>.jsonl

    The records carry isSidechain=true, sessionId (= parent session UUID) and
    promptId (= uuid of the user prompt in the parent session that triggered the aside).
    """

    id: str  # agent id extracted from records (or filename stem)
    slug: str  # human-readable name, e.g. "compiled-chasing-shore"
    filepath: Path
    parent_session_id: str  # sessionId field from records
    parent_prompt_uuid: str  # promptId field — links to the triggering user prompt
    messages: list[Message]
    cwd: str

    @property
    def start_time(self) -> datetime | None:
        return self.messages[0].timestamp if self.messages else None

    @property
    def user_prompts(self) -> list[Message]:
        return [m for m in self.messages if m.is_user_prompt]

    @property
    def preview(self) -> str:
        prompts = self.user_prompts
        return prompts[0].text[:80] if prompts else ""


# ---------------------------------------------------------------------------
# Content parser
# ---------------------------------------------------------------------------


def _parse_content(raw) -> tuple[list[ContentBlock], bool]:
    """
    Parse raw message content into ContentBlocks.
    Returns (blocks, is_user_prompt).
    is_user_prompt is False when the entire content is tool_result blocks
    (these are API-level messages, not real user turns).
    """
    if isinstance(raw, str):
        return [ContentBlock(type="text", text=raw)], True

    if not isinstance(raw, list):
        return [], False

    blocks: list[ContentBlock] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        btype = item.get("type", "text")

        if btype == "text":
            blocks.append(ContentBlock(type="text", text=item.get("text", "")))

        elif btype == "tool_use":
            input_data = item.get("input", {})
            try:
                text = json.dumps(input_data, indent=2)
            except Exception:
                text = str(input_data)
            blocks.append(
                ContentBlock(
                    type="tool_use",
                    tool_name=item.get("name", "unknown"),
                    text=text,
                )
            )

        elif btype == "tool_result":
            result_content = item.get("content", "")
            if isinstance(result_content, list):
                text = "\n".join(
                    r.get("text", "")
                    for r in result_content
                    if isinstance(r, dict) and r.get("type") == "text"
                )
            elif isinstance(result_content, str):
                text = result_content
            else:
                text = str(result_content)
            blocks.append(ContentBlock(type="tool_result", text=text))

    # If every block is a tool_result, this is not a real user-initiated turn.
    is_user_prompt = not (blocks and all(b.type == "tool_result" for b in blocks))
    return blocks, is_user_prompt


# ---------------------------------------------------------------------------
# File parser
# ---------------------------------------------------------------------------


def _parse_timestamp(ts_str: str) -> datetime:
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


def parse_session_file(filepath: Path) -> Session | None:
    """Parse a single JSONL session file into a Session object."""
    messages: list[Message] = []
    cwd = ""
    session_name = None

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rec_type = record.get("type", "")
                if rec_type == "custom-title":
                    session_name = record.get("customTitle", None)

                # Grab cwd from the first record that has it.
                if not cwd:
                    cwd = record.get("cwd", "")

                if rec_type not in ("user", "assistant"):
                    continue

                msg_data = record.get("message", {})
                if not isinstance(msg_data, dict):
                    continue

                role = msg_data.get("role", rec_type)
                raw_content = msg_data.get("content", "")
                blocks, is_prompt = _parse_content(raw_content)

                # Only mark user-role records as prompts.
                is_user_prompt = (role == "user") and is_prompt

                messages.append(
                    Message(
                        uuid=record.get("uuid", ""),
                        role=role,
                        content=blocks,
                        timestamp=_parse_timestamp(record.get("timestamp", "")),
                        is_user_prompt=is_user_prompt,
                        model=msg_data.get("model", ""),
                        usage=msg_data.get("usage") or record.get("usage") or {},
                    )
                )
    except Exception:
        return None

    if not messages:
        return None

    return Session(id=filepath.stem, filepath=filepath, cwd=cwd, messages=messages, name=session_name)


def _get_file_cwd(filepath: Path) -> str:
    """
    Quickly extract the cwd from the first ~20 lines of a session file.
    Used to skip files that don't match the target directory without fully parsing them.
    """
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 20:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    cwd = record.get("cwd", "")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_sessions(project_dir: str) -> list[Session]:
    """
    Find and return all sessions whose cwd matches project_dir.
    Results are sorted newest-first.
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        return []

    norm_target = _norm_path(project_dir)
    sessions: list[Session] = []
    seen_ids: set[str] = set()

    for jsonl_file in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        if _is_sidechain_path(jsonl_file):
            continue  # sidechains are loaded on-demand via find_sidechains_for_session
        # Fast check: only read full file if cwd matches.
        file_cwd = _get_file_cwd(jsonl_file)
        if file_cwd and _norm_path(file_cwd) != norm_target:
            continue

        session = parse_session_file(jsonl_file)
        if session is None:
            continue

        # Double-check cwd from full parse (it may be in a later line).
        if session.cwd and _norm_path(session.cwd) != norm_target:
            continue

        if session.id not in seen_ids:
            seen_ids.add(session.id)
            sessions.append(session)

    sessions.sort(key=lambda s: s.start_time or datetime.min, reverse=True)
    return _collapse_continuation_sessions(sessions)


def search_sessions(query: str, max_results: int = 100) -> list[dict]:
    """
    Full-text search across all sessions.
    Returns a flat list of matching messages, newest sessions first.
    Each result includes the enclosing prompt UUID so the UI can jump to
    the right exchange regardless of whether the match is in a user or
    assistant message.
    """
    if not CLAUDE_PROJECTS_DIR.exists() or not query:
        return []

    query_lower = query.lower()
    results: list[dict] = []
    seen_ids: set[str] = set()
    seen_msg_uuids: set[str] = set()

    for jsonl_file in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        session = parse_session_file(jsonl_file)
        if session is None or session.id in seen_ids:
            continue
        seen_ids.add(session.id)

        # Track the most recent user-prompt UUID so assistant matches can
        # reference their enclosing exchange.
        current_prompt_uuid: str | None = None

        for msg in session.messages:
            if msg.is_user_prompt:
                current_prompt_uuid = msg.uuid

            if query_lower not in msg.text.lower():
                continue

            if msg.uuid and msg.uuid in seen_msg_uuids:
                continue
            if msg.uuid:
                seen_msg_uuids.add(msg.uuid)

            results.append(
                {
                    "session_id": session.id,
                    "session_cwd": session.cwd,
                    "session_start": session.start_time.isoformat()
                    if session.start_time
                    else None,
                    "message_uuid": msg.uuid,
                    "prompt_uuid": current_prompt_uuid,
                    "role": msg.role,
                    "is_user_prompt": msg.is_user_prompt,
                    "snippet": _snippet(msg.text, query_lower),
                }
            )

    # Newest sessions first, user prompts before assistant messages within a session.
    results.sort(key=lambda r: (r.get("session_start") or ""), reverse=True)
    return results[:max_results]


def _snippet(text: str, query: str, context: int = 120) -> str:
    """Return a short excerpt of text centred on the first occurrence of query."""
    idx = text.lower().find(query)
    if idx == -1:
        return text[:context]
    start = max(0, idx - context // 2)
    end = min(len(text), idx + len(query) + context // 2)
    out = text[start:end].replace("\n", " ")
    if start > 0:
        out = "…" + out
    if end < len(text):
        out = out + "…"
    return out


def parse_imported_json(filepath: Path) -> "Session | None":
    """Parse an Uplink JSON export file back into a Session."""
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        if data.get("_uplink", {}).get("format") != "session-export-v1":
            return None

        sd = data.get("session", {})
        messages: list[Message] = []
        for m in sd.get("messages", []):
            raw = m.get("content", [])
            # content is already serialised as a list of dicts from our own export
            blocks: list[ContentBlock] = []
            if isinstance(raw, list):
                for b in raw:
                    if isinstance(b, dict):
                        blocks.append(
                            ContentBlock(
                                type=b.get("type", "text"),
                                text=b.get("text", ""),
                                tool_name=b.get("tool_name", ""),
                            )
                        )
            messages.append(
                Message(
                    uuid=m.get("uuid", ""),
                    role=m.get("role", "user"),
                    content=blocks,
                    timestamp=_parse_timestamp(m.get("timestamp", "")),
                    is_user_prompt=m.get("is_user_prompt", False),
                    model=m.get("model", ""),
                    usage=m.get("usage") or {},
                )
            )

        if not messages:
            return None

        original_cwd = sd.get("cwd", "")
        return Session(
            id=sd.get("id", filepath.stem),
            filepath=filepath,
            cwd=original_cwd,
            messages=messages,
            is_imported=True,
            imported_from=original_cwd,
            name=sd.get("name"),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Session cache — avoids re-parsing every JSONL file on every HTTP request.
# The frontend auto-refreshes every 5 s; without a cache each poll triggers
# a full directory scan which keeps growing the process heap faster than GC
# can collect it, eventually causing MemoryError.
# ---------------------------------------------------------------------------
_SESSION_CACHE_TTL = 8  # seconds
_session_cache: list["Session"] = []
_session_cache_ts: float = 0.0


def _invalidate_session_cache() -> None:
    """Force the next find_all_sessions() call to re-scan the filesystem."""
    global _session_cache_ts
    _session_cache_ts = 0.0


def _build_sessions() -> list["Session"]:
    sessions: list[Session] = []
    seen_ids: set[str] = set()

    if CLAUDE_PROJECTS_DIR.exists():
        for jsonl_file in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
            if _is_sidechain_path(jsonl_file):
                continue  # sidechains are loaded on-demand via find_sidechains_for_session
            session = parse_session_file(jsonl_file)
            if session is not None and session.id not in seen_ids:
                seen_ids.add(session.id)
                sessions.append(session)

    if UPLINK_IMPORTS_DIR.exists():
        for json_file in UPLINK_IMPORTS_DIR.glob("*.json"):
            session = parse_imported_json(json_file)
            if session is not None and session.id not in seen_ids:
                seen_ids.add(session.id)
                sessions.append(session)

    sessions.sort(key=lambda s: s.start_time or datetime.min, reverse=True)
    return _collapse_continuation_sessions(sessions)


def find_all_sessions() -> list[Session]:
    """
    Return every session across all projects plus any imported sessions,
    sorted newest-first.  Results are cached for _SESSION_CACHE_TTL seconds
    to prevent repeated full-filesystem scans on every API poll.
    """
    global _session_cache, _session_cache_ts
    now = time.monotonic()
    if now - _session_cache_ts > _SESSION_CACHE_TTL:
        _session_cache = _build_sessions()
        _session_cache_ts = now
    return _session_cache


def _is_sidechain_path(filepath: Path) -> bool:
    """
    Return True for JSONL files that live inside a *subagents/* directory.
    These are /btw aside-conversations, not top-level sessions.
    """
    return "subagents" in filepath.parts


def parse_sidechain_file(filepath: Path) -> "SidechainInfo | None":
    """Parse a sidechain JSONL file (isSidechain=true records) into a SidechainInfo."""
    messages: list[Message] = []
    agent_id = ""
    slug = ""
    parent_session_id = ""
    parent_prompt_uuid = ""
    cwd = ""

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not agent_id:
                    agent_id = record.get("agentId", "")
                if not slug:
                    slug = record.get("slug", "")
                if not parent_session_id:
                    parent_session_id = record.get("sessionId", "")
                if not parent_prompt_uuid:
                    parent_prompt_uuid = record.get("promptId", "")
                if not cwd:
                    cwd = record.get("cwd", "")

                rec_type = record.get("type", "")
                if rec_type not in ("user", "assistant"):
                    continue

                msg_data = record.get("message", {})
                if not isinstance(msg_data, dict):
                    continue

                role = msg_data.get("role", rec_type)
                raw_content = msg_data.get("content", "")
                blocks, is_prompt = _parse_content(raw_content)

                messages.append(
                    Message(
                        uuid=record.get("uuid", ""),
                        role=role,
                        content=blocks,
                        timestamp=_parse_timestamp(record.get("timestamp", "")),
                        is_user_prompt=(role == "user") and is_prompt,
                        model=msg_data.get("model", ""),
                        usage=msg_data.get("usage") or record.get("usage") or {},
                    )
                )
    except Exception:
        return None

    if not messages or not parent_session_id:
        return None

    return SidechainInfo(
        id=agent_id or filepath.stem,
        slug=slug,
        filepath=filepath,
        parent_session_id=parent_session_id,
        parent_prompt_uuid=parent_prompt_uuid,
        messages=messages,
        cwd=cwd,
    )


def find_sidechains_for_session(session: "Session") -> "list[SidechainInfo]":
    """
    Return all sidechain conversations belonging to a session.
    They live in: <session.filepath.parent>/<session.id>/subagents/*.jsonl
    """
    subagents_dir = session.filepath.parent / session.id / "subagents"
    if not subagents_dir.exists():
        return []
    result: list[SidechainInfo] = []
    for jsonl_file in subagents_dir.glob("*.jsonl"):
        sc = parse_sidechain_file(jsonl_file)
        if sc is not None:
            result.append(sc)
    return result


def _norm_path(path: str) -> str:
    """Normalise a path for cross-platform comparison."""
    return os.path.normcase(os.path.normpath(path))


def _collapse_continuation_sessions(sessions: list[Session]) -> list[Session]:
    """
    When Claude Code exhausts a context window it creates a new JSONL file that
    replays the entire prior conversation.  Those "continuation" files share the
    same prompt UUIDs as the session(s) they continue, but contain more messages.

    Detect them by checking strict-subset relationships among prompt-UUID sets
    and keep only the most-complete (superset) session for each logical
    conversation thread.  Sessions with no user-prompt UUIDs are always kept.
    """
    if len(sessions) <= 1:
        return sessions

    prompt_sets: list[set[str]] = [
        {m.uuid for m in s.messages if m.is_user_prompt and m.uuid} for s in sessions
    ]

    discard: list[bool] = [False] * len(sessions)
    for i in range(len(sessions)):
        if not prompt_sets[i]:
            continue  # no UUIDs — can't determine relationship, always keep
        for j in range(len(sessions)):
            if i == j or discard[j]:
                continue
            if (
                prompt_sets[i] < prompt_sets[j]
            ):  # strict subset → i is a continuation stub
                discard[i] = True
                break

    return [s for s, drop in zip(sessions, discard) if not drop]
