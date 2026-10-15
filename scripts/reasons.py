"""Evidence-bound primary reasons for confirmed reprimands.

The host supplies semantic judgments. This module only validates provenance,
persists explanations, and computes counts; source text never becomes a command.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3

import scold


TAXONOMY_VERSION = 1
CATEGORY_LABELS = {
    "unclear_communication": "说明不清楚",
    "instruction_not_followed": "未遵守需求",
    "incorrect_result": "结果错误",
    "repeated_error": "重复犯错",
    "unwanted_action": "擅自行动",
    "incomplete_work": "任务未完成",
    "other": "其他",
    "unknown": "无法判断",
}
BASES = ("user_stated", "context_inferred", "unknown")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_schema(db):
    """Add reason storage without committing or ending the caller's transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS reason_contexts (
        context_id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES events(event_id),
        context_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS reason_contexts_event ON reason_contexts(event_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS reason_reviews (
        event_id TEXT PRIMARY KEY REFERENCES events(event_id),
        context_id TEXT NOT NULL REFERENCES reason_contexts(context_id),
        analysis_json TEXT NOT NULL,
        analyzed_at TEXT NOT NULL
    )""")


def invalidate(db, event_id):
    """Remove a prior reason within the caller's existing classification transaction."""
    ensure_schema(db)
    db.execute("DELETE FROM reason_reviews WHERE event_id=?", (event_id,))


def _positive_int(value, name, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise scold.ScoldError(f"{name} must be a {'nonnegative' if allow_zero else 'positive'} integer")


def pending(data_dir, limit=30, offset=0):
    """Return confirmed events without a reason; unknown is already analyzed."""
    scold._config(data_dir)
    _positive_int(limit, "limit")
    _positive_int(offset, "offset", allow_zero=True)
    with closing(scold._database(data_dir)) as db, db:
        ensure_schema(db)
        clause = "FROM events e LEFT JOIN reason_reviews r ON r.event_id=e.event_id WHERE e.status='scolding' AND r.event_id IS NULL"
        total = db.execute("SELECT COUNT(*) " + clause).fetchone()[0]
        events = [scold._event(row) for row in db.execute(
            "SELECT e.* " + clause + " ORDER BY e.occurred_at,e.event_id LIMIT ? OFFSET ?", (limit, offset))]
    return {"total_pending": total, "limit": limit, "offset": offset, "events": events}


def _context_reader():
    from context import read_context
    return read_context


def _confirmed(db, event_id):
    if not isinstance(event_id, str) or not event_id:
        raise scold.ScoldError("event_id must be a nonempty string")
    row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
    if row is None or row["status"] != "scolding":
        raise scold.ScoldError("Reason analysis requires a currently confirmed scolding event")
    return scold._event(row)


def _bound_context(context, max_messages, max_chars):
    """Keep the target first in the budget and bound every stored text snapshot."""
    kept, budget = [], max_chars
    truncated = False
    for message in reversed(context["messages"]):
        if len(kept) == max_messages or budget == 0:
            truncated = True
            break
        value = dict(message)
        if len(value["text"]) > budget:
            value["text"] = value["text"][:budget]
            value["text_truncated"] = True
            truncated = True
        kept.append(value)
        budget -= len(value["text"])
    context["messages"] = list(reversed(kept))
    if truncated:
        context["coverage"]["complete"] = False
        context["coverage"]["truncated"] = True
        context["coverage"]["warnings"].append("Context text was truncated to the configured message or character limit")
    return context


def _fallback(event, warnings, max_messages, max_chars):
    return _bound_context({"messages": [{"message_id": event["message_id"], "role": "user",
            "occurred_at": event["occurred_at"], "text": event["text"], "source_ref": event["source_ref"]}],
            "target_found": False,
            "coverage": {"complete": False, "truncated": False,
                         "warnings": list(dict.fromkeys(warnings + [
                             "Native target context was unavailable; only the original ledger message is available"]))}},
                          max_messages, max_chars)


def _normalize_context(value, event, max_messages, max_chars):
    def fallback(warnings):
        return _fallback(event, warnings, max_messages, max_chars)
    if not isinstance(value, dict):
        return fallback(["Native context reader returned an unsupported result"])
    raw_coverage = value.get("coverage")
    raw_coverage = raw_coverage if isinstance(raw_coverage, dict) else {}
    warnings = raw_coverage.get("warnings", [])
    warnings = [w for w in warnings if isinstance(w, str)] if isinstance(warnings, list) else []
    raw_messages = value.get("messages")
    if value.get("target_found") is not True or not isinstance(raw_messages, list):
        return fallback(warnings)
    messages, seen = [], set()
    for message in raw_messages:
        if (not isinstance(message, dict) or message.get("role") not in ("user", "assistant")
                or not isinstance(message.get("message_id"), str) or not message["message_id"]
                or message["message_id"] in seen or not isinstance(message.get("text"), str)):
            return fallback(warnings + ["Native context contained an unsupported message"])
        seen.add(message["message_id"])
        messages.append({key: message.get(key) for key in
                         ("message_id", "role", "occurred_at", "text", "source_ref")})
        if message.get("text_truncated") is True:
            messages[-1]["text_truncated"] = True
        if message["message_id"] == event["message_id"]:
            if message["role"] != "user":
                return fallback(warnings + ["Native target did not identify a user message"])
            break
    if not messages or messages[-1]["message_id"] != event["message_id"]:
        return fallback(warnings)
    target = messages[-1]
    accepted_prefix = target.get("text_truncated") is True and event["text"].startswith(target["text"])
    changed_target = target["text"] != event["text"] and not accepted_prefix
    if changed_target:
        messages[-1] = {"message_id": event["message_id"], "role": "user",
                        "occurred_at": event["occurred_at"], "text": event["text"], "source_ref": event["source_ref"]}
        warnings.append("Native target text differs from the reviewed ledger text; the ledger original remains the analysis target")
    return _bound_context({"messages": messages, "target_found": True,
            "coverage": {"complete": raw_coverage.get("complete") is True and not changed_target,
                         "warnings": list(dict.fromkeys(warnings)),
                         "truncated": raw_coverage.get("truncated") is True}}, max_messages, max_chars)


def context_for(data_dir, event_id, max_messages=12, max_chars=16000):
    """Read and persist a bounded native snapshot belonging to this exact event."""
    config = scold._config(data_dir)
    _positive_int(max_messages, "max_messages")
    _positive_int(max_chars, "max_chars")
    with closing(scold._database(data_dir)) as db:
        event = _confirmed(db, event_id)
    try:
        result = _context_reader()(config["platform"], config["source_root"],
                                   event["conversation_id"], event["message_id"],
                                   max_messages=max_messages, max_chars=max_chars)
    except (ImportError, OSError, ValueError, RuntimeError, sqlite3.Error):
        result = {"target_found": False, "coverage": {
            "complete": False, "warnings": ["Native conversation context could not be read"], "truncated": False}}
    context = _normalize_context(result, event, max_messages, max_chars)
    payload = {"event_id": event_id, "platform": event["platform"],
               "conversation_id": event["conversation_id"], **context}
    serialized = _json(payload)
    context_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    with closing(scold._database(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        ensure_schema(db)
        event = _confirmed(db, event_id)
        db.execute("INSERT OR IGNORE INTO reason_contexts(context_id,event_id,context_json,created_at) VALUES(?,?,?,?)",
                   (context_id, event_id, serialized, scold._iso(scold._now())))
    return {"event": event, "context_id": context_id, **context}


def _validated_analysis(db, analysis, index):
    if not isinstance(analysis, dict):
        raise scold.ScoldError(f"Analysis at index {index} must be an object")
    event_id = analysis.get("event_id")
    event = _confirmed(db, event_id)
    context_id = analysis.get("context_id")
    if not isinstance(context_id, str) or not context_id:
        raise scold.ScoldError(f"Analysis at index {index} requires context_id")
    row = db.execute("SELECT event_id,context_json FROM reason_contexts WHERE context_id=?", (context_id,)).fetchone()
    if row is None or row["event_id"] != event_id:
        raise scold.ScoldError(f"Analysis at index {index} must use a context snapshot belonging to the event")
    try:
        context = json.loads(row["context_json"])
    except (TypeError, json.JSONDecodeError):
        raise scold.ScoldError("Stored context is invalid; read the event context again") from None
    if (context.get("event_id") != event_id or context.get("platform") != event["platform"]
            or context.get("conversation_id") != event["conversation_id"]):
        raise scold.ScoldError("Stored context does not belong to this event and conversation")
    category, basis, summary = (analysis.get(key) for key in ("category", "basis", "summary"))
    if not isinstance(category, str) or category not in CATEGORY_LABELS:
        raise scold.ScoldError(f"Analysis at index {index} has an unsupported category")
    if basis not in BASES:
        raise scold.ScoldError(f"Analysis at index {index} has an unsupported basis")
    if (category == "unknown") != (basis == "unknown"):
        raise scold.ScoldError(f"Analysis at index {index} must pair unknown category with unknown basis")
    if not isinstance(summary, str) or not summary.strip():
        raise scold.ScoldError(f"Analysis at index {index} requires a nonempty summary")
    evidence = analysis.get("evidence")
    if not isinstance(evidence, list) or (basis != "unknown" and not evidence):
        raise scold.ScoldError(f"Analysis at index {index} requires evidence; unknown may use an empty list")
    messages = {message["message_id"]: message for message in context["messages"]}
    validated, seen = [], set()
    for item in evidence:
        if not isinstance(item, dict):
            raise scold.ScoldError(f"Analysis at index {index} contains invalid evidence")
        message_id, quote = item.get("message_id"), item.get("quote")
        if not isinstance(message_id, str) or message_id not in messages:
            raise scold.ScoldError(f"Analysis at index {index} cites an unknown context message")
        message = messages[message_id]
        if not isinstance(quote, str) or not quote.strip() or quote not in message["text"]:
            raise scold.ScoldError(f"Analysis at index {index} must quote a nonempty exact substring of its context message")
        identity = (message_id, quote)
        if identity not in seen:
            seen.add(identity)
            validated.append({"message_id": message_id, "quote": quote,
                              "role": message["role"], "occurred_at": message["occurred_at"],
                              "source_ref": message["source_ref"]})
    if basis == "user_stated" and not any(item["role"] == "user" for item in validated):
        raise scold.ScoldError(f"Analysis at index {index} with user_stated basis requires user evidence")
    return {"event_id": event_id, "context_id": context_id, "category": category,
            "basis": basis, "summary": summary, "evidence": validated,
            "taxonomy_version": TAXONOMY_VERSION, "context_coverage": context["coverage"]}


def review(data_dir, analyses):
    """Validate a batch atomically; exact replay cannot rewrite analysis time."""
    scold._config(data_dir)
    if not isinstance(analyses, list):
        raise scold.ScoldError("analyses must be a list")
    prepared, changed = {}, 0
    with closing(scold._database(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        ensure_schema(db)
        for index, analysis in enumerate(analyses):
            value = _validated_analysis(db, analysis, index)
            event_id, serialized = value["event_id"], _json(value)
            if event_id in prepared and prepared[event_id] != serialized:
                raise scold.ScoldError(f"Analysis at index {index} conflicts with another analysis in this batch")
            prepared[event_id] = serialized
        now = scold._iso(scold._now())
        for event_id, serialized in prepared.items():
            old = db.execute("SELECT analysis_json FROM reason_reviews WHERE event_id=?", (event_id,)).fetchone()
            if old is None or old["analysis_json"] != serialized:
                context_id = json.loads(serialized)["context_id"]
                db.execute("""INSERT INTO reason_reviews(event_id,context_id,analysis_json,analyzed_at)
                    VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
                    context_id=excluded.context_id,analysis_json=excluded.analysis_json,analyzed_at=excluded.analyzed_at""",
                           (event_id, context_id, serialized, now))
                changed += 1
    return {"processed": len(prepared), "updated": changed, "unchanged": len(prepared) - changed}


def annotate_report(db, events):
    """Attach each current period's primary reason and return additive counters."""
    ensure_schema(db)
    summary = {"analyzed_count": 0, "pending_count": 0,
               "user_stated_count": 0, "context_inferred_count": 0, "unknown_count": 0,
               "categories": dict.fromkeys(CATEGORY_LABELS, 0), "category_labels": dict(CATEGORY_LABELS)}
    for event in events:
        row = db.execute("""SELECT r.analysis_json,r.analyzed_at FROM reason_reviews r
                          JOIN events e ON e.event_id=r.event_id
                          WHERE r.event_id=? AND e.status='scolding'""", (event["event_id"],)).fetchone()
        if row is None:
            event["reason"] = None
            summary["pending_count"] += 1
            continue
        reason = json.loads(row["analysis_json"])
        reason["analyzed_at"] = row["analyzed_at"]
        event["reason"] = reason
        summary["analyzed_count"] += 1
        summary[reason["basis"] + "_count"] += 1
        summary["categories"][reason["category"]] += 1
    return summary
