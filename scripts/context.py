"""Read visible dialogue before one native user message, never later replies.

The phase-two window is bounded text evidence, not model instructions. Native
ordering wins over clocks. All application databases and logs are read only.
"""
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET


_SPEC = importlib.util.spec_from_file_location("ai_scold_context_sources", Path(__file__).with_name("sources.py"))
_S = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_S)

_VISIBLE_PHASES = {None, "final", "final_answer", "commentary"}
_TEXT_BLOCKS = {"text", "input_text", "output_text"}
_NON_TEXT_BLOCKS = {"tool_result", "tool_use", "thinking", "redacted_thinking", "image", "input_image", "localImage", "document", "file", "audio", "input_audio"}


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("Unsupported text content")
    pieces = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Unsupported content block")
        if block.get("type") in _TEXT_BLOCKS:
            if not isinstance(block.get("text"), str):
                raise ValueError("Invalid text block")
            pieces.append(block["text"])
        elif block.get("type") not in _NON_TEXT_BLOCKS:
            raise ValueError("Unknown content block")
    return "\n".join(pieces)


def _codex_heartbeat(item, text):
    """Recognize only the observed complete host-generated timer wrapper.

    User-owned quotes, ordinary prose and partial/lookalike XML remain evidence.
    Parsing is local; no content is executed or external entities resolved.
    """
    if item.get("type") != "userMessage" or "clientId" not in item or item["clientId"] is not None:
        return False
    content = item.get("content")
    if not isinstance(content, list) or len(content) != 1 or content[0].get("type") != "text":
        return False
    stripped = text.strip()
    if not stripped.startswith("<heartbeat>") or not stripped.endswith("</heartbeat>"):
        return False
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        root = ET.fromstring(stripped, parser=parser)
        children = list(root)
        if root.tag != "heartbeat" or root.attrib or (root.text or "").strip():
            return False
        if [child.tag for child in children] != ["automation_id", "current_time_iso", "instructions"]:
            return False
        if any(child.attrib or len(child) or not (child.text or "").strip() or (child.tail or "").strip() for child in children):
            return False
        _S._time(children[1].text.strip())
        return True
    except (ET.ParseError, ValueError, TypeError, OverflowError):
        return False


class _Window:
    def __init__(self, conversation, target):
        self.conversation = conversation
        self.target = target
        self.warnings = []
        self.rows = []

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def message(self, ident, role, stamp, text, ref):
        if not isinstance(ident, str) or not ident or role not in {"user", "assistant"}:
            self.warn(f"Missing or invalid context identity: {ref}")
            return None
        if not isinstance(text, str):
            self.warn(f"Unsupported context text: {ref}")
            return None
        if not text:
            return None
        try:
            occurred = _S._iso(_S._time(stamp))
        except (TypeError, ValueError, OverflowError):
            self.warn(f"Missing or invalid context timestamp: {ref}")
            return None
        return {"message_id": ident, "role": role, "occurred_at": occurred, "text": text, "source_ref": ref}

    def result(self, max_messages, max_chars):
        found = bool(self.rows and self.rows[-1]["message_id"] == self.target and self.rows[-1]["role"] == "user")
        truncated = False
        if not found:
            self.warn("Target human user message was not found in a supported source")
            rows = []
        else:
            rows = self.rows[-max_messages:]
            truncated = len(rows) < len(self.rows)
            target = dict(rows[-1])
            if len(target["text"]) > max_chars:
                target["text"] = target["text"][:max_chars]
                target["text_truncated"] = True
                rows = [target]
                truncated = True
                self.warn("Target text was truncated to max_chars; the original message is longer")
            else:
                retained = [target]
                used = len(target["text"])
                for row in reversed(rows[:-1]):
                    if used + len(row["text"]) > max_chars:
                        truncated = True
                        break
                    retained.append(row)
                    used += len(row["text"])
                rows = list(reversed(retained))
            if truncated and not target.get("text_truncated"):
                self.warn("Earlier messages were omitted by the configured context window")
        return {"messages": rows, "target_found": found, "coverage": {"complete": found and not self.warnings and not truncated, "warnings": self.warnings, "truncated": truncated}}


