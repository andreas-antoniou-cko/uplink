"""Tests for the session file reader."""
import json
import pytest
from datetime import timezone
from pathlib import Path
import tempfile
import os

from uplink.reader import (
    parse_session_file,
    _parse_content,
    ContentBlock,
    Session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def make_record(role: str, content, cwd: str = "/test/project", ts: str = "2024-01-01T10:00:00.000Z", uuid: str = "abc") -> dict:
    return {
        "type": role,
        "message": {"role": role, "content": content},
        "cwd": cwd,
        "timestamp": ts,
        "uuid": uuid,
    }


# ---------------------------------------------------------------------------
# _parse_content
# ---------------------------------------------------------------------------

class TestParseContent:
    def test_plain_string(self):
        blocks, is_prompt = _parse_content("hello world")
        assert len(blocks) == 1
        assert blocks[0].type == "text"
        assert blocks[0].text == "hello world"
        assert is_prompt is True

    def test_text_block_list(self):
        blocks, is_prompt = _parse_content([{"type": "text", "text": "hi"}])
        assert blocks[0].type == "text"
        assert blocks[0].text == "hi"
        assert is_prompt is True

    def test_tool_use_block(self):
        blocks, _ = _parse_content([{
            "type": "tool_use",
            "name": "Bash",
            "input": {"command": "ls"},
        }])
        assert blocks[0].type == "tool_use"
        assert blocks[0].tool_name == "Bash"
        assert "ls" in blocks[0].text

    def test_tool_result_is_not_user_prompt(self):
        blocks, is_prompt = _parse_content([{
            "type": "tool_result",
            "tool_use_id": "x",
            "content": [{"type": "text", "text": "result"}],
        }])
        assert blocks[0].type == "tool_result"
        assert is_prompt is False

    def test_tool_result_string_content(self):
        blocks, _ = _parse_content([{
            "type": "tool_result",
            "content": "plain string result",
        }])
        assert blocks[0].text == "plain string result"

    def test_mixed_blocks_still_prompt_if_has_text(self):
        blocks, is_prompt = _parse_content([
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "Bash", "input": {}},
        ])
        assert is_prompt is True

    def test_empty_list(self):
        blocks, is_prompt = _parse_content([])
        assert blocks == []


# ---------------------------------------------------------------------------
# parse_session_file
# ---------------------------------------------------------------------------

class TestParseSessionFile:
    def test_basic_conversation(self, tmp_path):
        f = tmp_path / "session1.jsonl"
        write_jsonl(f, [
            make_record("user", "hello", uuid="u1"),
            make_record("assistant", "hi there", uuid="a1"),
        ])
        session = parse_session_file(f)
        assert session is not None
        assert session.id == "session1"
        assert len(session.messages) == 2

    def test_cwd_extracted(self, tmp_path):
        f = tmp_path / "s.jsonl"
        write_jsonl(f, [make_record("user", "q", cwd="/my/project")])
        session = parse_session_file(f)
        assert session.cwd == "/my/project"

    def test_user_prompt_flag(self, tmp_path):
        f = tmp_path / "s.jsonl"
        write_jsonl(f, [
            make_record("user", "real question", uuid="u1"),
            make_record("assistant", "answer", uuid="a1"),
        ])
        session = parse_session_file(f)
        user_msg = next(m for m in session.messages if m.role == "user")
        assert user_msg.is_user_prompt is True

    def test_tool_result_not_flagged_as_prompt(self, tmp_path):
        f = tmp_path / "s.jsonl"
        tool_result_content = [{"type": "tool_result", "content": "ok"}]
        write_jsonl(f, [
            make_record("user", "real q", uuid="u1"),
            make_record("user", tool_result_content, uuid="tool1"),
        ])
        session = parse_session_file(f)
        tool_msg = next(m for m in session.messages if m.uuid == "tool1")
        assert tool_msg.is_user_prompt is False

    def test_returns_none_for_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert parse_session_file(f) is None

    def test_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "s.jsonl"
        with open(f, "w") as fh:
            fh.write("not json\n")
            fh.write(json.dumps(make_record("user", "hello")) + "\n")
        session = parse_session_file(f)
        assert session is not None
        assert len(session.messages) == 1

    def test_skips_non_user_assistant_types(self, tmp_path):
        f = tmp_path / "s.jsonl"
        write_jsonl(f, [
            {"type": "summary", "content": "summary text", "cwd": "/p"},
            make_record("user", "hello"),
        ])
        session = parse_session_file(f)
        assert len(session.messages) == 1

    def test_message_text_property(self, tmp_path):
        f = tmp_path / "s.jsonl"
        write_jsonl(f, [make_record("user", "what is 2+2?")])
        session = parse_session_file(f)
        assert session.messages[0].text == "what is 2+2?"

    def test_session_preview(self, tmp_path):
        f = tmp_path / "s.jsonl"
        write_jsonl(f, [
            make_record("user", "explain the architecture"),
            make_record("assistant", "Sure, here is the explanation"),
        ])
        session = parse_session_file(f)
        assert "explain" in session.preview

    def test_start_time_parsed(self, tmp_path):
        f = tmp_path / "s.jsonl"
        write_jsonl(f, [make_record("user", "hi", ts="2024-06-15T09:30:00.000Z")])
        session = parse_session_file(f)
        assert session.start_time is not None
        assert session.start_time.year == 2024
        assert session.start_time.month == 6
        assert session.start_time.day == 15

    def test_user_prompts_property(self, tmp_path):
        f = tmp_path / "s.jsonl"
        tool_result = [{"type": "tool_result", "content": "ok"}]
        write_jsonl(f, [
            make_record("user", "q1", uuid="u1"),
            make_record("user", tool_result, uuid="tool1"),
            make_record("user", "q2", uuid="u2"),
        ])
        session = parse_session_file(f)
        prompts = session.user_prompts
        assert len(prompts) == 2
        assert all(m.is_user_prompt for m in prompts)
