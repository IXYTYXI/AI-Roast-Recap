"""Read non-secret identity from an already signed-in host's official CLI.

No credential files are inspected. These adapters never log in, request token
refresh, send model prompts or print native responses. Only the host-reported email is
returned; it is an authorized current-account binding, not historical authorship
proof or remote authentication.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time


_TIMEOUT_SECONDS = 15
_STOP_TIMEOUT_SECONDS = 2
_MAX_RESPONSE_BYTES = 65536


class _IdentityError(Exception):
    def __init__(self, reason):
        self.reason = reason


def _observed_at():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _unavailable(reason):
    return {"available": False, "account": None, "observed_at": _observed_at(), "reason": reason}


def _email(value):
    if not isinstance(value, str) or not value or len(value) > 320 or value != value.strip():
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value if re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", value) else None


class _Reader:
    """Bound both wall time and output while reading a local subprocess pipe."""
    def __init__(self, process, deadline):
        self.process = process
        self.deadline = deadline
        self.selector = selectors.DefaultSelector()
        try:
            self.selector.register(process.stdout, selectors.EVENT_READ)
        except BaseException:
            self.selector.close()
            raise
        self.buffer = bytearray()
        self.received = 0

    def close(self):
        self.selector.close()

    def receive(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or not self.selector.select(remaining):
            raise _IdentityError("timeout")
        raw = os.read(self.process.stdout.fileno(), min(16384, _MAX_RESPONSE_BYTES + 1))
        self.received += len(raw)
        if self.received > _MAX_RESPONSE_BYTES:
            raise _IdentityError("response_too_large")
        return raw

    def rpc_result(self, ident):
        while True:
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (UnicodeError, ValueError):
                    raise _IdentityError("invalid_response") from None
                if not isinstance(message, dict):
                    raise _IdentityError("invalid_response")
                if message.get("id") != ident:
                    # Unsolicited notifications and credential requests are
                    # never acted on. Only the two explicit replies are used.
                    continue
                if "error" in message:
                    raise _IdentityError("host_error")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise _IdentityError("invalid_response")
                return result
            raw = self.receive()
            if not raw:
                raise _IdentityError("host_unavailable")
            self.buffer.extend(raw)

    def json_result(self):
        chunks = bytearray()
        while True:
            raw = self.receive()
            if not raw:
                break
            chunks.extend(raw)
        try:
            result = json.loads(chunks)
        except (UnicodeError, ValueError):
            raise _IdentityError("invalid_response") from None
        if not isinstance(result, dict):
            raise _IdentityError("invalid_response")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _IdentityError("timeout")
        if self.process.wait(timeout=remaining) != 0:
            raise _IdentityError("host_error")
        return result


def _send(process, value):
    process.stdin.write(json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n")
    process.stdin.flush()


def _codex_account(process, reader):
    _send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "ai_roast_recap_identity", "version": "1"}}})
    reader.rpc_result(1)
    _send(process, {"jsonrpc": "2.0", "method": "initialized"})
    _send(process, {"jsonrpc": "2.0", "id": 2, "method": "account/read", "params": {"refreshToken": False}})
    account = reader.rpc_result(2).get("account")
    if account is None:
        raise _IdentityError("not_signed_in")
    if not isinstance(account, dict):
        raise _IdentityError("invalid_response")
    if account.get("type") != "chatgpt":
        raise _IdentityError("unsupported_auth_mode")
    email = _email(account.get("email"))
    if email is None:
        raise _IdentityError("email_unavailable")
    return {"source": "codex", "email": email}


def _claude_account(reader):
    status = reader.json_result()
    if status.get("loggedIn") is not True:
        raise _IdentityError("not_signed_in")
    email = _email(status.get("email"))
    if email is None:
        # oauth_token authentication can be active without exposing an email.
        # Do not infer a person from authMethod, organization or token contents.
        raise _IdentityError("email_unavailable")
    return {"source": "claude-code", "email": email}


def _stop(process):
    stopped = False
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        stopped = process.poll() is not None
    except (OSError, subprocess.TimeoutExpired):
        # A failed terminate must still get one kill attempt.
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
            stopped = process.poll() is not None
        except (OSError, subprocess.TimeoutExpired):
            stopped = False
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    return stopped


def read_identity(platform, source_root):
    """Read the current host account from the specified host configuration root.

    Native failures return safe reason codes with no stderr/exception text.
    Returned account fields are a strict source/email whitelist. This function
    performs no external submission and does not cache or print the identity.
    """
    if platform not in {"codex", "claude-code"}:
        return _unavailable("unsupported_platform")
    try:
        root = Path(source_root).expanduser().resolve()
        if not root.is_dir():
            return _unavailable("source_root_unavailable")
    except (OSError, TypeError, ValueError):
        return _unavailable("source_root_unavailable")
    env = os.environ.copy()
    if platform == "codex":
        env["CODEX_HOME"] = str(root)
        command = ["codex", "app-server", "--stdio", "-c", "analytics.enabled=false"]
    else:
        env["CLAUDE_CONFIG_DIR"] = str(root)
        command = ["claude", "auth", "status", "--json"]
    process, reader = None, None
    stopped = True
    try:
        deadline = time.monotonic() + _TIMEOUT_SECONDS
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env)
        reader = _Reader(process, deadline)
        account = _codex_account(process, reader) if platform == "codex" else _claude_account(reader)
        result = {"available": True, "account": account, "observed_at": _observed_at()}
    except _IdentityError as error:
        result = _unavailable(error.reason)
    except FileNotFoundError:
        result = _unavailable("cli_not_found")
    except PermissionError:
        result = _unavailable("permission_denied")
    except subprocess.TimeoutExpired:
        result = _unavailable("timeout")
    except (OSError, ValueError, TypeError):
        result = _unavailable("host_unavailable")
    finally:
        try:
            if reader is not None:
                reader.close()
        except Exception:
            stopped = False
        finally:
            if process is not None:
                stopped = _stop(process) and stopped
    return result if stopped else _unavailable("process_cleanup_failed")