def _codex_message(row, path, window):
    ref = f"{path}#thread_items/{window.conversation}/{row['item_id']}"
    try:
        item = json.loads(row["item_json"])
        kind = row["item_type"]
        if not isinstance(item, dict) or item.get("type") != kind:
            raise ValueError("Mismatched item type")
        if _S._synthetic(item):
            return None
        role = "user" if kind == "userMessage" else "assistant"
        if item.get("role") not in {None, role}:
            raise ValueError("Mismatched message role")
        if role == "assistant" and item.get("phase") not in _VISIBLE_PHASES:
            return None
        text = _text(item.get("content")) if role == "user" else _text(item.get("text", item.get("content")))
        if role == "user" and _codex_heartbeat(item, text):
            return None
        at = row["created_at_ms"]
        if type(at) not in {int, float}:
            raise ValueError("Invalid timestamp")
        stamp = _S._iso(datetime.fromtimestamp(at / 1000, timezone.utc))
        return window.message(row["item_id"], role, stamp, text, ref)
    except (ValueError, TypeError, OverflowError, OSError):
        window.warn(f"Invalid Codex context record: {ref}")
        return None


def _paginated(root, window):
    path = _S._latest(root, "thread_history")
    if path is None:
        window.warn("Missing Codex thread history database")
        return
    with closing(_S._open_db(path)) as db:
        if not _S._schema(db, "thread_items", {"thread_id", "item_id", "created_at_ms", "item_json", "item_type"}):
            window.warn(f"Unsupported Codex context database schema: {path}")
            return
        has_order = _S._schema(db, "thread_items", {"rollout_ordinal"})
        fields = "item_id, created_at_ms, item_json, item_type" + (", rollout_ordinal" if has_order else "")
        targets = db.execute(f"SELECT {fields} FROM thread_items WHERE thread_id = ? AND item_id = ?", (window.conversation, window.target)).fetchall()
        if len(targets) != 1 or targets[0]["item_type"] != "userMessage":
            window.warn("Target is missing, ambiguous, or is not a Codex user message")
            return
        target = targets[0]
        target_message = _codex_message(target, path, window)
        if target_message is None:
            return
        ordered = has_order and type(target["rollout_ordinal"]) is int
        if ordered:
            # Strictly less excludes later records sharing the target's clock.
            sql = f"SELECT {fields} FROM thread_items WHERE thread_id = ? AND rollout_ordinal < ? AND item_type IN ('userMessage','agentMessage','assistantMessage') ORDER BY rollout_ordinal, item_id"
            args = (window.conversation, target["rollout_ordinal"])
            ambiguous = db.execute("SELECT count(*) FROM thread_items WHERE thread_id=? AND item_id != ? AND item_type IN ('userMessage','agentMessage','assistantMessage') AND (rollout_ordinal IS NULL OR typeof(rollout_ordinal) != 'integer' OR rollout_ordinal = ?)", (window.conversation, window.target, target["rollout_ordinal"])).fetchone()[0]
            if ambiguous:
                window.warn("Some Codex context has missing or ambiguous native ordering and was omitted")
        else:
            sql = f"SELECT {fields} FROM thread_items WHERE thread_id = ? AND created_at_ms < ? AND item_type IN ('userMessage','agentMessage','assistantMessage') ORDER BY created_at_ms, item_id"
            args = (window.conversation, target["created_at_ms"])
            window.warn("Codex native ordering is unavailable; only strictly earlier timestamps were used")
        seen = set()
        for row in db.execute(sql, args):
            if ordered and type(row["rollout_ordinal"]) is not int:
                continue
            message = _codex_message(row, path, window)
            if message and message["message_id"] not in seen and message["message_id"] != window.target:
                window.rows.append(message)
                seen.add(message["message_id"])
        window.rows.append(target_message)


