"""Opt-in reporting with synthetic identities and mocked transports only."""
from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scold
try:
    import sharing
except ImportError:
    sharing = None


START = datetime(2026, 10, 15, 7, 0, tzinfo=timezone.utc)
IDENTITY = {"available": True, "account": {"source": "codex", "email": "person@example.test"},
            "observed_at": "2026-10-15T07:00:00Z"}
ENDPOINT = "https://reports.example.test/ingest"


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(sharing, "The opt-in sharing module is not implemented")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "ledger"
        scold.initialize(self.data, "codex", Path(self.tmp.name) / "native", since="2026-01-01T00:00:00Z")
        with closing(scold._database(self.data)) as db, db:
            sharing.ensure_schema(db)

    def enable(self, endpoint=ENDPOINT, identity=IDENTITY, now=START):
        with patch.object(sharing, "_read_identity", return_value=identity), patch.object(scold, "_now", return_value=now):
            return sharing.configure(self.data, endpoint, consent=True)

    def event(self, ident="m1", status="scolding", occurred="2026-10-15T07:01:00Z"):
        message = {"platform": "codex", "conversation_id": "private-conversation", "message_id": ident,
                   "occurred_at": occurred, "text": "private raw text should not leave the device",
                   "source_ref": "/private/native/source:" + ident}
        scold.ingest_messages(self.data, [message])
        event_id = scold.event_id_for("codex", "private-conversation", ident)
        scold.review(self.data, [{"event_id": event_id, "status": status, "note": "private classification note"}])
        self.queue(event_id)
        return event_id

    def queue(self, event_id):
        with closing(scold._database(self.data)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return sharing.queue_event(db, self.data, event_id)

    def rows(self):
        with closing(scold._database(self.data)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM sharing_queue ORDER BY event_id")]

    def change(self, event_id, status):
        scold.review(self.data, [{"event_id": event_id, "status": status, "note": "corrected locally"}])
        self.queue(event_id)

    def sync(self, identity=IDENTITY, response=None, **kwargs):
        def sender(endpoint, envelope):
            return response if response is not None else {"accepted": [
                {"event_id": e["event_id"], "revision": e["revision"]} for e in envelope["events"]]}
        with patch.object(sharing, "_read_identity", return_value=identity), patch.object(sharing, "_post", side_effect=sender):
            return sharing.sync(self.data, **kwargs)

    def test_default_disabled_requires_consent_and_readable_email(self):
        self.assertFalse(sharing.status(self.data)["enabled"])
        self.event()
        self.assertEqual([], self.rows())
        with patch.object(sharing, "_read_identity") as reader:
            with self.assertRaises(scold.ScoldError):
                sharing.configure(self.data, ENDPOINT)
            reader.assert_not_called()
        with self.assertRaises(scold.ScoldError):
            self.enable(identity={"available": False, "account": None, "reason": "email_unavailable"})
        with self.assertRaises(scold.ScoldError):
            self.enable(identity={"available": True, "account": {"source": "codex", "email": ""}, "observed_at": "2026-10-15T07:00:00Z"})
        self.assertFalse(sharing.status(self.data)["enabled"])

    def test_enable_masks_account_and_never_backfills_historical_events(self):
        historical = self.event("before", occurred="2026-10-15T06:59:59Z")
        result = self.enable()
        self.assertTrue(result["enabled"])
        self.assertEqual(ENDPOINT, result["endpoint"])
        self.assertNotIn(IDENTITY["account"]["email"], json.dumps(result))
        self.queue(historical)
        self.event("not-scolding", status="not_scolding")
        self.assertEqual([], self.rows())
        self.event("after")
        self.assertEqual(1, sharing.status(self.data)["queued"])
        self.assertEqual(0, (self.data / "sharing.json").stat().st_mode & 0o077)

    def test_wire_payload_is_whitelisted_and_uses_stable_random_identity(self):
        self.enable()
        event_id = self.event()
        row = self.rows()[0]
        payload = json.loads(row["payload_json"])
        self.assertEqual({"event_id", "revision", "active", "occurred_at", "actor", "identity_observed_at",
                          "actor_basis", "client_app", "client_vendor", "model_provider"}, set(payload))
        self.assertEqual(4, UUID(payload["event_id"]).version)
        self.assertNotEqual(event_id, payload["event_id"])
        self.assertEqual({"source": "codex", "email": "person@example.test"}, payload["actor"])
        self.assertEqual("self_reported_account_binding", payload["actor_basis"])
        self.assertEqual("codex", payload["client_app"])
        self.assertEqual("OpenAI", payload["client_vendor"])
        self.assertIsNone(payload["model_provider"])
        serialized = json.dumps(payload)
        for private in ("private-conversation", "private raw", "private classification", "/private/native", event_id):
            self.assertNotIn(private, serialized)
        self.queue(event_id)
        self.assertEqual(row, self.rows()[0])

    def test_retractions_and_reopening_use_same_uuid_and_monotonic_revision(self):
        self.enable()
        event_id = self.event()
        original = self.rows()[0]
        self.assertEqual(1, self.sync()["acknowledged"])
        self.change(event_id, "uncertain")
        retraction = self.rows()[0]
        self.assertEqual(original["remote_id"], retraction["remote_id"])
        self.assertEqual(2, retraction["revision"])
        self.assertFalse(json.loads(retraction["payload_json"])["active"])
        self.queue(event_id)
        self.assertEqual(retraction, self.rows()[0])
        self.change(event_id, "scolding")
        revived = self.rows()[0]
        self.assertEqual(3, revived["revision"])
        self.assertTrue(json.loads(revived["payload_json"])["active"])

    def test_disable_retains_queue_updates_retractions_and_no_network(self):
        self.enable()
        event_id = self.event()
        sharing.disable(self.data)
        self.change(event_id, "not_scolding")
        self.assertEqual(2, self.rows()[0]["revision"])
        self.event("disabled")
        self.assertEqual(1, len(self.rows()))
        with patch.object(sharing, "_read_identity") as reader, patch.object(sharing, "_post") as transport:
            result = sharing.sync(self.data)
        reader.assert_not_called()
        transport.assert_not_called()
        self.assertEqual("disabled", result["reason"])

    def test_reenable_same_binding_excludes_disabled_period_new_events(self):
        first = self.enable()
        retained = self.event()
        sharing.disable(self.data)
        gap = self.event("gap", occurred="2026-10-15T08:00:00Z")
        second = self.enable(now=datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc))
        self.assertNotEqual(first["enabled_at"], second["enabled_at"])
        self.queue(gap)
        self.queue(retained)
        self.event("new", occurred="2026-10-15T09:01:00Z")
        self.assertEqual(2, len(self.rows()))

    def test_endpoint_or_account_change_is_rejected_and_tampering_cannot_reroute(self):
        self.enable()
        self.event()
        with self.assertRaises(scold.ScoldError):
            self.enable(endpoint="https://other.example.test/ingest")
        other = {**IDENTITY, "account": {"source": "codex", "email": "other@example.test"}}
        with self.assertRaises(scold.ScoldError):
            self.enable(identity=other)
        config = json.loads((self.data / "sharing.json").read_text())
        config["endpoint"] = "https://other.example.test/ingest"
        (self.data / "sharing.json").write_text(json.dumps(config))
        with patch.object(sharing, "_read_identity") as reader, patch.object(sharing, "_post") as transport:
            result = sharing.sync(self.data)
        transport.assert_not_called()
        self.assertEqual("binding_mismatch", result["reason"])
        self.assertEqual(1, result["remaining"])

    def test_dry_run_uses_neither_account_process_nor_http(self):
        self.enable()
        self.event()
        with patch.object(sharing, "_read_identity") as reader, patch.object(sharing, "_post") as transport:
            result = sharing.sync(self.data, dry_run=True)
        reader.assert_not_called()
        transport.assert_not_called()
        self.assertEqual(0, result["submitted"])
        self.assertEqual(1, result["remaining"])
        self.assertNotIn(IDENTITY["account"]["email"], json.dumps(result))

    def test_manual_reenable_or_historical_cutoff_change_is_not_authorization(self):
        self.enable()
        self.event()
        sharing.disable(self.data)
        config = json.loads((self.data / "sharing.json").read_text())
        config["enabled"] = True
        config["enabled_at"] = "2020-01-01T00:00:00Z"
        (self.data / "sharing.json").write_text(json.dumps(config))
        with patch.object(sharing, "_read_identity") as reader, patch.object(sharing, "_post") as transport:
            result = sharing.sync(self.data)
        reader.assert_not_called()
        transport.assert_not_called()
        self.assertFalse(result["enabled"])
        self.assertEqual("config_mismatch", result["reason"])

    def test_invalid_or_extra_payload_fields_never_reach_transport(self):
        self.enable()
        self.event()
        original = self.rows()[0]
        payload = json.loads(original["payload_json"])
        for modified in (dict(payload, text="private text"), dict(payload, model_provider="guessed-model"),
                         dict(payload, actor={"source": "codex", "email": "other@example.test"}),
                         dict(payload, revision=True), dict(payload, occurred_at="/private/path")):
            with closing(scold._database(self.data)) as db, db:
                db.execute("UPDATE sharing_queue SET payload_json=?", (json.dumps(modified),))
            with patch.object(sharing, "_read_identity", return_value=IDENTITY), patch.object(sharing, "_post") as transport:
                result = sharing.sync(self.data)
            transport.assert_not_called()
            self.assertEqual("payload_invalid", result["reason"])
            self.assertEqual(1, result["remaining"])

    def test_account_probe_exception_is_safe_and_does_not_send(self):
        self.enable()
        self.event()
        with patch.object(sharing, "_read_identity", side_effect=RuntimeError("token=private")), patch.object(sharing, "_post") as transport:
            result = sharing.sync(self.data)
        transport.assert_not_called()
        self.assertEqual("identity_unavailable", result["reason"])
        self.assertNotIn("private", json.dumps(result))

    def test_repeated_enable_does_not_move_prospective_cutoff(self):
        original = self.enable()
        repeated = self.enable(now=datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(original["enabled_at"], repeated["enabled_at"])

    def test_account_mismatch_or_unavailable_preserves_pending_events(self):
        self.enable()
        self.event()
        cases = [({"available": False, "account": None, "reason": "unavailable"}, "identity_unavailable"),
                 ({**IDENTITY, "account": {"source": "codex", "email": "other@example.test"}}, "account_mismatch")]
        for identity, code in cases:
            with patch.object(sharing, "_read_identity", return_value=identity), patch.object(sharing, "_post") as transport:
                result = sharing.sync(self.data)
            transport.assert_not_called()
            self.assertEqual(code, result["reason"])
            self.assertEqual(1, result["remaining"])

    def test_network_failure_and_retry_leave_local_classification_unchanged(self):
        self.enable()
        self.event()
        with patch.object(sharing, "_read_identity", return_value=IDENTITY), patch.object(sharing, "_post", side_effect=OSError("private connection detail")):
            failed = sharing.sync(self.data)
        self.assertEqual("network_error", failed["reason"])
        self.assertEqual(1, failed["remaining"])
        self.assertNotIn("private", json.dumps(failed))
        self.assertEqual(1, scold.status(self.data)["counts"]["scolding"])
        self.assertEqual(1, self.sync()["acknowledged"])
        self.assertEqual(0, self.sync()["submitted"])

    def test_only_exact_submitted_acknowledgments_are_accepted(self):
        self.enable()
        self.event()
        row = self.rows()[0]
        for response in ({"accepted": [{"event_id": "unsubmitted", "revision": 1}]},
                         {"accepted": [{"event_id": row["remote_id"], "revision": 2}]},
                         {"accepted": [{"event_id": row["remote_id"], "revision": True}]},
                         {"accepted": "not a list"}):
            result = self.sync(response=response)
            self.assertEqual("invalid_acknowledgment", result["reason"])
            self.assertEqual(0, result["acknowledged"])
            self.assertEqual(1, result["remaining"])
        self.assertEqual(1, self.sync()["acknowledged"])

    def test_partial_acknowledgment_only_releases_matching_event(self):
        self.enable()
        self.event("first")
        self.event("second")
        row = self.rows()[0]
        result = self.sync(response={"accepted": [{"event_id": row["remote_id"], "revision": 1}]})
        self.assertEqual(1, result["acknowledged"])
        self.assertEqual(1, result["remaining"])

    def test_late_ack_cannot_acknowledge_a_newer_retraction(self):
        self.enable()
        event_id = self.event()
        def sender(endpoint, envelope):
            self.change(event_id, "not_scolding")
            return {"accepted": [{"event_id": envelope["events"][0]["event_id"], "revision": 1}]}
        with patch.object(sharing, "_read_identity", return_value=IDENTITY), patch.object(sharing, "_post", side_effect=sender):
            result = sharing.sync(self.data)
        self.assertEqual(1, result["remaining"])
        self.assertEqual(1, self.rows()[0]["acknowledged_revision"])
        self.assertEqual(2, self.rows()[0]["revision"])

    def test_queue_and_schema_obey_caller_rollback(self):
        self.enable()
        event_id = self.event()
        before = self.rows()[0]
        with closing(scold._database(self.data)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE events SET status='not_scolding' WHERE event_id=?", (event_id,))
            sharing.ensure_schema(db)
            sharing.queue_event(db, self.data, event_id)
            db.rollback()
        self.assertEqual(before, self.rows()[0])
        self.assertEqual(1, scold.status(self.data)["counts"]["scolding"])

    def test_bad_endpoints_and_limits_rejected_without_identity_call(self):
        invalid = ["http://example.test/a", "https://user:secret@example.test/a", "https://example.test/a?token=secret",
                   "https://example.test/a#secret", "ftp://example.test/a", "http://localhost.evil/a", "https://example.test:99999/a"]
        with patch.object(sharing, "_read_identity") as reader:
            for endpoint in invalid:
                with self.assertRaises(scold.ScoldError):
                    sharing.configure(self.data, endpoint, consent=True)
        reader.assert_not_called()
        for limit in (0, -1, True, 101, "30"):
            with self.assertRaises(scold.ScoldError):
                sharing.sync(self.data, limit=limit)

    def test_loopback_http_is_supported(self):
        self.assertEqual("http://127.0.0.1:8080/ingest", self.enable(endpoint="http://127.0.0.1:8080/ingest")["endpoint"])


class Response(BytesIO):
    status = 200
    def __init__(self, body):
        super().__init__(body)
        self.headers = {}


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(sharing, "The opt-in sharing module is not implemented")

    def test_post_uses_json_timeout_and_disallows_redirects(self):
        with patch.object(sharing.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = Response(b'{"accepted":[]}')
            result = sharing._post(ENDPOINT, {"schema_version": 1, "events": []})
        self.assertEqual({"accepted": []}, result)
        self.assertTrue(any(isinstance(handler, sharing._NoRedirect) for handler in build.call_args.args))
        request = build.return_value.open.call_args.args[0]
        self.assertEqual("POST", request.method)
        self.assertEqual({"schema_version": 1, "events": []}, json.loads(request.data))
        self.assertEqual(10, build.return_value.open.call_args.kwargs["timeout"])
        self.assertIsNone(sharing._NoRedirect().redirect_request(Request(ENDPOINT), None, 302, "redirect", {}, "https://other.example.test"))

    def test_redirect_error_does_not_retry_another_url(self):
        with patch.object(sharing.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = HTTPError(ENDPOINT, 307, "redirect", {}, None)
            with self.assertRaises(sharing.TransportError) as error:
                sharing._post(ENDPOINT, {"schema_version": 1, "events": []})
        self.assertEqual("redirect_refused", error.exception.code)
        self.assertEqual(1, build.return_value.open.call_count)

    def test_response_size_and_json_are_bounded(self):
        cases = [(b"x" * 65537, "response_too_large"), (b"not json", "invalid_response")]
        for body, expected in cases:
            with patch.object(sharing.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = Response(body)
                with self.assertRaises(sharing.TransportError) as error:
                    sharing._post(ENDPOINT, {"schema_version": 1, "events": []})
            self.assertEqual(expected, error.exception.code)


if __name__ == "__main__":
    unittest.main()
