"""Evidence-bound reason reviews; isolated synthetic ledgers only."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scold
try:
    import reasons
except ImportError:
    reasons = None


class ReasonTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(reasons, "The evidence-bound reason ledger is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "ledger"
        scold.initialize(self.data, "codex", Path(self.temp.name) / "source",
                         since="2026-01-01T00:00:00Z")
        with closing(scold._database(self.data)) as db, db:
            reasons.ensure_schema(db)

    def event(self, message_id="m1", status="scolding", text="你这解释太差了，条件为什么没讲清楚"):
        message = {"platform": "codex", "conversation_id": "c1", "message_id": message_id,
                   "occurred_at": "2026-05-12T08:12:34Z", "text": text,
                   "source_ref": "native/session.jsonl:" + message_id}
        scold.ingest_messages(self.data, [message])
        event_id = scold.event_id_for("codex", "c1", message_id)
        if status != "pending":
            scold.review(self.data, [{"event_id": event_id, "status": status, "note": "synthetic review"}])
        return event_id

    def native_context(self, message_id="m1"):
        return {"messages": [
            {"message_id": "a1", "role": "assistant", "occurred_at": "2026-05-12T08:12:00Z",
             "text": "导出文件会保存到下载目录。", "source_ref": "native/session.jsonl:a1"},
            {"message_id": message_id, "role": "user", "occurred_at": "2026-05-12T08:12:34Z",
             "text": "你这解释太差了，条件为什么没讲清楚", "source_ref": "native/session.jsonl:" + message_id}],
            "target_found": True, "coverage": {"complete": True, "warnings": [], "truncated": False}}

    def snapshot(self, event_id, message_id="m1", result=None):
        with patch.object(reasons, "_context_reader", return_value=lambda *a, **k: result or self.native_context(message_id)):
            return reasons.context_for(self.data, event_id)

    def analysis(self, event_id, snapshot, **changes):
        value = {"event_id": event_id, "context_id": snapshot["context_id"],
                 "category": "unclear_communication", "basis": "user_stated",
                 "summary": "用户明确抱怨说明不清楚。",
                 "evidence": [{"message_id": snapshot["event"]["message_id"], "quote": "为什么没讲清楚"}]}
        value.update(changes)
        return value

    def annotated(self):
        with closing(scold._database(self.data)) as db:
            events = [scold._event(row) for row in db.execute("SELECT * FROM events WHERE status='scolding' ORDER BY event_id")]
            summary = reasons.annotate_report(db, events)
        return events, summary

    def test_pending_contains_only_unanalyzed_confirmed_events(self):
        expected = self.event()
        self.event("m2", "not_scolding")
        self.event("m3", "uncertain")
        self.event("m4", "pending")
        result = reasons.pending(self.data)
        self.assertEqual(1, result["total_pending"])
        self.assertEqual(expected, result["events"][0]["event_id"])
        self.assertEqual("你这解释太差了，条件为什么没讲清楚", result["events"][0]["text"])

    def test_context_is_persisted_and_repeat_hash_is_stable(self):
        event_id = self.event()
        first = self.snapshot(event_id)
        self.assertEqual(first, self.snapshot(event_id))
        with closing(scold._database(self.data)) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM reason_contexts").fetchone()[0])

    def test_context_failure_uses_only_ledger_target(self):
        event_id = self.event()
        for native in ({"messages": self.native_context()["messages"], "target_found": False,
                        "coverage": {"complete": False, "warnings": ["target missing"], "truncated": False}}, None):
            reader = (lambda *a, **k: native) if native else (lambda *a, **k: (_ for _ in ()).throw(OSError("private source details")))
            with patch.object(reasons, "_context_reader", return_value=reader):
                result = reasons.context_for(self.data, event_id)
            self.assertFalse(result["target_found"])
            self.assertFalse(result["coverage"]["complete"])
            self.assertTrue(result["coverage"]["warnings"])
            self.assertEqual(["m1"], [m["message_id"] for m in result["messages"]])
            self.assertEqual("你这解释太差了，条件为什么没讲清楚", result["messages"][0]["text"])

    def test_forged_quote_or_foreign_context_is_rejected(self):
        first, second = self.event(), self.event("m2")
        own, foreign = self.snapshot(first), self.snapshot(second, "m2")
        for changes in ({"context_id": foreign["context_id"]},
                        {"evidence": [{"message_id": "m1", "quote": "从未说过的原因"}]},
                        {"evidence": [{"message_id": "absent", "quote": "为什么没讲清楚"}]},
                        {"evidence": [{"message_id": "m1", "quote": " "}]}):
            with self.assertRaises(scold.ScoldError):
                reasons.review(self.data, [self.analysis(first, own, **changes)])
        self.assertEqual(2, reasons.pending(self.data)["total_pending"])

    def test_fallback_respects_character_limit(self):
        event_id = self.event(text="原始消息" * 100)
        with patch.object(reasons, "_context_reader", return_value=lambda *a, **k: {"target_found": False}):
            snapshot = reasons.context_for(self.data, event_id, max_chars=7)
        self.assertEqual(7, sum(len(message["text"]) for message in snapshot["messages"]))
        self.assertTrue(snapshot["coverage"]["truncated"])
        self.assertEqual(("原始消息" * 100)[:7], snapshot["messages"][0]["text"])

    def test_changed_native_target_keeps_original_ledger_text_with_warning(self):
        event_id = self.event()
        native = self.native_context()
        native["messages"][-1]["text"] = "变化后的原生日志不再符合已审阅事件"
        snapshot = self.snapshot(event_id, result=native)
        self.assertEqual("你这解释太差了，条件为什么没讲清楚", snapshot["messages"][-1]["text"])
        self.assertFalse(snapshot["coverage"]["complete"])
        self.assertTrue(snapshot["coverage"]["warnings"])

    def test_truncated_native_target_is_valid_and_cannot_cite_hidden_tail(self):
        event_id = self.event()
        native = self.native_context()
        native["messages"] = native["messages"][-1:]
        native["messages"][0]["text"] = "你这解释"
        native["messages"][0]["text_truncated"] = True
        native["coverage"] = {"complete": False, "truncated": True, "warnings": ["Target text truncated"]}
        snapshot = self.snapshot(event_id, result=native)
        self.assertTrue(snapshot["target_found"])
        self.assertEqual("你这解释", snapshot["messages"][0]["text"])
        with self.assertRaises(scold.ScoldError):
            reasons.review(self.data, [self.analysis(event_id, snapshot)])

    def test_user_stated_requires_user_evidence_and_metadata_is_derived(self):
        event_id = self.event()
        snapshot = self.snapshot(event_id)
        assistant_only = [{"message_id": "a1", "quote": "导出文件", "role": "user"}]
        with self.assertRaises(scold.ScoldError):
            reasons.review(self.data, [self.analysis(event_id, snapshot, evidence=assistant_only)])
        self.assertEqual(1, reasons.review(self.data, [self.analysis(event_id, snapshot,
                          basis="context_inferred", evidence=assistant_only)])["updated"])
        evidence = self.annotated()[0][0]["reason"]["evidence"][0]
        self.assertEqual("assistant", evidence["role"])
        self.assertEqual("native/session.jsonl:a1", evidence["source_ref"])

    def test_unknown_is_analyzed_but_not_pending(self):
        unknown, pending = self.event(), self.event("m2")
        snapshot = self.snapshot(unknown)
        analysis = self.analysis(unknown, snapshot, category="unknown", basis="unknown",
                                 summary="现有证据不足以判断原因。", evidence=[])
        reasons.review(self.data, [analysis])
        events, summary = self.annotated()
        self.assertEqual(1, summary["analyzed_count"])
        self.assertEqual(1, summary["unknown_count"])
        self.assertEqual(1, summary["pending_count"])
        self.assertEqual(1, summary["categories"]["unknown"])
        self.assertEqual(2, sum(summary["categories"].values()) + summary["pending_count"])
        self.assertEqual([pending], [e["event_id"] for e in events if e["reason"] is None])

    def test_invalid_basis_category_or_empty_evidence_is_rejected(self):
        event_id = self.event()
        snapshot = self.snapshot(event_id)
        for changes in ({"basis": "proven"}, {"category": "invented"},
                        {"basis": "unknown"}, {"category": "unknown"},
                        {"evidence": []}, {"summary": " "}):
            with self.assertRaises(scold.ScoldError):
                reasons.review(self.data, [self.analysis(event_id, snapshot, **changes)])

    def test_review_is_atomic_and_rejects_current_non_scolding(self):
        first, second = self.event(), self.event("m2")
        a, b = self.snapshot(first), self.snapshot(second, "m2")
        scold.review(self.data, [{"event_id": second, "status": "not_scolding", "note": "用户纠正为引用"}])
        with self.assertRaises(scold.ScoldError):
            reasons.review(self.data, [self.analysis(first, a), self.analysis(second, b)])
        self.assertEqual(1, reasons.pending(self.data)["total_pending"])
        with closing(scold._database(self.data)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM reason_reviews").fetchone()[0])

    def test_identical_review_does_not_change_analysis_time_and_correction_updates(self):
        event_id = self.event()
        snapshot = self.snapshot(event_id)
        analysis = self.analysis(event_id, snapshot)
        self.assertEqual(1, reasons.review(self.data, [analysis])["updated"])
        original = self.annotated()[0][0]["reason"]
        self.assertEqual({"processed": 1, "updated": 0, "unchanged": 1}, reasons.review(self.data, [deepcopy(analysis)]))
        self.assertEqual(original, self.annotated()[0][0]["reason"])
        analysis["summary"] = "用户纠正了具体解释。"
        self.assertEqual(1, reasons.review(self.data, [analysis])["updated"])
        corrected = self.annotated()[0][0]["reason"]
        self.assertEqual(1, corrected["taxonomy_version"])
        self.assertEqual("用户纠正了具体解释。", corrected["summary"])
        self.assertEqual(snapshot["coverage"], corrected["context_coverage"])

    def test_conflicting_duplicate_analysis_rolls_back(self):
        event_id = self.event()
        snapshot = self.snapshot(event_id)
        a = self.analysis(event_id, snapshot)
        with self.assertRaises(scold.ScoldError):
            reasons.review(self.data, [a, dict(a, summary="different")])
        self.assertEqual(1, reasons.pending(self.data)["total_pending"])

    def test_invalidate_uses_callers_transaction_and_no_old_reason_revives(self):
        event_id = self.event()
        snapshot = self.snapshot(event_id)
        reasons.review(self.data, [self.analysis(event_id, snapshot)])
        with closing(scold._database(self.data)) as db:
            db.execute("BEGIN IMMEDIATE")
            reasons.ensure_schema(db)
            reasons.invalidate(db, event_id)
            db.rollback()
        self.assertEqual(0, reasons.pending(self.data)["total_pending"])
        with closing(scold._database(self.data)) as db, db:
            reasons.invalidate(db, event_id)
        self.assertEqual(1, reasons.pending(self.data)["total_pending"])
        self.assertIsNone(self.annotated()[0][0]["reason"])

    def test_pending_rejects_bad_pagination(self):
        for changes in ({"limit": 0}, {"limit": True}, {"offset": -1}, {"offset": "0"}):
            with self.assertRaises(scold.ScoldError):
                reasons.pending(self.data, **changes)

    def test_context_rejects_non_scolding_and_invalid_window(self):
        not_scolding = self.event(status="not_scolding")
        with self.assertRaises(scold.ScoldError):
            reasons.context_for(self.data, not_scolding)
        event_id = self.event("m2")
        for changes in ({"max_messages": 0}, {"max_chars": -1}, {"max_messages": True}):
            with self.assertRaises(scold.ScoldError):
                reasons.context_for(self.data, event_id, **changes)


if __name__ == "__main__":
    unittest.main()