def _legacy(path, window, trusted=False):
    human = trusted
    verified_conversation = trusted
    user_occurrences = {}
    assistant_occurrences = {}
    response_messages = []
    ordered = []
    found = False
    for number, row in _S._json_lines(path, window):
        ref = f"{path}:{number}"
        kind, payload = row.get("type"), row.get("payload")
        if kind == "session_meta":
            if not isinstance(payload, dict) or payload.get("id") != window.conversation:
                window.warn(f"Codex legacy conversation identity does not match: {ref}")
                return
            verified_conversation = True
            if not trusted or payload.get("source") is not None or payload.get("agent_path") is not None:
                human = _S._human_codex(payload.get("source"), payload.get("agent_path"), window, ref)
            if not human:
                return
            continue
        if not human or not verified_conversation or not isinstance(payload, dict):
            continue
        role = None
        representation = None
        if kind == "event_msg" and payload.get("type") == "user_message":
            role, representation = "user", "user"
            content = payload.get("message")
        elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
            if payload.get("phase") not in _VISIBLE_PHASES or payload.get("channel") not in _VISIBLE_PHASES:
                continue
            role, representation = "assistant", "response"
            content = payload.get("content")
        elif kind == "event_msg" and payload.get("type") == "agent_message":
            if payload.get("phase") not in _VISIBLE_PHASES or payload.get("channel") not in _VISIBLE_PHASES:
                continue
            role, representation = "assistant", "event"
            content = payload.get("message")
        if role is None:
            continue
        try:
            stamp = _S._iso(_S._time(row.get("timestamp")))
        except (TypeError, ValueError, OverflowError):
            window.warn(f"Invalid Codex legacy context record: {ref}")
            continue
        occurrences = user_occurrences if role == "user" else assistant_occurrences
        occurrence_key = (stamp, representation)
        count = occurrences.get(occurrence_key, 0) + 1
        occurrences[occurrence_key] = count
        prefix = "legacy" if role == "user" else f"legacy-assistant-{representation}"
        ident = payload.get("message_id") or payload.get("id") or f"{prefix}:{stamp}:{count}"
        try:
            text = _text(content)
        except (TypeError, ValueError):
            window.warn(f"Invalid Codex legacy context text: {ref}")
            if ident == window.target:
                break
            continue
        message = window.message(ident, role, stamp, text, ref)
        if message:
            ordered.append((representation, message))
            if representation == "response":
                response_messages.append(message)
        if ident == window.target:
            found = role == "user" and message is not None
            break
    if found:
        # Keep event-only visible replies in mixed-version logs. Remove an
        # event echo only when its time and text match a canonical response.
        response_keys = {(message["occurred_at"], message["text"]) for message in response_messages}
        seen = set()
        for kind, message in ordered:
            if (kind == "event" and (message["occurred_at"], message["text"]) in response_keys) or message["message_id"] in seen:
                continue
            window.rows.append(message)
            seen.add(message["message_id"])


def _codex(root, window):
    state = _S._latest(root, "state")
    if state is None:
        window.warn("Missing Codex state index; only matching legacy filenames can be checked")
        for base in (root / "sessions", root / "archived_sessions"):
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.jsonl")):
                if window.conversation in path.stem and not path.is_symlink():
                    _legacy(path, window)
                    if window.rows:
                        return
        return
    with closing(_S._open_db(state)) as db:
        if not _S._schema(db, "threads", {"id", "source", "agent_path", "history_mode", "rollout_path"}):
            window.warn(f"Unsupported Codex context index schema: {state}")
            return
        thread = db.execute("SELECT id, source, agent_path, history_mode, rollout_path FROM threads WHERE id = ?", (window.conversation,)).fetchone()
    if thread is None:
        window.warn("Requested conversation is missing from the Codex index")
        return
    if not _S._human_codex(thread["source"], thread["agent_path"], window, f"{state}#threads/{window.conversation}"):
        window.warn("Requested Codex conversation is not a supported human conversation")
        return
    if thread["history_mode"] == "paginated":
        _paginated(root, window)
    elif thread["history_mode"] == "legacy" and isinstance(thread["rollout_path"], str) and thread["rollout_path"]:
        path = Path(thread["rollout_path"]).expanduser()
        _legacy(path if path.is_absolute() else root / path, window, trusted=True)
    else:
        window.warn("Unsupported Codex history mode or missing legacy path")


