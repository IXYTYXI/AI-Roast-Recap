"""Read native Codex / Claude Code human messages without modifying the sources.

Only the standard library is required. The collector never prints message text.
``coverage.complete`` describes supported, readable sources encountered during
this scan; it cannot prove that the applications retained all historical logs.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3


UTC = timezone.utc
_HUMAN_CODEX_SOURCES = {"cli", "vscode", "app", "desktop"}
_SYNTHETIC = {"synthetic", "system", "hook", "agent", "subagent", "internal", "automation", "automated", "compaction", "compact", "tool", "tool_result"}
_CLAUDE_NONMESSAGE_TYPES = {
    "assistant", "system", "summary", "progress", "queue-operation",
    "file-history-snapshot", "last-prompt", "custom-title", "tag",
    "agent-name", "agent-color", "attribution-snapshot", "mode",
    "permission-mode", "atis-latch", "attachment", "ai-title", "cost-state",
}


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Expected an ISO timestamp with a timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return result.astimezone(UTC)


def _iso(value):
    return value.isoformat(timespec="microseconds" if value.microsecond else "seconds").replace("+00:00", "Z")


class _Collection:
    def __init__(self, platform, since, until):
        self.platform = platform
        self.since = since
        self.until = until
        self.messages = {}
        self.warnings = []
        self.scanned = set()

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def add(self, conversation, message, occurred, text, source):
        if not isinstance(text, str):
            self.warn(f"Unsupported message text schema: {source}")
            return
        if not text or occurred < self.since or (self.until is not None and occurred >= self.until):
            return
        if not conversation or not message:
            self.warn(f"Missing conversation or message identity: {source}")
            return
        key = (str(conversation), str(message))
        self.messages.setdefault(key, {"platform": self.platform, "conversation_id": str(conversation), "message_id": str(message), "occurred_at": _iso(occurred), "text": text, "source_ref": str(source)})

    def result(self):
        # Sorting datetime values avoids lexical anomalies with fractional seconds.
        messages = sorted(self.messages.values(), key=lambda row: (_time(row["occurred_at"]), row["conversation_id"], row["message_id"]))
        return {"messages": messages, "coverage": {"complete": not self.warnings, "warnings": self.warnings, "scanned_conversations": len(self.scanned)}}


def _latest(root, prefix):
    versions = []
    for path in root.glob(f"{prefix}_*.sqlite"):
        match = re.fullmatch(re.escape(prefix) + r"_(\d+)\.sqlite", path.name)
        if match and path.is_file():
            versions.append((int(match.group(1)), path))
    return max(versions, default=(0, None), key=lambda pair: pair[0])[1]


def _open_db(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        return db
    except Exception:
        db.close()
        raise


def _schema(db, table, columns):
    # Table names here are constants, never source content or user input.
    actual = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    return set(columns).issubset(actual)


def _human_codex(source, agent_path, collector, ref):
    if agent_path is not None:
        return False
    if isinstance(source, str):
        if source in _HUMAN_CODEX_SOURCES:
            return True
        try:
            source = json.loads(source)
        except (ValueError, TypeError):
            collector.warn(f"Unknown Codex conversation source; omitted: {ref}")
            return False
    if isinstance(source, dict):
        # Codex nested source metadata explicitly identifies spawned agents.
        def subagent(value):
            if isinstance(value, dict):
                return any(key in {"subagent", "thread_spawn"} or subagent(item) for key, item in value.items())
            if isinstance(value, list):
                return any(subagent(item) for item in value)
            return False
        if subagent(source):
            return False
    collector.warn(f"Unknown Codex conversation source; omitted: {ref}")
    return False


def _text(content):
    """Preserve text block contents verbatim; never turn tool payloads into text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("Unsupported content schema")
    pieces = []
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ValueError("Unsupported content block schema")
        if block["type"] in {"text", "input_text"}:
            if not isinstance(block.get("text"), str):
                raise ValueError("Unsupported text block schema")
            pieces.append(block["text"])
        elif block["type"] not in {"tool_result", "tool_use", "image", "input_image", "localImage", "document", "file", "audio", "input_audio"}:
            raise ValueError("Unknown content block type")
    return "\n".join(pieces)


def _json_lines(path, collector):
    try:
        before = path.stat()
        with path.open("rb") as stream:
            for line_number, raw in enumerate(stream, 1):
                ref = f"{path}:{line_number}"
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError("Expected an object")
                except (ValueError, UnicodeDecodeError):
                    collector.warn(f"Invalid or unfinished JSON record: {ref}")
                    continue
                yield line_number, value
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            collector.warn(f"Source changed during scan; rerun to complete coverage: {path}")
    except OSError:
        collector.warn(f"Source file is missing or unreadable: {path}")


