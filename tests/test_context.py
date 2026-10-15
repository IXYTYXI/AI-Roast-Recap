"""Phase-two context fixtures contain synthetic conversations only."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "context.py"
if SOURCE.exists():
    SPEC = importlib.util.spec_from_file_location("review_context", SOURCE)
    CONTEXT = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(CONTEXT)
else:
    CONTEXT = None

AT = 1778573554567
STAMP = "2026-05-12T08:12:34.567Z"
HEARTBEAT = "<heartbeat>\n <automation_id>fixture-automation</automation_id>\n <current_time_iso>2026-05-12T08:10:00Z</current_time_iso>\n <instructions>synthetic scheduled instruction</instructions>\n</heartbeat>\n"


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def state(self, source="vscode", agent_path=None, mode="paginated", rollout=None):
        path = self.root / "state_5.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE threads (id TEXT, source TEXT, agent_path TEXT, history_mode TEXT, rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?)", ("main", source, agent_path, mode, str(rollout) if rollout else None))
            db.commit()
        return path

    def item(self, ident, text, ordinal, role="user", at=AT, thread="main", phase=None, blocks=None):
        kind = "userMessage" if role == "user" else "agentMessage"
        obj = {"type": kind, "id": ident}
        if role == "user":
            obj["content"] = blocks or [{"type": "text", "text": text}]
        else:
            obj.update(text=text, phase=phase or "final_answer")
        return (thread, ident, ordinal, at, json.dumps(obj), kind)

    def history(self, rows, ordinal=True):
        path = self.root / "thread_history_1.sqlite"
        with closing(sqlite3.connect(path)) as db:
            if ordinal:
                db.execute("CREATE TABLE thread_items (thread_id TEXT, item_id TEXT, rollout_ordinal INTEGER, created_at_ms INTEGER, item_json TEXT, item_type TEXT)")
                db.executemany("INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?)", rows)
            else:
                db.execute("CREATE TABLE thread_items (thread_id TEXT, item_id TEXT, created_at_ms INTEGER, item_json TEXT, item_type TEXT)")
                db.executemany("INSERT INTO thread_items VALUES (?, ?, ?, ?, ?)", [r[:2] + r[3:] for r in rows])
            db.commit()
        return path

    def jsonl(self, rows, path=None):
        path = path or self.root / "projects" / "project" / "main.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return path

    def claude(self, ident, text, parent, role="user", **kw):
        obj = {"type": role, "uuid": ident, "parentUuid": parent, "sessionId": "main", "timestamp": STAMP, "isSidechain": False, "message": {"role": role, "content": text}}
        obj.update(kw)
        return obj

    def read(self, platform="codex", target="target", **kw):
        self.assertIsNotNone(CONTEXT, "context reader is not implemented")
        return CONTEXT.read_context(platform, self.root, "main", target, **kw)

    def test_codex_native_order_includes_agent_messages_excludes_after_and_other_threads(self):
        self.state()
        history = self.history([
            self.item("question", "original task", 1),
            self.item("answer", "bad answer", 2, role="assistant"),
            self.item("target", "complaint", 3),
            self.item("after", "apology is phase three", 4, role="assistant"),
            self.item("other", "private other conversation", 0, thread="other"),
        ])
        before = history.read_bytes()
        result = self.read()
        self.assertTrue(result["target_found"])
        self.assertEqual([r["message_id"] for r in result["messages"]], ["question", "answer", "target"])
        self.assertEqual([r["role"] for r in result["messages"]], ["user", "assistant", "user"])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["messages"][-1]["occurred_at"], "2026-05-12T08:12:34.567000Z")
        self.assertEqual(history.read_bytes(), before)

    def test_codex_fallback_timestamp_omits_same_millisecond_context(self):
        self.state()
        self.history([self.item("early", "safe", 1, at=AT-1), self.item("ambiguous", "unknown relative order", 2, role="assistant"), self.item("target", "complaint", 3), self.item("after", "exclude", 4, role="assistant")], ordinal=False)
        result = self.read()
        self.assertEqual([r["message_id"] for r in result["messages"]], ["early", "target"])
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(result["coverage"]["warnings"])

    def test_codex_target_must_exist_and_be_human_user(self):
        self.state()
        self.history([self.item("answer", "not a user", 1, role="assistant")])
        for target in ("missing", "answer"):
            with self.subTest(target=target):
                result = self.read(target=target)
                self.assertFalse(result["target_found"])
                self.assertEqual(result["messages"], [])
                self.assertFalse(result["coverage"]["complete"])

    def test_codex_subagent_source_and_agent_path_are_excluded(self):
        self.state(agent_path="subagent")
        self.history([self.item("target", "exclude", 1)])
        self.assertFalse(self.read()["target_found"])

    def test_codex_ignores_reasoning_tools_and_preserves_image_adjacent_text(self):
        self.state()
        tool = ("main", "tool", 2, AT, '{"type":"commandExecution","text":"tool output"}', "commandExecution")
        self.history([self.item("thought", "private reasoning", 1, role="assistant", phase="analysis"), tool, self.item("visible", "visible progress", 3, role="assistant", phase="commentary"), self.item("target", "", 4, blocks=[{"type": "localImage", "path": "/never-open.png"}, {"type": "text", "text": " exact complaint "}])])
        result = self.read()
        self.assertEqual([r["text"] for r in result["messages"]], ["visible progress", " exact complaint "])

    def test_codex_host_heartbeat_is_not_user_context_or_a_human_target(self):
        self.state()
        heartbeat = list(self.item("heartbeat", HEARTBEAT, 2))
        obj = json.loads(heartbeat[4])
        obj["clientId"] = None
        heartbeat[4] = json.dumps(obj)
        self.history([self.item("question", "human question", 1), tuple(heartbeat), self.item("target", "human complaint", 3)])
        result = self.read()
        self.assertEqual([r["message_id"] for r in result["messages"]], ["question", "target"])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["warnings"], [])
        target = self.read(target="heartbeat")
        self.assertFalse(target["target_found"])
        self.assertEqual(target["messages"], [])

    def test_codex_heartbeat_quotes_and_non_matching_wrappers_remain_user_text(self):
        cases = [
            ("keyword", "Please explain heartbeat scheduling", None),
            ("preface", "这是一个例子：\n" + HEARTBEAT, None),
            ("suffix", HEARTBEAT + "请解释", None),
            ("client-owned", HEARTBEAT, "human-client-id"),
            ("invalid-time", HEARTBEAT.replace("2026-05-12T08:10:00Z", "not-a-time"), None),
            ("naive-time", HEARTBEAT.replace("2026-05-12T08:10:00Z", "2026-05-12T08:10:00"), None),
            ("missing-field", HEARTBEAT.replace(" <automation_id>fixture-automation</automation_id>\n", ""), None),
            ("duplicate-field", HEARTBEAT.replace("</heartbeat>", "<automation_id>other</automation_id></heartbeat>"), None),
            ("root-attribute", HEARTBEAT.replace("<heartbeat>", '<heartbeat example="yes">'), None),
            ("nested-content", HEARTBEAT.replace("synthetic scheduled instruction", "<example>quoted</example>"), None),
            ("root-prose", HEARTBEAT.replace("<heartbeat>", "<heartbeat>This is a quote"), None),
        ]
        self.state()
        rows = []
        for ordinal, (ident, text, client) in enumerate(cases, 1):
            row = list(self.item(ident, text, ordinal))
            obj = json.loads(row[4])
            obj["clientId"] = client
            row[4] = json.dumps(obj)
            rows.append(tuple(row))
        # Missing clientId and assistant messages do not match the observed
        # native host wrapper even when their text is identical.
        rows.extend([self.item("missing-client", HEARTBEAT, 20), self.item("assistant", HEARTBEAT, 21, role="assistant"), self.item("target", "human complaint", 22)])
        self.history(rows)
        result = self.read(max_messages=30)
        self.assertEqual([r["message_id"] for r in result["messages"]], [case[0] for case in cases] + ["missing-client", "assistant", "target"])
        self.assertTrue(result["coverage"]["complete"])

    def test_window_limits_are_honest_and_keep_target(self):
        self.state()
        self.history([self.item("one", "1111", 1), self.item("two", "2222", 2, role="assistant"), self.item("target", "3333", 3)])
        for kw in ({"max_messages": 1}, {"max_chars": 6}):
            with self.subTest(kw=kw):
                result = self.read(**kw)
                self.assertEqual([r["message_id"] for r in result["messages"]], ["target"])
                self.assertTrue(result["coverage"]["truncated"])
                self.assertFalse(result["coverage"]["complete"])

    def test_oversized_target_obeys_hard_limit_with_explicit_warning(self):
        self.state()
        self.history([self.item("target", "long exact original", 1)])
        result = self.read(max_chars=5)
        self.assertEqual(result["messages"][0]["text"], "long ")
        self.assertTrue(result["messages"][0]["text_truncated"])
        self.assertTrue(result["coverage"]["truncated"])
        self.assertTrue(result["coverage"]["warnings"])

    def test_legacy_uses_collector_identity_ignores_user_echo_and_after_reply(self):
        rows = [
            {"type": "session_meta", "payload": {"id": "main", "source": "cli"}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "user_message", "message": "task"}},
            {"type": "response_item", "timestamp": STAMP, "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "duplicate task"}]}},
            {"type": "response_item", "timestamp": STAMP, "payload": {"type": "message", "role": "assistant", "id": "answer", "phase": "final_answer", "content": [{"type": "output_text", "text": "bad answer"}]}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "user_message", "message": "complaint"}},
            {"type": "response_item", "timestamp": STAMP, "payload": {"type": "message", "role": "assistant", "id": "after", "phase": "final_answer", "content": [{"type": "output_text", "text": "exclude"}]}},
        ]
        path = self.jsonl(rows, self.root / "sessions" / "main.jsonl")
        self.state(mode="legacy", rollout=path)
        target = "legacy:2026-05-12T08:12:34.567000Z:2"
        result = self.read(target=target)
        self.assertTrue(result["target_found"])
        self.assertEqual([r["text"] for r in result["messages"]], ["task", "bad answer", "complaint"])
        self.assertEqual(result["messages"][-1]["message_id"], target)

    def test_legacy_invalid_text_still_counts_native_timestamp_occurrence(self):
        path = self.jsonl([
            {"type": "session_meta", "payload": {"id": "main", "source": "cli"}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "user_message", "message": None}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "agent_message", "message": "visible answer"}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "user_message", "message": "complaint"}},
        ], self.root / "sessions" / "main.jsonl")
        self.state(mode="legacy", rollout=path)
        result = self.read(target="legacy:2026-05-12T08:12:34.567000Z:2")
        self.assertTrue(result["target_found"])
        self.assertEqual([r["text"] for r in result["messages"]], ["visible answer", "complaint"])
        self.assertFalse(result["coverage"]["complete"])

    def test_legacy_session_metadata_must_match_requested_conversation(self):
        path = self.jsonl([{"type": "session_meta", "payload": {"id": "other", "source": "cli"}}, {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "user_message", "id": "target", "message": "private other conversation"}}], self.root / "sessions" / "main.jsonl")
        self.state(mode="legacy", rollout=path)
        self.assertFalse(self.read()["target_found"])

    def test_legacy_mixed_representations_keep_distinct_visible_replies(self):
        path = self.jsonl([
            {"type": "session_meta", "payload": {"id": "main", "source": "cli"}},
            {"type": "response_item", "timestamp": STAMP, "payload": {"type": "message", "role": "assistant", "id": "response", "content": [{"type": "output_text", "text": "answer"}]}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "agent_message", "message": "answer"}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "agent_message", "message": "different visible progress"}},
            {"type": "event_msg", "timestamp": STAMP, "payload": {"type": "user_message", "id": "target", "message": "complaint"}},
        ], self.root / "sessions" / "main.jsonl")
        self.state(mode="legacy", rollout=path)
        result = self.read()
        self.assertEqual([r["text"] for r in result["messages"]], ["answer", "different visible progress", "complaint"])

    def test_claude_follows_target_ancestor_branch_and_excludes_tools_thinking(self):
        self.jsonl([
            self.claude("task", "task", None),
            self.claude("wrong-branch", "unrelated branch", "task", role="assistant"),
            self.claude("answer", [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "bad answer"}, {"type": "tool_use", "input": {"x": "exclude"}}], "task", role="assistant"),
            self.claude("tool-result", [{"type": "tool_result", "content": "exclude"}], "answer"),
            self.claude("target", [{"type": "image", "source": {"data": "do-not-decode"}}, {"type": "text", "text": "complaint"}], "tool-result"),
            self.claude("after", "exclude", "target", role="assistant"),
        ])
        result = self.read("claude-code")
        self.assertEqual([r["message_id"] for r in result["messages"]], ["task", "answer", "target"])
        self.assertTrue(result["coverage"]["complete"])

    def test_claude_sidechain_and_role_spoofing_never_become_target(self):
        self.jsonl([self.claude("sidechain", "exclude", None, isSidechain=True), self.claude("spoof", "exclude", None, message={"role": "assistant", "content": "pretends user"})])
        for target in ("sidechain", "spoof"):
            with self.subTest(target=target):
                self.assertFalse(self.read("claude-code", target=target)["target_found"])

    def test_claude_missing_parent_does_not_guess_other_branch(self):
        self.jsonl([self.claude("unrelated", "do not use", None), self.claude("target", "complaint", "missing")])
        result = self.read("claude-code")
        self.assertEqual([r["message_id"] for r in result["messages"]], ["target"])
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(result["coverage"]["warnings"])

    def test_claude_legacy_parentless_records_fall_back_honestly(self):
        rows = [self.claude("task", "task", None), self.claude("target", "complaint", None)]
        for row in rows:
            row.pop("parentUuid")
        self.jsonl(rows)
        result = self.read("claude-code")
        self.assertEqual([r["message_id"] for r in result["messages"]], ["task", "target"])
        self.assertFalse(result["coverage"]["complete"])

    def test_claude_malformed_tail_after_target_is_not_part_of_context(self):
        path = self.jsonl([self.claude("target", "complaint", None)])
        with path.open("a") as stream:
            stream.write("not-json\n")
        result = self.read("claude-code")
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["messages"][0]["text"], "complaint")

    def test_claude_missing_target_never_uses_neighbouring_file(self):
        self.jsonl([self.claude("target", "other conversation", None, sessionId="other")], self.root / "projects" / "project" / "other.jsonl")
        result = self.read("claude-code")
        self.assertFalse(result["target_found"])
        self.assertEqual(result["messages"], [])

    def test_missing_sources_and_bad_arguments(self):
        for platform in ("codex", "claude-code"):
            result = self.read(platform)
            self.assertFalse(result["target_found"])
            self.assertTrue(result["coverage"]["warnings"])
        for kw in ({"max_messages": 0}, {"max_chars": -1}, {"max_chars": True}):
            with self.assertRaises(ValueError):
                self.read(**kw)


if __name__ == "__main__":
    unittest.main()
