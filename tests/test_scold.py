"""Behavior tests for the local review ledger; no account data or network used."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE / "scripts" / "scold.py"
sys.path.insert(0, str(SCRIPT.parent))
if SCRIPT.exists():
    import scold
else:
    scold = None


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(scold, "The scold ledger implementation does not exist yet")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.source = self.root / "native"
        self.source.mkdir()
        scold.initialize(self.data, "codex", self.source, since="2026-01-01T00:00:00Z")

    def message(self, ident, when="2026-10-14T16:00:00Z", text="你又没按要求做", conversation="c1"):
        return {"platform": "codex", "conversation_id": conversation,
                "message_id": ident, "occurred_at": when, "text": text,
                "source_ref": {"path": "native/session.jsonl", "line": 2}}

    def ingest(self, *messages, complete=True, warnings=None, at="2026-11-01T00:00:00Z"):
        return scold.ingest_messages(self.data, list(messages),
                coverage={"complete": complete, "warnings": warnings or [], "scanned_conversations": 1},
                collected_at=at)

    def decision(self, event_id, status="scolding", note="明确批评助手未执行要求"):
        return {"event_id": event_id, "status": status, "note": note}

    def test_initialize_is_idempotent_and_never_resets_enabled_at(self):
        original = scold.status(self.data)["config"]
        second = scold.initialize(self.data, "codex", self.source)
        self.assertEqual(original, second["config"])
        for changes in ({"platform": "claude-code"}, {"timezone": "UTC"},
                        {"since": "2026-02-01T00:00:00Z"}, {"source_root": self.root / "other"}):
            args = {"data_dir": self.data, "platform": "codex", "source_root": self.source}
            args.update(changes)
            with self.assertRaises(scold.ScoldError):
                scold.initialize(**args)
        self.assertEqual(original, scold.status(self.data)["config"])

    def test_default_enabled_at_is_installation_time_and_files_are_private(self):
        from datetime import datetime, timezone
        before = datetime.now(timezone.utc)
        result = scold.initialize(self.root / "new", "codex", self.source)
        after = datetime.now(timezone.utc)
        enabled = datetime.fromisoformat(result["config"]["enabled_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before, enabled)
        self.assertLessEqual(enabled, after)
        for name in ("config.json", "ledger.sqlite"):
            self.assertEqual(0, (self.root / "new" / name).stat().st_mode & 0o077)

    def test_rejects_naive_since_and_invalid_timezone(self):
        for kwargs in ({"since": "2026-01-01T00:00:00"}, {"timezone": "Invalid/Zone"}):
            with self.assertRaises(scold.ScoldError):
                scold.initialize(self.root / "invalid", "codex", self.source, **kwargs)

    def test_reinitialize_without_timezone_preserves_existing_custom_zone(self):
        other = self.root / "utc"
        original = scold.initialize(other, "codex", self.source, timezone="UTC")["config"]
        repeated = scold.initialize(other, "codex", self.source)["config"]
        self.assertEqual(original, repeated)

    def test_ingest_deduplicates_identity_but_preserves_same_text_messages(self):
        message = self.message("m1")
        first = self.ingest(message, self.message("m2"), self.message("m1", conversation="c2"))
        self.assertEqual(3, first["inserted"])
        self.assertEqual(0, self.ingest(message)["inserted"])
        result = scold.pending(self.data, limit=2, offset=1)
        self.assertEqual(3, result["total_pending"])
        self.assertEqual(2, len(result["events"]))
        self.assertEqual(3, scold.status(self.data)["total_events"])
        other = self.root / "other"
        scold.initialize(other, "codex", self.source, since="2026-01-01T00:00:00Z")
        scold.ingest_messages(other, [message])
        first_id = next(e["event_id"] for e in scold.pending(self.data)["events"]
                        if e["message_id"] == "m1" and e["conversation_id"] == "c1")
        self.assertEqual(first_id, scold.pending(other)["events"][0]["event_id"])

    def test_collect_scans_from_enabled_at_on_every_run_to_allow_backfill(self):
        calls = []
        batches = [[self.message("new")], [self.message("old", "2026-09-01T00:00:00Z"), self.message("new")]]
        def collector(platform, source_root, since, until=None):
            calls.append((platform, source_root, since, until))
            return {"messages": batches.pop(0), "coverage": {"complete": True, "warnings": [], "scanned_conversations": 1}}
        with patch.object(scold, "_source_collector", return_value=collector):
            self.assertEqual(1, scold.collect(self.data)["inserted"])
            self.assertEqual(1, scold.collect(self.data)["inserted"])
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertEqual(scold.status(self.data)["config"]["enabled_at"], calls[1][2])
        self.assertIsNotNone(calls[0][3])
        self.assertEqual(2, scold.status(self.data)["total_events"])

    def test_before_enabled_at_is_not_imported(self):
        result = self.ingest(self.message("old", "2025-12-31T23:59:59Z"), self.message("new", "2026-01-01T00:00:00Z"))
        self.assertEqual(1, result["inserted"])
        self.assertEqual(1, result["before_enabled_at"])

    def test_invalid_batch_is_atomic(self):
        invalid = self.message("bad")
        invalid["occurred_at"] = "no time"
        with self.assertRaises(scold.ScoldError):
            self.ingest(self.message("good"), invalid)
        self.assertEqual(0, scold.status(self.data)["total_events"])

    def test_review_atomic_unknown_id_invalid_status_and_missing_note(self):
        self.ingest(self.message("m1"), self.message("m2"))
        events = scold.pending(self.data)["events"]
        good = self.decision(events[0]["event_id"])
        invalid_decisions = [self.decision("unknown"), self.decision(events[1]["event_id"], "unknown"),
                             {"event_id": events[1]["event_id"], "status": "scolding"}]
        for bad in invalid_decisions:
            with self.assertRaises(scold.ScoldError):
                scold.review(self.data, [good, bad])
            self.assertEqual(2, scold.pending(self.data)["total_pending"])

    def test_review_is_idempotent_and_allows_correction(self):
        self.ingest(self.message("m1"))
        ident = scold.pending(self.data)["events"][0]["event_id"]
        decision = self.decision(ident)
        self.assertEqual(1, scold.review(self.data, [decision])["updated"])
        with closing(sqlite3.connect(self.data / "ledger.sqlite")) as db:
            before = db.execute("SELECT reviewed_at FROM events").fetchone()[0]
        self.assertEqual(0, scold.review(self.data, [decision])["updated"])
        with closing(sqlite3.connect(self.data / "ledger.sqlite")) as db:
            self.assertEqual(before, db.execute("SELECT reviewed_at FROM events").fetchone()[0])
        self.assertEqual(1, scold.review(self.data, [self.decision(ident, "not_scolding", "引用了他人的话")])["updated"])
        self.assertEqual(1, scold.status(self.data)["counts"]["not_scolding"])

    def test_recollection_preserves_existing_classification(self):
        message = self.message("m1")
        self.ingest(message)
        ident = scold.pending(self.data)["events"][0]["event_id"]
        scold.review(self.data, [self.decision(ident)])
        self.assertEqual(0, self.ingest(message)["inserted"])
        self.assertEqual(0, scold.pending(self.data)["total_pending"])
        self.assertEqual(1, scold.report(self.data, "day", "2026-10-15")["confirmed_scolding_count"])

    def test_recollection_refreshes_source_location_without_rewriting_reviewed_event(self):
        message = self.message("m1")
        self.ingest(message)
        ident = scold.pending(self.data)["events"][0]["event_id"]
        scold.review(self.data, [self.decision(ident)])
        original = scold.report(self.data, "day", "2026-10-15")["scoldings"][0]
        moved = dict(message, source_ref={"path": "native/archived/session.jsonl", "line": 2},
                     text="changed source text must not replace the reviewed original",
                     occurred_at="2026-10-15T16:00:00Z")
        result = self.ingest(moved, at="2026-11-02T00:00:00Z")
        self.assertEqual(0, result["inserted"])
        self.assertEqual(1, result["duplicates"])
        self.assertEqual(1, scold.status(self.data)["total_events"])
        self.assertEqual(0, scold.pending(self.data)["total_pending"])
        after = scold.report(self.data, "day", "2026-10-15")["scoldings"][0]
        self.assertEqual(dict(original, source_ref=moved["source_ref"]), after)
        self.assertEqual(0, scold.report(self.data, "day", "2026-10-16")["confirmed_scolding_count"])

    def test_conflicting_duplicate_decisions_fail_atomically(self):
        self.ingest(self.message("m1"))
        ident = scold.pending(self.data)["events"][0]["event_id"]
        with self.assertRaises(scold.ScoldError):
            scold.review(self.data, [self.decision(ident), self.decision(ident, "uncertain")])
        self.assertEqual(1, scold.pending(self.data)["total_pending"])

    def test_invalid_pagination_and_date_do_not_silently_use_defaults(self):
        for kwargs in ({"limit": 0}, {"offset": -1}, {"limit": True}):
            with self.assertRaises(scold.ScoldError):
                scold.pending(self.data, **kwargs)
        for date in ("20261015", "2026-13-01", "yesterday"):
            with self.assertRaises(scold.ScoldError):
                scold.report(self.data, "day", date)

    def test_pending_and_uncertain_never_count_as_confirmed(self):
        self.ingest(*(self.message(str(i)) for i in range(4)))
        ids = [e["event_id"] for e in scold.pending(self.data)["events"]]
        scold.review(self.data, [self.decision(ids[0]), self.decision(ids[1], "not_scolding"), self.decision(ids[2], "uncertain")])
        result = scold.report(self.data, "day", "2026-10-15")
        self.assertEqual(1, result["confirmed_scolding_count"])
        self.assertEqual({"scolding": 1, "not_scolding": 1, "uncertain": 1, "pending": 1}, result["counts"])
        self.assertEqual(1, result["pending_count"])
        self.assertEqual(1, result["uncertain_count"])
        self.assertEqual("你又没按要求做", result["scoldings"][0]["text"])
        self.assertIn("source_ref", result["scoldings"][0])

    def test_uncertain_events_remain_addressable_for_later_correction(self):
        self.ingest(self.message("m1"))
        ident = scold.pending(self.data)["events"][0]["event_id"]
        scold.review(self.data, [self.decision(ident, "uncertain", "缺少足够上下文")])
        result = scold.report(self.data, "day", "2026-10-15")
        self.assertIn("uncertain_events", result)
        event = result["uncertain_events"][0]
        self.assertEqual(ident, event["event_id"])
        self.assertEqual("你又没按要求做", event["text"])
        self.assertEqual("缺少足够上下文", event["note"])
        self.assertIn("source_ref", event)
        self.assertEqual(0, result["confirmed_scolding_count"])

    def test_local_day_week_and_month_have_half_open_boundaries(self):
        moments = ["2026-09-30T15:59:59Z", "2026-09-30T16:00:00Z",
                   "2026-10-11T15:59:59Z", "2026-10-11T16:00:00Z",
                   "2026-10-14T15:59:59Z", "2026-10-14T16:00:00Z",
                   "2026-10-15T15:59:59Z", "2026-10-15T16:00:00Z",
                   "2026-10-18T15:59:59Z", "2026-10-18T16:00:00Z",
                   "2026-10-31T15:59:59Z", "2026-10-31T16:00:00Z"]
        self.ingest(*(self.message(str(i), moment) for i, moment in enumerate(moments)))
        scold.review(self.data, [self.decision(e["event_id"]) for e in scold.pending(self.data)["events"]])
        self.assertEqual(2, scold.report(self.data, "day", "2026-10-15")["confirmed_scolding_count"])
        week = scold.report(self.data, "week", "2026-10-15")
        self.assertEqual(6, week["confirmed_scolding_count"])
        self.assertEqual("2026-10-12", week["period"]["start_local"][:10])
        self.assertEqual(10, scold.report(self.data, "month", "2026-10-15")["confirmed_scolding_count"])

    def test_dst_day_and_december_month_are_calendar_periods(self):
        other = self.root / "ny"
        scold.initialize(other, "codex", self.source, timezone="America/New_York", since="2026-01-01T00:00:00Z")
        result = scold.report(other, "day", "2026-03-08")["period"]
        from datetime import datetime
        duration = datetime.fromisoformat(result["end"].replace("Z", "+00:00")) - datetime.fromisoformat(result["start"].replace("Z", "+00:00"))
        self.assertEqual(23 * 3600, duration.total_seconds())
        self.assertTrue(scold.report(other, "month", "2026-12-22")["period"]["end_local"].startswith("2027-01-01"))

    def test_no_collection_and_preinstallation_period_are_not_claimed_complete(self):
        uncollected = scold.report(self.data, "day", "2026-10-15")
        self.assertEqual("not_collected", uncollected["coverage"]["status"])
        self.assertFalse(uncollected["coverage"]["complete"])
        self.assertIsNone(uncollected["latest_collection_at"])
        self.assertTrue(uncollected["coverage"]["warnings"])
        self.ingest()
        old = scold.report(self.data, "month", "2025-12-01")
        self.assertEqual("not_collected", old["coverage"]["status"])
        self.assertIsNone(old["coverage"]["observed_from"])
        self.assertTrue(old["coverage"]["warnings"])

    def test_partial_source_coverage_is_visible_even_with_zero_confirmed(self):
        self.ingest(complete=False, warnings=["Source root is missing"])
        result = scold.report(self.data, "day", "2026-10-15")
        self.assertEqual(0, result["confirmed_scolding_count"])
        self.assertFalse(result["coverage"]["complete"])
        self.assertIn("Source root is missing", result["coverage"]["warnings"])
        self.assertIn("enabled_at", result)
        self.assertIn("coverage_start", result)
        self.assertEqual("partial", result["coverage"]["status"])

    def test_closed_period_with_complete_scan_can_be_complete(self):
        self.ingest()
        self.assertTrue(scold.report(self.data, "month", "2026-10-01")["coverage"]["complete"])
        current = scold.report(self.data, "month", "2026-11-01")
        self.assertFalse(current["coverage"]["complete"])

    def test_collection_failure_is_recorded_without_exposing_message_text(self):
        def fail(*args, **kwargs):
            raise ValueError("PRIVATE FULL MESSAGE")
        with patch.object(scold, "_source_collector", return_value=fail):
            with self.assertRaises(scold.ScoldError) as error:
                scold.collect(self.data)
        self.assertNotIn("PRIVATE", str(error.exception))
        result = scold.status(self.data)
        self.assertFalse(result["coverage"]["complete"])
        self.assertIsNone(result["latest_collection_at"])
        self.assertFalse(result["latest_collection_attempt"]["succeeded"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_failed_collection_cannot_extend_observed_coverage_after_prior_success(self):
        from datetime import datetime, timezone
        first_at = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        failed_at = datetime(2026, 10, 16, 0, tzinfo=timezone.utc)
        recovered_at = datetime(2026, 10, 16, 1, tzinfo=timezone.utc)
        scan = {"messages": [], "coverage": {"complete": True, "warnings": [], "scanned_conversations": 1}}
        with patch.object(scold, "_source_collector", return_value=lambda *args, **kwargs: scan), \
                patch.object(scold, "_now", return_value=first_at):
            scold.collect(self.data)
        with patch.object(scold, "_source_collector", side_effect=OSError("PRIVATE SOURCE DETAIL")), \
                patch.object(scold, "_now", return_value=failed_at):
            with self.assertRaises(scold.ScoldError):
                scold.collect(self.data)
        result = scold.report(self.data, "day", "2026-10-15")
        self.assertEqual(scold._iso(first_at), result["latest_collection_at"])
        self.assertEqual(scold._iso(failed_at), result["latest_collection_attempt"]["attempted_at"])
        self.assertFalse(result["latest_collection_attempt"]["succeeded"])
        self.assertEqual("not_collected", result["coverage"]["status"])
        self.assertIsNone(result["coverage"]["observed_from"])
        self.assertIsNone(result["coverage"]["observed_until"])
        self.assertTrue(any("Source collection failed" in warning for warning in result["coverage"]["warnings"]))
        self.assertNotIn("PRIVATE", json.dumps(result))
        with patch.object(scold, "_source_collector", return_value=lambda *args, **kwargs: scan), \
                patch.object(scold, "_now", return_value=recovered_at):
            scold.collect(self.data)
        recovered = scold.report(self.data, "day", "2026-10-15")
        self.assertEqual(scold._iso(recovered_at), recovered["latest_collection_at"])
        self.assertTrue(recovered["latest_collection_attempt"]["succeeded"])
        self.assertEqual("complete", recovered["coverage"]["status"])
        self.assertFalse(any("Source collection failed" in warning for warning in recovered["coverage"]["warnings"]))


class CLITests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), "The scold CLI does not exist yet")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.source = self.root / "codex"

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)
        if ok:
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        return result

    def test_help_and_errors(self):
        self.assertIn("help", self.cli("--help"))
        self.assertIn("help", self.cli("report", "--help"))
        self.cli("status", "--data-dir", self.data, ok=False)

    def test_native_codex_init_collect_review_report_and_json_output_file(self):
        session = self.source / "sessions" / "2026" / "10" / "15" / "rollout-test.jsonl"
        session.parent.mkdir(parents=True)
        rows = [
            {"timestamp": "2026-10-15T01:00:00Z", "type": "session_meta", "payload": {"id": "test-session", "source": "cli"}},
            {"timestamp": "2026-10-15T01:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "你又没按要求做！"}},
            {"timestamp": "2026-10-15T01:00:02Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "抱歉"}},
            {"timestamp": "2026-10-15T01:00:03Z", "type": "event_msg", "payload": {"type": "user_message", "message": "谢谢"}},
        ]
        session.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        initial = self.cli("init", "--data-dir", self.data, "--platform", "codex", "--source-root", self.source, "--timezone", "Asia/Shanghai", "--since", "2026-10-15T00:00:00Z")
        result = self.cli("collect", "--data-dir", self.data)
        self.assertEqual(2, result["inserted"])
        self.assertEqual(0, self.cli("collect", "--data-dir", self.data)["inserted"])
        pending = self.cli("--data-dir", self.data, "pending", "--limit", "5")["events"]
        self.assertEqual(2, len(pending))
        decisions = self.root / "decisions.json"
        decisions.write_text(json.dumps({"decisions": [{"event_id": e["event_id"], "status": "scolding" if "没按" in e["text"] else "not_scolding", "note": "基于当前用户消息判定"} for e in pending]}), encoding="utf-8")
        self.assertEqual(2, self.cli("review", "--data-dir", self.data, "--input", decisions)["updated"])
        output = self.root / "report.json"
        report = self.cli("report", "--data-dir", self.data, "--period", "day", "--date", "2026-10-15", "--output", output)
        self.assertEqual(1, report["confirmed_scolding_count"])
        self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))
        self.assertEqual(initial["config"]["enabled_at"], report["enabled_at"])
        self.assertEqual(0, report["pending_count"])
        decisions.write_text('{"decisions": [{"event_id": "missing", "status": "scolding", "note": "PRIVATE FULL MESSAGE"}]}', encoding="utf-8")
        failed = self.cli("review", "--data-dir", self.data, "--input", decisions, ok=False)
        self.assertNotIn("PRIVATE", failed.stderr)


if __name__ == "__main__":
    unittest.main()
