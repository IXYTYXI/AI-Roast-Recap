"""Official-account adapters tested with synthetic subprocesses only."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "identity.py"
if SOURCE.exists():
    SPEC = importlib.util.spec_from_file_location("review_identity", SOURCE)
    IDENTITY = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(IDENTITY)
else:
    IDENTITY = None


class FakeInput:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def write(self, raw):
        obj = json.loads(raw)
        self.process.requests.append(obj)
        if self.process.timeout:
            return len(raw)
        if obj.get("method") == "initialize":
            response = {"id": obj["id"], "result": {"userAgent": "synthetic"}}
            if self.process.init_error:
                response = {"id": obj["id"], "error": {"code": -1, "message": "SECRET-init-error"}}
            self.process.emit(response)
        elif obj.get("method") == "account/read":
            response = {"id": obj["id"], "result": {"account": self.process.account, "requiresOpenaiAuth": True}}
            if self.process.account_error:
                response = {"id": obj["id"], "error": {"code": -1, "message": "SECRET-account-error"}}
            self.process.emit(response)
        return len(raw)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, account=None, claude=None, timeout=False, init_error=False, account_error=False, raw=None, slow_stop=False):
        read_fd, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.stdin = FakeInput(self)
        self.account = account
        self.timeout = timeout
        self.init_error = init_error
        self.account_error = account_error
        self.slow_stop = slow_stop
        self.returncode = None
        self.requests = []
        self.terminated = False
        self.killed = False
        if claude is not None or raw is not None:
            os.write(self.write_fd, raw if raw is not None else json.dumps(claude).encode())
            self.close_writer()
            self.returncode = 0

    def emit(self, response):
        os.write(self.write_fd, json.dumps(response).encode() + b"\n")

    def close_writer(self):
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.slow_stop:
            self.returncode = -15
            self.close_writer()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.close_writer()

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        return self.returncode

    def close(self):
        self.close_writer()
        self.stdin.close()
        self.stdout.close()


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assertIsNotNone(IDENTITY, "identity adapter is not implemented")

    def run_fake(self, fake, platform="codex"):
        self.addCleanup(fake.close)
        with patch.object(IDENTITY.subprocess, "Popen", return_value=fake) as popen, patch.object(IDENTITY, "_TIMEOUT_SECONDS", 0.03):
            result = IDENTITY.read_identity(platform, self.root)
        return result, popen

    def assert_safe_failure(self, result, reason):
        self.assertFalse(result["available"])
        self.assertIsNone(result["account"])
        self.assertEqual(result["reason"], reason)
        self.assertTrue(result["observed_at"].endswith("Z"))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_codex_whitelists_email_and_uses_exact_read_only_protocol(self):
        fake = FakeProcess(account={"type": "chatgpt", "email": "person@example.test", "planType": "SECRET-plan", "accessToken": "SECRET-access", "refreshToken": "SECRET-refresh", "id": "SECRET-id"})
        result, popen = self.run_fake(fake)
        self.assertTrue(result["available"])
        self.assertEqual(result["account"], {"source": "codex", "email": "person@example.test"})
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual([q["method"] for q in fake.requests], ["initialize", "initialized", "account/read"])
        self.assertEqual(fake.requests[-1]["params"], {"refreshToken": False})
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["codex", "app-server", "--stdio", "-c", "analytics.enabled=false"])
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.root.resolve()))
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("shell", kwargs)
        self.assertTrue(fake.terminated)
        self.assertIsNotNone(fake.poll())
        self.assertTrue(fake.stdout.closed)

    def test_codex_null_account_and_api_key_cannot_supply_identity(self):
        for account, reason in [(None, "not_signed_in"), ({"type": "apiKey", "email": "SECRET@example.test", "apiKey": "SECRET"}, "unsupported_auth_mode"), ({"type": "amazonBedrock"}, "unsupported_auth_mode"), ({"type": "chatgpt", "email": None}, "email_unavailable")]:
            with self.subTest(account_type=account.get("type") if account else None):
                result, _ = self.run_fake(FakeProcess(account=account))
                self.assert_safe_failure(result, reason)

    def test_codex_timeout_cleans_up_process_and_returns_no_raw_output(self):
        fake = FakeProcess(timeout=True)
        result, _ = self.run_fake(fake)
        self.assert_safe_failure(result, "timeout")
        self.assertTrue(fake.terminated)
        self.assertEqual([q["method"] for q in fake.requests], ["initialize"])

    def test_codex_cleanup_escalates_to_kill_when_terminate_does_not_stop(self):
        fake = FakeProcess(account={"type": "chatgpt", "email": "person@example.test"}, slow_stop=True)
        result, _ = self.run_fake(fake)
        self.assertTrue(result["available"])
        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)
        self.assertIsNotNone(fake.poll())

    def test_codex_host_errors_are_safe_codes_and_always_cleanup(self):
        for kwargs in ({"init_error": True}, {"account_error": True}):
            with self.subTest(kwargs=kwargs):
                fake = FakeProcess(**kwargs)
                result, _ = self.run_fake(fake)
                self.assert_safe_failure(result, "host_error")
                self.assertTrue(fake.terminated)

    def test_claude_reads_official_status_and_filters_extra_fields(self):
        fake = FakeProcess(claude={"loggedIn": True, "authMethod": "claude.ai", "email": "person@example.test", "organizationName": "SECRET-org", "accessToken": "SECRET-access", "accountUuid": "SECRET-id"})
        result, popen = self.run_fake(fake, "claude-code")
        self.assertTrue(result["available"])
        self.assertEqual(result["account"], {"source": "claude-code", "email": "person@example.test"})
        self.assertNotIn("SECRET", json.dumps(result))
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["claude", "auth", "status", "--json"])
        self.assertEqual(kwargs["env"]["CLAUDE_CONFIG_DIR"], str(self.root.resolve()))
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(fake.requests, [])
        self.assertTrue(fake.stdout.closed)

    def test_claude_oauth_without_email_is_explicitly_unavailable(self):
        result, _ = self.run_fake(FakeProcess(claude={"loggedIn": True, "authMethod": "oauth_token", "apiProvider": "firstParty"}), "claude-code")
        self.assert_safe_failure(result, "email_unavailable")

    def test_nonzero_claude_exit_cannot_supply_an_identity(self):
        fake = FakeProcess(claude={"loggedIn": True, "email": "SECRET@example.test"})
        fake.returncode = 1
        result, _ = self.run_fake(fake, "claude-code")
        self.assert_safe_failure(result, "host_error")

    def test_claude_logged_out_does_not_reuse_stale_email(self):
        result, _ = self.run_fake(FakeProcess(claude={"loggedIn": False, "email": "SECRET@example.test"}), "claude-code")
        self.assert_safe_failure(result, "not_signed_in")

    def test_claude_timeout_and_malformed_json_are_safe(self):
        for fake, reason in [(FakeProcess(timeout=True), "timeout"), (FakeProcess(raw=b"SECRET-invalid-json"), "invalid_response")]:
            result, _ = self.run_fake(fake, "claude-code")
            self.assert_safe_failure(result, reason)
            self.assertIsNotNone(fake.poll())

    def test_missing_cli_and_permission_errors_do_not_leak_exception_text(self):
        for error, reason in [(FileNotFoundError("SECRET-command"), "cli_not_found"), (PermissionError("SECRET-path"), "permission_denied"), (OSError("SECRET-other"), "host_unavailable")]:
            with self.subTest(reason=reason), patch.object(IDENTITY.subprocess, "Popen", side_effect=error):
                self.assert_safe_failure(IDENTITY.read_identity("codex", self.root), reason)

    def test_response_size_is_bounded_and_oversize_child_is_stopped(self):
        fake = FakeProcess(account={"type": "chatgpt", "email": "person@example.test", "token": "SECRET" * 100})
        with patch.object(IDENTITY, "_MAX_RESPONSE_BYTES", 128):
            result, _ = self.run_fake(fake)
        self.assert_safe_failure(result, "response_too_large")
        self.assertTrue(fake.terminated)

    def test_pipe_write_exception_still_stops_the_child(self):
        fake = FakeProcess()
        with patch.object(fake.stdin, "write", side_effect=OSError("SECRET-native-error")):
            result, _ = self.run_fake(fake)
        self.assert_safe_failure(result, "host_unavailable")
        self.assertTrue(fake.terminated)

    def test_selector_setup_failure_closes_selector_and_child(self):
        fake = FakeProcess()
        selector = Mock()
        selector.register.side_effect = PermissionError("SECRET-selector-error")
        with patch.object(IDENTITY.selectors, "DefaultSelector", return_value=selector):
            result, _ = self.run_fake(fake)
        self.assert_safe_failure(result, "permission_denied")
        selector.close.assert_called_once()
        self.assertTrue(fake.terminated)

    def test_reader_close_exception_cannot_skip_process_cleanup(self):
        fake = FakeProcess(account={"type": "chatgpt", "email": "person@example.test"})
        close = IDENTITY._Reader.close

        def fail_after_close(reader):
            close(reader)
            raise OSError("SECRET-close-error")

        with patch.object(IDENTITY._Reader, "close", fail_after_close):
            result, _ = self.run_fake(fake)
        self.assert_safe_failure(result, "process_cleanup_failed")
        self.assertTrue(fake.terminated)
        self.assertIsNotNone(fake.poll())

    def test_invalid_email_never_escapes_the_adapter(self):
        for email in ["", "SECRET-not-an-email", " x@example.test", "x@example.test\n", "x\x00@example.test", 42, {"token": "SECRET"}]:
            with self.subTest(email_type=type(email).__name__):
                result, _ = self.run_fake(FakeProcess(account={"type": "chatgpt", "email": email}))
                self.assert_safe_failure(result, "email_unavailable")

    def test_unsupported_platform_or_missing_root_never_starts_a_child(self):
        with patch.object(IDENTITY.subprocess, "Popen") as popen:
            self.assert_safe_failure(IDENTITY.read_identity("unknown", self.root), "unsupported_platform")
            self.assert_safe_failure(IDENTITY.read_identity("codex", self.root / "missing"), "source_root_unavailable")
        popen.assert_not_called()

    def test_adapter_never_prints_identity_or_native_responses(self):
        fake = FakeProcess(account={"type": "chatgpt", "email": "private@example.test", "accessToken": "SECRET"})
        with patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO) as err:
            result, _ = self.run_fake(fake)
        self.assertTrue(result["available"])
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
