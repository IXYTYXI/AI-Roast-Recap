#!/usr/bin/env python3
"""Private, local event ledger for AI reprimand reviews (Python standard library).

The collector only imports user messages. A host agent supplies explicit review
decisions; this module never classifies text or calls a model/network service.
Public functions return JSON-compatible dictionaries and raise ScoldError for
invalid requests. Timestamps are aware ISO 8601; report intervals are half-open.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date as Date, datetime, time, timedelta, timezone as UTCZone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Reason helpers share this module's error class when the CLI is the entrypoint.
if __name__ == "__main__":
    sys.modules.setdefault("scold", sys.modules[__name__])


UTC = UTCZone.utc
STATUSES = ("scolding", "not_scolding", "uncertain", "pending")
PLATFORMS = ("codex", "claude-code")


class ScoldError(ValueError):
    """An actionable error whose message is safe to display without source text."""


def _now():
    return datetime.now(UTC)


def _timestamp(value, field="timestamp"):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(UTC)
    except (TypeError, ValueError, AttributeError, OverflowError):
        raise ScoldError(f"{field} must be an ISO 8601 timestamp with an explicit timezone") from None


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ScoldError("timezone must be an installed IANA timezone, for example Asia/Shanghai") from None


def _path(value):
    return Path(value).expanduser().resolve()


def _read_json(path, description):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ScoldError(f"Cannot read {description}; check its path, permissions and JSON format") from None


def _write_json(path, value):
    """Atomically replace a JSON file, retaining user-only access permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".scold-", delete=False) as stream:
            temp_name = stream.name
            os.chmod(temp_name, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _config(data_dir):
    result = _read_json(_path(data_dir) / "config.json", "configuration (run init first)")
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise ScoldError("Unsupported or invalid configuration; expected schema_version 1")
    if result.get("platform") not in PLATFORMS or not isinstance(result.get("source_root"), str):
        raise ScoldError("Configuration has an invalid platform or source_root")
    _timestamp(result.get("enabled_at"), "enabled_at")
    _zone(result.get("timezone"))
    return result


def _database(data_dir):
    path = _path(data_dir) / "ledger.sqlite"
    if not path.is_file():
        raise ScoldError("Ledger database is missing; run init with the original configuration")
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _reason_module():
    import reasons
    return reasons


def initialize(data_dir, platform, source_root, timezone=None, since=None):
    """Create config/database once; repeat initialization cannot reset enabled_at."""
    if platform not in PLATFORMS:
        raise ScoldError("platform must be codex or claude-code")
    if timezone is not None:
        _zone(timezone)
    enabled = _iso(_timestamp(since, "since")) if since is not None else None
    root = _path(data_dir)
    source = str(_path(source_root))
    if (root / "config.json").exists():
        config = _config(root)
        requested = {"platform": platform, "source_root": source}
        if timezone is not None:
            requested["timezone"] = timezone
        if enabled is not None:
            requested["enabled_at"] = enabled
        if any(config.get(key) != value for key, value in requested.items()):
            raise ScoldError("Existing configuration differs; use a separate data directory instead of resetting it")
        created = False
    else:
        stamp = _iso(_now())
        config = {"schema_version": 1, "platform": platform, "source_root": source,
                  "timezone": timezone or "Asia/Shanghai", "enabled_at": enabled or stamp, "created_at": stamp}
        created = True
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    database = root / "ledger.sqlite"
    if not database.exists():
        fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    with closing(_database(root)) as db, db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                text TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','scolding','not_scolding','uncertain')),
                note TEXT,
                collected_at TEXT NOT NULL,
                reviewed_at TEXT,
                UNIQUE(platform, conversation_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS events_period ON events(occurred_at, status);
            CREATE INDEX IF NOT EXISTS events_pending ON events(status, occurred_at, event_id);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        _reason_module().ensure_schema(db)
    if created:
        _write_json(root / "config.json", config)
    else:
        os.chmod(root / "config.json", 0o600)
    return {"initialized": True, "created": created, "config": config, "data_dir": str(root)}


def event_id_for(platform, conversation_id, message_id):
    """Stable identity across rescans; identical text in different messages counts separately."""
    identity = json.dumps([platform, conversation_id, message_id], ensure_ascii=False, separators=(",", ":"))
    return "evt_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _coverage(value):
    if value is None:
        return {"complete": False, "warnings": ["No source coverage metadata was provided"], "scanned_conversations": 0}
    if not isinstance(value, dict) or not isinstance(value.get("complete"), bool):
        raise ScoldError("Source coverage must contain a boolean complete field")
    warnings = value.get("warnings", [])
    scanned = value.get("scanned_conversations", 0)
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ScoldError("Source coverage warnings must be a list of strings")
    if isinstance(scanned, bool) or not isinstance(scanned, int) or scanned < 0:
        raise ScoldError("Source coverage scanned_conversations must be a nonnegative integer")
    return {"complete": value["complete"], "warnings": list(warnings), "scanned_conversations": scanned}


def _save_scan(db, coverage, collected_at, succeeded=True):
    # Failed attempts must not advance the valid ingestion watermark. Keep their
    # warning separately so reports can show both the usable scan and its failure.
    values = [("latest_collection_attempt", {"attempted_at": collected_at,
               "succeeded": succeeded, "warnings": coverage["warnings"]})]
    if succeeded:
        values.extend((("coverage", coverage), ("latest_collection_at", collected_at)))
    for key, value in values:
        db.execute("INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value, ensure_ascii=False)))


def ingest_messages(data_dir, messages, coverage=None, collected_at=None):
    """Validate and atomically insert a source batch, preserving review decisions."""
    config = _config(data_dir)
    enabled = _timestamp(config["enabled_at"])
    collected = _iso(_timestamp(collected_at, "collected_at")) if collected_at is not None else _iso(_now())
    scan = _coverage(coverage)
    if not isinstance(messages, list):
        raise ScoldError("Source messages must be a list")
    prepared, before = [], 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ScoldError(f"Source message at index {index} must be an object")
        for key in ("platform", "conversation_id", "message_id", "text"):
            if not isinstance(message.get(key), str) or (key != "text" and not message[key]):
                raise ScoldError(f"Source message at index {index} has an invalid {key}")
        if message["platform"] != config["platform"]:
            raise ScoldError(f"Source message at index {index} has a different platform from the configuration")
        occurred = _timestamp(message.get("occurred_at"), f"Source message {index} occurred_at")
        if "source_ref" not in message:
            raise ScoldError(f"Source message at index {index} is missing source_ref")
        try:
            source_ref = json.dumps(message["source_ref"], ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise ScoldError(f"Source message at index {index} has a non-JSON source_ref") from None
        if occurred < enabled:
            before += 1
            continue
        prepared.append((event_id_for(message["platform"], message["conversation_id"], message["message_id"]),
                         message["platform"], message["conversation_id"], message["message_id"], _iso(occurred),
                         message["text"], source_ref, collected))
    with closing(_database(data_dir)) as db, db:
        inserted = 0
        for row in prepared:
            cursor = db.execute("""INSERT OR IGNORE INTO events
                (event_id,platform,conversation_id,message_id,occurred_at,text,source_ref,collected_at)
                VALUES (?,?,?,?,?,?,?,?)""", row)
            inserted += cursor.rowcount
            if cursor.rowcount == 0:
                # Native sessions can move into an archive. Refresh the pointer,
                # retaining the exact evidence and semantic review first stored.
                db.execute("UPDATE events SET source_ref=? WHERE event_id=?", (row[6], row[0]))
        _save_scan(db, scan, collected)
    return {"received": len(messages), "inserted": inserted, "duplicates": len(prepared) - inserted,
            "before_enabled_at": before, "collected_at": collected, "coverage": scan}


def _source_collector():
    from sources import collect_messages
    return collect_messages


def collect(data_dir):
    """Rescan from enabled_at, so late-arriving native messages cannot be missed."""
    config = _config(data_dir)
    collected = _iso(_now())
    try:
        result = _source_collector()(config["platform"], config["source_root"], config["enabled_at"], until=collected)
        if not isinstance(result, dict) or "messages" not in result or "coverage" not in result:
            raise ScoldError("Collector returned an invalid result")
        return ingest_messages(data_dir, result["messages"], result["coverage"], collected)
    except Exception as error:
        # Native exception messages can embed complete private records. Persist
        # only a safe error category, then let the caller inspect its local setup.
        warning = f"Source collection failed ({type(error).__name__}); check local source configuration"
        with closing(_database(data_dir)) as db, db:
            _save_scan(db, {"complete": False, "warnings": [warning], "scanned_conversations": 0}, collected,
                       succeeded=False)
        raise ScoldError(warning) from None


def _event(row):
    result = dict(row)
    result["source_ref"] = json.loads(result["source_ref"])
    return result


def _counts(db, start=None, end=None):
    clause, params = (" WHERE occurred_at >= ? AND occurred_at < ?", (start, end)) if start else ("", ())
    result = dict.fromkeys(STATUSES, 0)
    for row in db.execute("SELECT status, COUNT(*) AS n FROM events" + clause + " GROUP BY status", params):
        result[row["status"]] = row["n"]
    return result


def _scan_metadata(db):
    metadata = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key, value FROM metadata")}
    coverage = metadata.get("coverage", {"complete": False, "warnings": ["No collection has run"], "scanned_conversations": 0})
    collected = metadata.get("latest_collection_at")
    attempt = metadata.get("latest_collection_attempt")
    if attempt is None and collected is not None:
        attempt = {"attempted_at": collected, "succeeded": True, "warnings": coverage["warnings"]}
    if attempt is not None and not attempt["succeeded"]:
        coverage = dict(coverage, warnings=list(dict.fromkeys(coverage["warnings"] + attempt["warnings"])))
    return coverage, collected, attempt


def pending(data_dir, limit=100, offset=0):
    """Return a stable page of unreviewed original messages and their source locations."""
    _config(data_dir)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ScoldError("limit must be a positive integer")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ScoldError("offset must be a nonnegative integer")
    with closing(_database(data_dir)) as db:
        total = db.execute("SELECT COUNT(*) FROM events WHERE status='pending'").fetchone()[0]
        events = [_event(row) for row in db.execute("SELECT * FROM events WHERE status='pending' ORDER BY occurred_at,event_id LIMIT ? OFFSET ?", (limit, offset))]
    return {"total_pending": total, "limit": limit, "offset": offset, "events": events}


def review(data_dir, decisions):
    """Atomically apply explicit classifications; identical replay has no side effects."""
    _config(data_dir)
    if not isinstance(decisions, list):
        raise ScoldError("decisions must be a list")
    prepared = {}
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise ScoldError(f"Decision at index {index} must be an object")
        ident, classification, note = (decision.get(key) for key in ("event_id", "status", "note"))
        if not isinstance(ident, str) or not ident:
            raise ScoldError(f"Decision at index {index} has an invalid event_id")
        if classification not in STATUSES[:3]:
            raise ScoldError(f"Decision at index {index} must have status scolding, not_scolding or uncertain")
        if not isinstance(note, str) or not note.strip():
            raise ScoldError(f"Decision at index {index} requires a short nonempty note")
        if ident in prepared and prepared[ident] != (classification, note):
            raise ScoldError(f"Decision at index {index} conflicts with another decision in this batch")
        prepared[ident] = (classification, note)
    changed, now = 0, _iso(_now())
    with closing(_database(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for index, (ident, (classification, note)) in enumerate(prepared.items()):
            old = db.execute("SELECT status,note FROM events WHERE event_id=?", (ident,)).fetchone()
            if old is None:
                raise ScoldError(f"Decision at index {index} refers to an unknown event_id; no decisions were saved")
            if (old["status"], old["note"]) != (classification, note):
                if old["status"] == "scolding" and classification != "scolding":
                    _reason_module().invalidate(db, ident)
                db.execute("UPDATE events SET status=?,note=?,reviewed_at=? WHERE event_id=?", (classification, note, now, ident))
                changed += 1
    return {"processed": len(prepared), "updated": changed, "unchanged": len(prepared) - changed}


def _period(period, date, zone):
    if period not in ("day", "week", "month"):
        raise ScoldError("period must be day, week or month")
    try:
        day = Date.fromisoformat(date) if date is not None else _now().astimezone(zone).date()
        if date is not None and day.isoformat() != date:
            raise ValueError()
    except (ValueError, TypeError):
        raise ScoldError("date must use YYYY-MM-DD format") from None
    if period == "week":
        first = day - timedelta(days=day.weekday())
        last = first + timedelta(days=7)
    elif period == "month":
        first = day.replace(day=1)
        last = Date(first.year + (first.month == 12), 1 if first.month == 12 else first.month + 1, 1)
    else:
        first, last = day, day + timedelta(days=1)
    return datetime.combine(first, time.min, zone), datetime.combine(last, time.min, zone)


def report(data_dir, period, date=None):
    """Count only confirmed scolding; expose pending/uncertain and all coverage gaps."""
    config = _config(data_dir)
    zone = _zone(config["timezone"])
    local_start, local_end = _period(period, date, zone)
    start, end = local_start.astimezone(UTC), local_end.astimezone(UTC)
    with closing(_database(data_dir)) as db:
        counts = _counts(db, _iso(start), _iso(end))
        events = [_event(row) for row in db.execute("SELECT * FROM events WHERE occurred_at>=? AND occurred_at<? AND status='scolding' ORDER BY occurred_at,event_id", (_iso(start), _iso(end)))]
        uncertain = [_event(row) for row in db.execute("SELECT * FROM events WHERE occurred_at>=? AND occurred_at<? AND status='uncertain' ORDER BY occurred_at,event_id", (_iso(start), _iso(end)))]
        scan, collected_at, attempt = _scan_metadata(db)
        reason_summary = _reason_module().annotate_report(db, events)
    enabled = _timestamp(config["enabled_at"])
    collected = _timestamp(collected_at) if collected_at else None
    warnings = list(scan["warnings"])
    if enabled > start:
        warnings.append("The requested period begins before enabled_at; earlier history is not covered and must not be treated as zero")
    if collected is None:
        warnings.append("No source collection is available for this report; zero confirmed events does not mean no scolding")
    elif collected < end:
        warnings.append("The requested period extends beyond the latest collection; later messages have not been collected")
    if not scan["complete"] and not scan["warnings"]:
        warnings.append("The most recent source scan reported incomplete coverage")
    observed_from = max(start, enabled)
    observed_until = min(end, collected) if collected else None
    observed = observed_until is not None and observed_from < observed_until
    complete = bool(observed and enabled <= start and collected >= end and scan["complete"])
    coverage = {"status": "complete" if complete else ("partial" if observed else "not_collected"),
                "complete": complete, "source_scan_complete": scan["complete"],
                "observed_from": _iso(observed_from) if observed else None,
                "observed_until": _iso(observed_until) if observed else None,
                "scanned_conversations": scan["scanned_conversations"],
                "warnings": list(dict.fromkeys(warnings))}
    return {"schema_version": 1, "platform": config["platform"],
            "period": {"kind": period, "timezone": config["timezone"], "start": _iso(start), "end": _iso(end),
                       "start_local": local_start.isoformat(), "end_local": local_end.isoformat(), "end_exclusive": True},
            "enabled_at": config["enabled_at"], "coverage_start": config["enabled_at"],
            "latest_collection_at": collected_at, "latest_collection_attempt": attempt, "coverage": coverage,
            "confirmed_scolding_count": counts["scolding"], "counts": counts,
            "pending_count": counts["pending"], "uncertain_count": counts["uncertain"],
            "count_interpretation": "Only explicitly confirmed scolding events count; pending, uncertain and uncollected history are not confirmed zeroes",
            "scoldings": events, "uncertain_events": uncertain, "reason_summary": reason_summary}


def status(data_dir):
    """Return configuration, stored event counts and the most recent scan metadata."""
    config = _config(data_dir)
    with closing(_database(data_dir)) as db:
        counts = _counts(db)
        coverage, collected, attempt = _scan_metadata(db)
    return {"config": config, "total_events": sum(counts.values()), "counts": counts,
            "coverage": coverage, "latest_collection_at": collected, "latest_collection_attempt": attempt}


class _JSONParser(argparse.ArgumentParser):
    def print_help(self, file=None):
        print(json.dumps({"help": self.format_help()}, ensure_ascii=False), file=file or sys.stdout)

    def error(self, message):
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


def main(argv=None):
    parser = _JSONParser(description=__doc__)
    default_data = os.environ.get("SCOLD_DATA_DIR", str(Path.home() / ".local" / "share" / "ai-scold-review"))
    parser.add_argument("--data-dir", default=default_data, help="Private config/ledger directory; default: %(default)s")
    commands = parser.add_subparsers(dest="command", required=True)
    parsers = {}
    for name, help_text in (("init", "Initialize once; defaults to collection from now"),
                            ("collect", "Import source user messages since enabled_at"),
                            ("pending", "Read unclassified messages as JSON"),
                            ("review", "Apply explicit agent review decisions atomically"),
                            ("reason-pending", "List confirmed events awaiting primary-reason analysis"),
                            ("context", "Save bounded pre-event user and assistant evidence"),
                            ("reason-review", "Validate and save evidence-backed primary reasons"),
                            ("report", "Report confirmed counts with coverage gaps"),
                            ("status", "Read configuration, counts and latest coverage")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--data-dir", default=argparse.SUPPRESS, help="Private config/ledger directory")
        parsers[name] = sub
    parsers["init"].add_argument("--platform", choices=PLATFORMS, required=True)
    parsers["init"].add_argument("--source-root", required=True)
    parsers["init"].add_argument("--timezone", help="IANA timezone (new configuration default: Asia/Shanghai)")
    parsers["init"].add_argument("--since", help="Explicit historical start, aware ISO 8601 (default: now)")
    parsers["pending"].add_argument("--limit", type=int, default=100)
    parsers["pending"].add_argument("--offset", type=int, default=0)
    parsers["review"].add_argument("--input", required=True, help="JSON file containing a decisions list")
    parsers["reason-pending"].add_argument("--limit", type=int, default=30)
    parsers["reason-pending"].add_argument("--offset", type=int, default=0)
    parsers["context"].add_argument("--event-id", required=True)
    parsers["context"].add_argument("--max-messages", type=int, default=12)
    parsers["context"].add_argument("--max-chars", type=int, default=16000)
    parsers["reason-review"].add_argument("--input", required=True, help="JSON file containing an analyses list")
    parsers["report"].add_argument("--period", choices=("day", "week", "month"), required=True)
    parsers["report"].add_argument("--date", help="Local YYYY-MM-DD (default: today in configured timezone)")
    parsers["report"].add_argument("--output", help="Also atomically write the report JSON to this file")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = initialize(args.data_dir, args.platform, args.source_root, args.timezone, args.since)
        elif args.command == "collect":
            result = collect(args.data_dir)
        elif args.command == "pending":
            result = pending(args.data_dir, args.limit, args.offset)
        elif args.command == "review":
            payload = _read_json(_path(args.input), "review input")
            if not isinstance(payload, dict) or "decisions" not in payload:
                raise ScoldError("Review input must be a JSON object with a decisions list")
            result = review(args.data_dir, payload["decisions"])
        elif args.command == "reason-pending":
            result = _reason_module().pending(args.data_dir, args.limit, args.offset)
        elif args.command == "context":
            result = _reason_module().context_for(args.data_dir, args.event_id,
                                                  args.max_messages, args.max_chars)
        elif args.command == "reason-review":
            payload = _read_json(_path(args.input), "reason analysis input")
            if not isinstance(payload, dict) or "analyses" not in payload:
                raise ScoldError("Reason analysis input must be a JSON object with an analyses list")
            result = _reason_module().review(args.data_dir, payload["analyses"])
        elif args.command == "report":
            result = report(args.data_dir, args.period, args.date)
            if args.output:
                _write_json(_path(args.output), result)
        else:
            result = status(args.data_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ScoldError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
    except (OSError, sqlite3.Error):
        print(json.dumps({"error": "Local file or database operation failed; check paths, permissions, available space and database integrity"}), file=sys.stderr)
    except Exception as error:
        print(json.dumps({"error": f"Operation failed ({type(error).__name__}); no private source text is included"}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
