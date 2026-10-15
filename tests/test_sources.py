"""Synthetic, isolated fixtures; never inspect personal conversation contents."""
import hashlib
import importlib.util
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "sources.py"
if SOURCE.exists():
    SPEC = importlib.util.spec_from_file_location("review_sources", SOURCE)
    SOURCES = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(SOURCES)
else:
    SOURCES = None

SINCE = "2026-10-14T00:00:00+08:00"
UNTIL = "2026-10-15T00:00:00+08:00"
START_MS = 1791907200000  # 2026-10-13 16:00 UTC


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def collect(self, platform="codex", since=SINCE, until=UNTIL):
        self.assertIsNotNone(SOURCES, "the native read-only source collector is not implemented")
        return SOURCES.collect_messages(platform, self.root, since, until)

    def state(self, rows, version=5):
        path = self.root / f"state_{version}.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE threads (id TEXT, source TEXT, cwd TEXT, agent_path TEXT, history_mode TEXT, rollout_path TEXT)")
            db.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", rows)
            db.commit()
        return path

    def history(self, rows, version=1):
        path = self.root / f"thread_history_{version}.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, created_at_ms INTEGER, item_json TEXT, item_type TEXT)")
            db.executemany("INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?)", rows)
            db.commit()
        return path

    def item(self, ident, text, at=START_MS, thread="main", kind="userMessage"):
        payload = {"type": kind, "id": ident, "clientId": ident, "content": [{"type": "text", "text": text}]}
        return (thread, "turn", ident, at, json.dumps(payload), kind)

    def main(self, ident="main", mode="paginated", rollout=None):
        return (ident, "vscode", "/synthetic", None, mode, str(rollout) if rollout else None)

    def jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return path

    def claude(self, ident, text="  exact\n中文  ", **overrides):
        row = {"type": "user", "uuid": ident, "parentUuid": None, "sessionId": "session", "timestamp": "2026-10-13T16:00:00Z", "isSidechain": False, "origin": "user", "entrypoint": "cli", "promptSource": "user", "userType": "human", "message": {"role": "user", "content": text}}
        row.update(overrides)
        return row

    def test_codex_boundaries_utc_and_distinct_same_text(self):
        self.state([self.main()])
        self.history([self.item("before", "omit", START_MS - 1), self.item("one", "  exact\n中文  "), self.item("two", "  exact\n中文  "), self.item("until", "omit", START_MS + 86400000)])
        result = self.collect()
        self.assertEqual([x["message_id"] for x in result["messages"]], ["one", "two"])
        self.assertEqual(result["messages"][0]["occurred_at"], "2026-10-13T16:00:00Z")
        self.assertEqual(result["messages"][0]["text"], "  exact\n中文  ")
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["scanned_conversations"], 1)

    def test_codex_filters_assistant_subagents_and_deduplicates_ids(self):
        self.state([self.main(), ("sub", '{"subagent":{"thread_spawn":{}}}', "/synthetic", None, "paginated", None), ("agent", "vscode", "/synthetic", "agent/path", "paginated", None)])
        one = self.item("one", "hello")
        self.history([one, one, self.item("assistant", "omit", kind="assistantMessage"), self.item("sub", "omit", thread="sub"), self.item("agent", "omit", thread="agent")])
        result = self.collect()
        self.assertEqual([x["message_id"] for x in result["messages"]], ["one"])
        self.assertEqual(self.collect(), result)
        self.assertTrue(result["coverage"]["complete"])

    def test_codex_non_null_agent_path_is_excluded_even_when_empty(self):
        self.state([self.main(), ("agent", "vscode", "/synthetic", "", "paginated", None)])
        self.history([self.item("human", "yes"), self.item("agent", "omit", thread="agent")])
        self.assertEqual([x["message_id"] for x in self.collect()["messages"]], ["human"])

    def test_codex_local_images_preserve_surrounding_text_and_identity(self):
        self.state([self.main()])
        row = list(self.item("with-images", "unused"))
        payload = json.loads(row[4])
        payload["content"] = [
            {"type": "localImage", "path": "/nonexistent/first.png"},
            {"type": "text", "text": "  原文\n第一段  "},
            {"type": "localImage", "path": "/nonexistent/second.png", "text": "not user text", "data": "not an image to decode"},
            {"type": "input_text", "text": " 第二段 "},
        ]
        row[4] = json.dumps(payload)
        source = self.history([row])
        before = source.read_bytes()
        result = self.collect()
        self.assertEqual([x["text"] for x in result["messages"]], ["  原文\n第一段  \n 第二段 "])
        self.assertEqual(result["messages"][0]["message_id"], "with-images")
        self.assertEqual(result["messages"][0]["occurred_at"], "2026-10-13T16:00:00Z")
        self.assertEqual(result["messages"][0]["source_ref"], f"{source.resolve()}#thread_items/main/with-images")
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["warnings"], [])
        self.assertEqual(self.collect(), result)
        self.assertEqual(source.read_bytes(), before)

    def test_codex_local_image_only_does_not_invent_text(self):
        self.state([self.main()])
        row = list(self.item("image-only", "unused"))
        payload = json.loads(row[4])
        payload["content"] = [{"type": "localImage", "path": "/nonexistent/image.png"}]
        row[4] = json.dumps(payload)
        self.history([row])
        result = self.collect()
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["coverage"]["warnings"], [])

    def test_codex_unknown_attachment_still_marks_source_incomplete(self):
        self.state([self.main()])
        row = list(self.item("unknown", "keep caution"))
        payload = json.loads(row[4])
        payload["content"].append({"type": "localImage", "path": "/nonexistent/image.png"})
        payload["content"].append({"type": "futureAttachment-v999"})
        row[4] = json.dumps(payload)
        self.history([row])
        result = self.collect()
        self.assertEqual(result["messages"], [])
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(result["coverage"]["warnings"])

    def test_codex_latest_numeric_version(self):
        self.state([self.main("old")], version=5)
        self.state([self.main("main")], version=10)
        self.history([self.item("old", "omit", thread="old")], version=2)
        self.history([self.item("new", "yes")], version=10)
        self.assertEqual([x["message_id"] for x in self.collect()["messages"]], ["new"])

    def test_codex_paginated_preferred_over_rollout(self):
        path = self.jsonl(self.root / "sessions" / "main.jsonl", [{"timestamp": "2026-10-13T16:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "duplicate legacy"}}])
        self.state([self.main(rollout=path)])
        self.history([self.item("one", "authoritative")])
        self.assertEqual([x["text"] for x in self.collect()["messages"]], ["authoritative"])

    def test_codex_legacy_event_only_and_read_only(self):
        path = self.jsonl(self.root / "sessions" / "nested" / "main.jsonl", [
            {"type": "session_meta", "payload": {"id": "main", "source": "cli"}},
            {"timestamp": "2026-10-13T16:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "  exact\n中文  "}},
            {"timestamp": "2026-10-13T16:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "duplicate"}]}},
        ])
        state = self.state([self.main(mode="legacy", rollout=path)])
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (state, path)}
        result = self.collect()
        self.assertEqual([x["text"] for x in result["messages"]], ["  exact\n中文  "])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (state, path)})
        self.assertTrue(result["coverage"]["complete"])

    def test_codex_legacy_archive_move_does_not_duplicate_ledger_entries(self):
        event = {"timestamp": "2026-10-13T16:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "same text, separate messages"}}
        path = self.jsonl(self.root / "sessions" / "main.jsonl", [
            {"type": "session_meta", "payload": {"id": "main", "source": "cli"}},
            event, event,
        ])
        state = self.state([self.main(mode="legacy", rollout=path)])
        spec = importlib.util.spec_from_file_location("review_scold_for_sources", SOURCE.with_name("scold.py"))
        ledger = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ledger)
        data = self.root / "test-ledger"
        ledger.initialize(data, "codex", self.root, since=SINCE)
        first = self.collect()
        self.assertEqual(len({x["message_id"] for x in first["messages"]}), 2)
        self.assertEqual(ledger.ingest_messages(data, first["messages"], first["coverage"])["inserted"], 2)

        archived = self.root / "archived_sessions" / "main.jsonl"
        archived.parent.mkdir()
        path.rename(archived)
        with closing(sqlite3.connect(state)) as db:
            db.execute("UPDATE threads SET rollout_path = ? WHERE id = 'main'", (str(archived),))
            db.commit()
        second = self.collect()
        self.assertTrue(second["coverage"]["complete"])
        self.assertTrue(all(x["source_ref"].startswith(str(archived)) for x in second["messages"]))
        self.assertEqual(ledger.ingest_messages(data, second["messages"], second["coverage"])["inserted"], 0)
        self.assertEqual(ledger.status(data)["total_events"], 2)
        self.assertEqual([x["message_id"] for x in second["messages"]], [x["message_id"] for x in first["messages"]])

    def test_codex_legacy_identity_ignores_nonmessage_lines_and_scan_window(self):
        old = {"timestamp": "2026-10-13T15:59:59Z", "type": "event_msg", "payload": {"type": "user_message", "message": "same text"}}
        current = {"timestamp": "2026-10-13T16:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "same text"}}
        rows = [{"type": "session_meta", "payload": {"id": "main", "source": "cli"}}, old, current, current]
        path = self.jsonl(self.root / "sessions" / "main.jsonl", rows)
        self.state([self.main(mode="legacy", rollout=path)])
        first = self.collect()
        self.assertEqual(len(first["messages"]), 2)
        # Inserting unrelated metadata must not change a user event's identity.
        self.jsonl(path, [rows[0], {"type": "turn_context", "payload": {}}, *rows[1:]])
        second = self.collect(since="2026-10-13T15:59:59Z")
        current_ids = [x["message_id"] for x in second["messages"] if x["occurred_at"] == "2026-10-13T16:00:00Z"]
        self.assertEqual(current_ids, [x["message_id"] for x in first["messages"]])
        self.assertEqual(len({x["message_id"] for x in second["messages"]}), 3)

    def test_codex_missing_index_falls_back_to_sessions_but_incomplete(self):
        self.jsonl(self.root / "sessions" / "main.jsonl", [{"type": "session_meta", "payload": {"id": "main", "source": "cli"}}, {"timestamp": "2026-10-13T16:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "legacy"}}])
        self.jsonl(self.root / "sessions" / "sub.jsonl", [{"type": "session_meta", "payload": {"id": "sub", "source": {"subagent": {"thread_spawn": {}}}}}, {"timestamp": "2026-10-13T16:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "omit"}}])
        result = self.collect()
        self.assertEqual([x["text"] for x in result["messages"]], ["legacy"])
        self.assertFalse(result["coverage"]["complete"])

    def test_codex_missing_history_never_claims_complete(self):
        self.state([self.main()])
        result = self.collect()
        self.assertEqual(result["messages"], [])
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(result["coverage"]["warnings"])

    def test_codex_bad_json_or_user_schema_marks_incomplete(self):
        self.state([self.main()])
        self.history([self.item("good", "yes"), ("main", "turn", "bad", START_MS, "{", "userMessage"), ("main", "turn", "schema", START_MS, '{"type":"userMessage","content":42}', "userMessage")])
        result = self.collect()
        self.assertEqual([x["message_id"] for x in result["messages"]], ["good"])
        self.assertFalse(result["coverage"]["complete"])

    def test_codex_unknown_latest_schema_not_silently_skipped(self):
        self.state([self.main()], version=5)
        with closing(sqlite3.connect(self.root / "state_10.sqlite")) as db:
            db.execute("CREATE TABLE future_schema (id TEXT)")
        self.assertFalse(self.collect()["coverage"]["complete"])

    def test_codex_unknown_history_schema(self):
        self.state([self.main()])
        with closing(sqlite3.connect(self.root / "thread_history_1.sqlite")) as db:
            db.execute("CREATE TABLE future_schema (id TEXT)")
        self.assertFalse(self.collect()["coverage"]["complete"])

    def test_codex_invalid_timestamp_cannot_be_silently_lost_by_date_filter(self):
        self.state([self.main()])
        self.history([self.item("good", "yes"), self.item("missing-time", "omit", at=None), self.item("invalid-time", "omit", at="not-a-timestamp")])
        result = self.collect()
        self.assertEqual([x["message_id"] for x in result["messages"]], ["good"])
        self.assertFalse(result["coverage"]["complete"])

    def test_missing_sources_are_incomplete(self):
        for platform in ("codex", "claude-code"):
            with self.subTest(platform=platform):
                result = self.collect(platform)
                self.assertFalse(result["coverage"]["complete"])
                self.assertTrue(result["coverage"]["warnings"])

    def test_claude_boundaries_same_text_and_repeated_ids(self):
        row = self.claude("one")
        self.jsonl(self.root / "projects" / "project" / "session.jsonl", [self.claude("before", timestamp="2026-10-13T15:59:59.999Z"), row, row, self.claude("two"), self.claude("until", timestamp="2026-10-14T16:00:00Z")])
        result = self.collect("claude-code")
        self.assertEqual([x["message_id"] for x in result["messages"]], ["one", "two"])
        self.assertEqual(result["messages"][0]["text"], "  exact\n中文  ")
        self.assertEqual(result["messages"][0]["occurred_at"], "2026-10-13T16:00:00Z")
        self.assertEqual(self.collect("claude-code"), result)
        self.assertTrue(result["coverage"]["complete"])

    def test_claude_filters_assistant_tools_sidechain_synthetic_and_subdirectories(self):
        self.jsonl(self.root / "projects" / "project" / "session.jsonl", [
            self.claude("human"),
            self.claude("assistant", type="assistant", message={"role": "assistant", "content": "omit"}),
            self.claude("tool", message={"role": "user", "content": [{"type": "tool_result", "content": "omit"}]}),
            self.claude("sidechain", isSidechain=True),
            self.claude("synthetic", origin="synthetic"),
            self.claude("hook", promptSource="hook"),
            self.claude("system", userType="system"),
            self.claude("meta", isMeta=True),
        ])
        self.jsonl(self.root / "projects" / "project" / "subagents" / "agent.jsonl", [self.claude("subagent")])
        result = self.collect("claude-code")
        self.assertEqual([x["message_id"] for x in result["messages"]], ["human"])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["scanned_conversations"], 1)

    def test_claude_text_blocks_ignore_tool_result_content(self):
        self.jsonl(self.root / "projects" / "project" / "session.jsonl", [self.claude("one", message={"role": "user", "content": [{"type": "text", "text": " a "}, {"type": "tool_result", "content": "omit"}, {"type": "text", "text": " b "}]})])
        self.assertEqual(self.collect("claude-code")["messages"][0]["text"], " a \n b ")

    def test_claude_bad_json_and_partial_tail_are_incomplete_but_keep_good_rows(self):
        path = self.jsonl(self.root / "projects" / "project" / "session.jsonl", [self.claude("good")])
        with path.open("a", encoding="utf-8") as stream:
            stream.write("not-json\n{\"type\": \"user\"")
        result = self.collect("claude-code")
        self.assertEqual([x["message_id"] for x in result["messages"]], ["good"])
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(result["coverage"]["warnings"])

    def test_claude_unknown_user_schema_is_incomplete(self):
        self.jsonl(self.root / "projects" / "project" / "session.jsonl", [self.claude("bad", message={"role": "user", "content": 42})])
        result = self.collect("claude-code")
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["messages"], [])

    def test_claude_known_nonmessage_metadata_does_not_reduce_coverage(self):
        metadata = [
            {"type": "mode", "sessionId": "session", "mode": "synthetic-mode"},
            {"type": "permission-mode", "sessionId": "session", "permissionMode": "synthetic-permission"},
            {"type": "atis-latch", "sessionId": "session", "atis": False},
            {"type": "ai-title", "sessionId": "session", "aiTitle": "Synthetic fixture"},
            {"type": "cost-state", "sessionId": "session", "modelUsage": {}, "totalCostUSD": 0},
            {"type": "attachment", "sessionId": "session", "uuid": "attachment-1", "timestamp": "2026-10-13T16:00:00Z", "attachment": {"type": "synthetic-fixture", "text": "must not become a human message"}},
        ]
        self.jsonl(self.root / "projects" / "project" / "session.jsonl", [self.claude("human"), *metadata])
        result = self.collect("claude-code")
        self.assertEqual([x["message_id"] for x in result["messages"]], ["human"])
        self.assertEqual(result["coverage"]["warnings"], [])
        self.assertTrue(result["coverage"]["complete"])

    def test_claude_unknown_future_record_type_still_reduces_coverage(self):
        self.jsonl(self.root / "projects" / "project" / "session.jsonl", [self.claude("human"), {"type": "future-record-v999", "sessionId": "session"}])
        result = self.collect("claude-code")
        self.assertEqual([x["message_id"] for x in result["messages"]], ["human"])
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(result["coverage"]["warnings"])

    def test_requires_timezone_and_valid_platform(self):
        self.assertIsNotNone(SOURCES, "the native read-only source collector is not implemented")
        for platform, since, until in [("other", SINCE, UNTIL), ("codex", "2026-10-14T00:00:00", UNTIL), ("codex", UNTIL, SINCE)]:
            with self.subTest(platform=platform, since=since):
                with self.assertRaises(ValueError):
                    SOURCES.collect_messages(platform, self.root, since, until)

    def test_relative_source_root_still_produces_absolute_source_reference(self):
        self.jsonl(self.root / "projects" / "project" / "session.jsonl", [self.claude("one")])
        self.assertIsNotNone(SOURCES)
        result = SOURCES.collect_messages("claude-code", os.path.relpath(self.root), SINCE, UNTIL)
        self.assertTrue(result["messages"][0]["source_ref"].startswith(str(self.root.resolve())))


if __name__ == "__main__":
    unittest.main()