def _legacy(path, collector, conversation=None, trusted_human=False):
    rows = _json_lines(path, collector)
    human = trusted_human
    seen_metadata = False
    timestamp_occurrences = {}
    for number, row in rows:
        ref = f"{path}:{number}"
        kind = row.get("type")
        payload = row.get("payload")
        if kind == "session_meta":
            seen_metadata = True
            if not isinstance(payload, dict):
                collector.warn(f"Unsupported Codex session metadata: {ref}")
                human = False
                continue
            # If metadata is available, it must not contradict a trusted index.
            if not trusted_human or payload.get("source") is not None or payload.get("agent_path") is not None:
                human = _human_codex(payload.get("source"), payload.get("agent_path"), collector, ref)
            conversation = conversation or payload.get("id")
            if human and conversation:
                collector.scanned.add(str(conversation))
            continue
        if kind != "event_msg":
            # response_item repeats user messages; it is intentionally ignored.
            continue
        if not isinstance(payload, dict):
            collector.warn(f"Unsupported Codex event schema: {ref}")
            continue
        if payload.get("type") != "user_message":
            continue
        if not human or not conversation:
            if not seen_metadata and not trusted_human:
                collector.warn(f"Codex legacy message lacks human-source metadata: {ref}")
            continue
        try:
            occurred = _time(row.get("timestamp"))
        except (ValueError, TypeError, OverflowError):
            collector.warn(f"Missing or invalid message timestamp: {ref}")
            continue
        # Native IDs win. Older rollouts have no event ID, so combine the native
        # event time with its occurrence within the conversation. Count before
        # interval filtering: changing the window, moving/archiving the file or
        # inserting unrelated metadata must not change an existing identity.
        # Occurrences distinguish separate messages even with identical text
        # and timestamps; source_ref remains only the current physical locator.
        timestamp = _iso(occurred)
        occurrence_key = (str(conversation), timestamp)
        occurrence = timestamp_occurrences.get(occurrence_key, 0) + 1
        timestamp_occurrences[occurrence_key] = occurrence
        ident = payload.get("message_id") or payload.get("id") or f"legacy:{timestamp}:{occurrence}"
        collector.add(conversation, ident, occurred, payload.get("message"), ref)


def _paginated(path, threads, collector):
    if path is None:
        collector.warn("Missing Codex thread_history_*.sqlite for paginated conversations")
        return
    try:
        with closing(_open_db(path)) as db:
            required = {"thread_id", "turn_id", "item_id", "created_at_ms", "item_json", "item_type"}
            if not _schema(db, "thread_items", required):
                collector.warn(f"Unsupported Codex thread_items schema: {path}")
                return
            # A date WHERE clause would silently discard NULL / malformed dates.
            # Inspect identity and timestamp metadata only, without reading old text.
            invalid_dates = db.execute("SELECT thread_id, item_id FROM thread_items WHERE item_type = 'userMessage' AND (created_at_ms IS NULL OR typeof(created_at_ms) NOT IN ('integer', 'real'))")
            for row in invalid_dates:
                if row["thread_id"] in threads:
                    collector.warn(f"Invalid Codex userMessage timestamp: {path}#thread_items/{row['thread_id']}/{row['item_id']}")
            sql = "SELECT thread_id, item_id, created_at_ms, item_json, item_type FROM thread_items WHERE created_at_ms >= ?"
            args = [collector.since.timestamp() * 1000]
            if collector.until is not None:
                sql += " AND created_at_ms < ?"
                args.append(collector.until.timestamp() * 1000)
            sql += " ORDER BY created_at_ms, thread_id, item_id"
            for row in db.execute(sql, args):
                if row["thread_id"] not in threads or row["item_type"] != "userMessage":
                    continue
                ref = f"{path}#thread_items/{row['thread_id']}/{row['item_id']}"
                try:
                    item = json.loads(row["item_json"])
                    if not isinstance(item, dict) or item.get("type") != "userMessage":
                        raise ValueError("Unsupported user message schema")
                    text = _text(item.get("content"))
                    occurred = datetime.fromtimestamp(row["created_at_ms"] / 1000, UTC)
                except (ValueError, TypeError, OverflowError, OSError):
                    collector.warn(f"Invalid Codex userMessage record or schema: {ref}")
                    continue
                ident = row["item_id"] or item.get("id") or item.get("clientId")
                collector.add(row["thread_id"], ident, occurred, text, ref)
    except (sqlite3.Error, OSError):
        collector.warn(f"Codex history database is unreadable: {path}")