def _claude_message(row, ref, window):
    if _S._synthetic(row) or row.get("type") not in {"user", "assistant"}:
        return None
    message = row.get("message")
    if not isinstance(message, dict) or message.get("role") != row["type"]:
        window.warn(f"Unsupported or mismatched Claude Code context role: {ref}")
        return None
    try:
        text = _text(message.get("content"))
    except (TypeError, ValueError):
        window.warn(f"Unsupported Claude Code context text: {ref}")
        return None
    return window.message(row.get("uuid"), row["type"], row.get("timestamp"), text, ref)


def _claude_file(path, window):
    records = {}
    target = None
    for number, row in _S._json_lines(path, window):
        if row.get("sessionId") != window.conversation:
            continue
        ident = row.get("uuid")
        if not isinstance(ident, str) or not ident:
            continue
        ref = f"{path}:{number}"
        if ident in records:
            if records[ident][0] != row:
                window.warn(f"Conflicting Claude Code context identity: {ref}")
            continue
        records[ident] = (row, ref)
        if ident == window.target:
            target = row
            break
    if target is None:
        return False
    target_message = _claude_message(target, records[window.target][1], window)
    if target_message is None or target_message["role"] != "user":
        return True
    if "parentUuid" not in target:
        window.warn("Claude Code branch ancestry is unavailable; only the file prefix was used")
        selected = list(records.values())
    else:
        selected = []
        ident = window.target
        seen = set()
        while ident is not None:
            if not isinstance(ident, str) or ident in seen:
                window.warn("Claude Code context has invalid or cyclic branch ancestry")
                break
            if ident not in records:
                window.warn("Claude Code context has a missing ancestor; other branches were not substituted")
                break
            seen.add(ident)
            row, ref = records[ident]
            selected.append((row, ref))
            if "parentUuid" not in row:
                window.warn("Claude Code context has incomplete branch ancestry")
                break
            ident = row["parentUuid"]
        selected.reverse()
    for row, ref in selected:
        message = _claude_message(row, ref, window)
        if message:
            window.rows.append(message)
    return True


def _claude(root, window):
    projects = root if root.name == "projects" else root / "projects"
    if not projects.is_dir():
        window.warn("Missing Claude Code projects directory")
        return
    # Native Claude session filenames carry the exact session ID. Do not read
    # neighbouring private conversations as a fallback when the file is absent.
    paths = sorted(path for project in projects.iterdir() if project.is_dir() and not project.is_symlink() for path in project.glob("*.jsonl") if path.name == f"{window.conversation}.jsonl" and path.is_file() and not path.is_symlink())
    if not paths:
        window.warn("Claude Code conversation file was not found")
    for path in paths:
        if _claude_file(path, window):
            return


def read_context(platform, source_root, conversation_id, message_id, *, max_messages=12, max_chars=16000):
    """Return a bounded visible-text window ending at the given human message.

    Caller argument errors raise ValueError. Missing or unsupported native data
    yields explicit coverage warnings. ``max_chars`` is a hard text-character
    bound; an oversized target is visibly marked as truncated. ``complete``
    never implies that deleted native history or images have been recovered.
    """
    if platform not in {"codex", "claude-code"}:
        raise ValueError("platform must be 'codex' or 'claude-code'")
    if any(not isinstance(v, str) or not v for v in (conversation_id, message_id)):
        raise ValueError("conversation_id and message_id must be nonempty strings")
    if any(type(v) is not int or v < 1 for v in (max_messages, max_chars)):
        raise ValueError("max_messages and max_chars must be positive integers")
    window = _Window(conversation_id, message_id)
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        window.warn("Source root is missing or unreadable")
    else:
        try:
            (_codex if platform == "codex" else _claude)(root, window)
        except (OSError, sqlite3.Error):
            window.warn("Native context source is unreadable or changed during the scan")
    return window.result(max_messages, max_chars)
