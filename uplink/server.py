"""
Flask web server — serves the history viewer UI and JSON API.

Routes:
    GET  /                             → index.html
    GET  /api/sessions                 → list of sessions (summary)
    GET  /api/sessions/<id>            → full session with all messages
    GET  /api/stats/costly-prompts     → per-exchange token usage across all sessions
"""
import json
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from uplink import reader as _reader

# Populated by create_app().
_PROJECT_DIR: str = ""

app = Flask(__name__,
            template_folder=str(Path(__file__).parent / "templates"),
            static_folder=str(Path(__file__).parent / "static"))
app.config["JSON_SORT_KEYS"] = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_summary(s: _reader.Session) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "cwd": s.cwd,
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "message_count": len(s.messages),
        "prompt_count": len(s.user_prompts),
        "preview": s.preview,
        # First 120 chars of every user prompt — used by the UI to build
        # per-folder session thread trees (group continuation sessions together).
        "prompt_previews": [p.text[:120] for p in s.user_prompts],
        "is_imported": s.is_imported,
        "imported_from": s.imported_from,
    }


def _content_block_dict(b: _reader.ContentBlock) -> dict:
    return {"type": b.type, "text": b.text, "tool_name": b.tool_name}


def _message_dict(m: _reader.Message) -> dict:
    return {
        "uuid": m.uuid,
        "role": m.role,
        "timestamp": m.timestamp.isoformat(),
        "content": [_content_block_dict(b) for b in m.content],
        "text": m.text,
        "is_user_prompt": m.is_user_prompt,
        "model": m.model,
        "usage": m.usage,
    }


def _sidechain_dict(sc: _reader.SidechainInfo) -> dict:
    return {
        "id":                 sc.id,
        "slug":               sc.slug,
        "parent_prompt_uuid": sc.parent_prompt_uuid,
        "messages":           [_message_dict(m) for m in sc.messages],
    }


def _session_detail(s: _reader.Session) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "cwd": s.cwd,
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "messages": [_message_dict(m) for m in s.messages],
        "is_imported": s.is_imported,
        "imported_from": s.imported_from,
        "sidechains": [_sidechain_dict(sc)
                       for sc in _reader.find_sidechains_for_session(s)],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", project_dir=_PROJECT_DIR)


@app.route("/api/sessions")
def list_sessions():
    sessions = _reader.find_all_sessions()
    return jsonify([_session_summary(s) for s in sessions])


@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    return jsonify(_reader.search_sessions(query))


@app.route("/api/sessions/<session_id>")
def get_session(session_id: str):
    for s in _reader.find_all_sessions():
        if s.id == session_id:
            return jsonify(_session_detail(s))
    return jsonify({"error": "Session not found"}), 404


@app.route("/api/import", methods=["POST"])
def import_session():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400
    if data.get("_uplink", {}).get("format") != "session-export-v1":
        return jsonify({"error": "Not a valid Uplink export (missing _uplink.format)"}), 400

    session_id = data.get("session", {}).get("id", "")
    if not session_id:
        return jsonify({"error": "Export contains no session ID"}), 400

    import_dir = _reader.UPLINK_IMPORTS_DIR
    import_dir.mkdir(parents=True, exist_ok=True)
    dest = import_dir / f"{session_id}.json"
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _reader._invalidate_session_cache()
    return jsonify({"session_id": session_id, "status": "imported"})


@app.route("/api/import/<session_id>", methods=["DELETE"])
def delete_import(session_id: str):
    dest = _reader.UPLINK_IMPORTS_DIR / f"{session_id}.json"
    if dest.exists():
        dest.unlink()
        _reader._invalidate_session_cache()
        return jsonify({"status": "deleted"})
    return jsonify({"error": "Not found"}), 404


def _exchange_stats_row(session: _reader.Session, prompt_msg: _reader.Message,
                        assistant_msgs: list, prompt_index: int = 0) -> dict:
    usage: dict = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    model = ""
    for msg in assistant_msgs:
        u = msg.usage or {}
        usage["input_tokens"]                += u.get("input_tokens", 0)
        usage["output_tokens"]               += u.get("output_tokens", 0)
        usage["cache_read_input_tokens"]     += u.get("cache_read_input_tokens", 0)
        usage["cache_creation_input_tokens"] += u.get("cache_creation_input_tokens", 0)
        if not model and msg.model:
            model = msg.model

    # Context window size at this exchange: use the LAST assistant turn's token
    # counts (not summed) — that message had the fullest context window.
    context_tokens = 0
    if assistant_msgs:
        lu = assistant_msgs[-1].usage or {}
        context_tokens = (lu.get("input_tokens", 0)
                        + lu.get("cache_read_input_tokens", 0)
                        + lu.get("cache_creation_input_tokens", 0))

    return {
        "session_id":      session.id,
        "session_cwd":     session.cwd,
        "session_start":   session.start_time.isoformat() if session.start_time else None,
        "prompt_uuid":     prompt_msg.uuid,
        "prompt_text":     prompt_msg.text[:200],
        "prompt_index":    prompt_index,
        "model":           model,
        "usage":           usage,
        "context_tokens":  context_tokens,
    }


@app.route("/api/stats/costly-prompts")
def stats_costly_prompts():
    rows = []
    seen_prompt_uuids: set = set()
    for session in _reader.find_all_sessions():
        current_prompt = None
        asst_msgs: list = []
        prompt_index = 0
        for msg in session.messages:
            if msg.is_user_prompt:
                if current_prompt is not None:
                    if current_prompt.uuid not in seen_prompt_uuids:
                        seen_prompt_uuids.add(current_prompt.uuid)
                        rows.append(_exchange_stats_row(session, current_prompt, asst_msgs, prompt_index))
                    prompt_index += 1
                current_prompt = msg
                asst_msgs = []
            elif msg.role == "assistant" and current_prompt is not None:
                asst_msgs.append(msg)
        if current_prompt is not None:
            if current_prompt.uuid not in seen_prompt_uuids:
                seen_prompt_uuids.add(current_prompt.uuid)
                rows.append(_exchange_stats_row(session, current_prompt, asst_msgs, prompt_index))
    return jsonify(rows)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_app(project_dir: str) -> Flask:
    global _PROJECT_DIR
    _PROJECT_DIR = os.path.normpath(project_dir)
    return app