def _codex(root, collector):
    state = _latest(root, "state")
    if state is None:
        collector.warn(f"Missing Codex state_*.sqlite index: {root}")
        sessions = root / "sessions"
        if sessions.is_dir():
            for path in sorted(sessions.rglob("*.jsonl")):
                _legacy(path, collector)
        return
    try:
        with closing(_open_db(state)) as db:
            if not _schema(db, "threads", {"id", "source", "agent_path", "history_mode", "rollout_path"}):
                collector.warn(f"Unsupported Codex threads schema: {state}")
                return
            threads = list(db.execute("SELECT id, source, agent_path, history_mode, rollout_path FROM threads ORDER BY id"))
    except (sqlite3.Error, OSError):
        collector.warn(f"Codex state database is unreadable: {state}")
        return
    paginated = set()
    legacy_paths = set()
    for thread in threads:
        ident = thread["id"]
        ref = f"{state}#threads/{ident}"
        if not _human_codex(thread["source"], thread["agent_path"], collector, ref):
            continue
        if not ident:
            collector.warn(f"Missing Codex conversation identity: {ref}")
            continue
        collector.scanned.add(str(ident))
        mode = thread["history_mode"]
        if mode == "paginated":
            paginated.add(ident)
        elif mode == "legacy":
            if not thread["rollout_path"]:
                collector.warn(f"Missing Codex legacy rollout path: {ref}")
                continue
            path = Path(thread["rollout_path"]).expanduser()
            if not path.is_absolute():
                path = root / path
            if path.resolve() not in legacy_paths:
                legacy_paths.add(path.resolve())
                _legacy(path, collector, str(ident), trusted_human=True)
        else:
            collector.warn(f"Unsupported Codex history_mode: {ref}")
    if paginated:
        _paginated(_latest(root, "thread_history"), paginated, collector)


def _synthetic(row):
    if row.get("isSidechain") is True or row.get("isMeta") is True:
        return True
    for key in ("origin", "entrypoint", "promptSource", "userType"):
        value = row.get(key)
        if isinstance(value, dict):
            value = value.get("type") or value.get("source")
        if isinstance(value, str):
            value = value.lower().replace("-", "_")
            if value in _SYNTHETIC or value.startswith(("hook_", "subagent_", "synthetic_", "system_")):
                return True
    return False


def _claude(root, collector):
    projects = root if root.name == "projects" else root / "projects"
    if not projects.is_dir():
        collector.warn(f"Missing Claude Code projects directory: {projects}")
        return
    try:
        paths = sorted(path for project in projects.iterdir() if project.is_dir() and not project.is_symlink() for path in project.glob("*.jsonl") if path.is_file() and not path.is_symlink())
    except OSError:
        collector.warn(f"Claude Code projects directory is unreadable: {projects}")
        return
    for path in paths:
        conversation_seen = False
        for number, row in _json_lines(path, collector):
            ref = f"{path}:{number}"
            kind = row.get("type")
            if kind in _CLAUDE_NONMESSAGE_TYPES:
                continue
            if kind != "user":
                collector.warn(f"Unsupported Claude Code record type: {ref}")
                continue
            if _synthetic(row):
                continue
            conversation = row.get("sessionId")
            if conversation:
                collector.scanned.add(str(conversation))
                conversation_seen = True
            message = row.get("message")
            if not isinstance(message, dict) or message.get("role") != "user":
                collector.warn(f"Unsupported Claude Code user record schema: {ref}")
                continue
            try:
                text = _text(message.get("content"))
                occurred = _time(row.get("timestamp"))
            except (ValueError, TypeError, OverflowError):
                collector.warn(f"Invalid Claude Code user content or timestamp: {ref}")
                continue
            collector.add(conversation, row.get("uuid"), occurred, text, ref)
        if not conversation_seen:
            # An empty / assistant-only file is still a scanned conversation file.
            collector.scanned.add(f"file:{path.resolve()}")


def collect_messages(platform, source_root, since, until=None):
    """Collect human messages in the aware-ISO interval [since, until).

    ``source_root`` is the platform's configuration root (normally ``~/.codex``
    or ``~/.claude``). ``until=None`` means no upper time bound. Failures in
    individual logs are returned as coverage warnings without leaking content.
    Invalid caller arguments raise ``ValueError``.
    """
    if platform not in {"codex", "claude-code"}:
        raise ValueError("platform must be 'codex' or 'claude-code'")
    start = _time(since)
    end = _time(until) if until is not None else None
    if end is not None and end < start:
        raise ValueError("until must be at or after since")
    collector = _Collection(platform, start, end)
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        collector.warn(f"Source root is missing or unreadable: {root}")
        return collector.result()
    try:
        (_codex if platform == "codex" else _claude)(root, collector)
    except OSError:
        collector.warn(f"Source discovery failed or source changed during scan: {root}")
    return collector.result()
