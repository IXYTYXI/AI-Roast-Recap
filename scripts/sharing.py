"""Optional, explicitly authorized event metadata sharing.

No identity discovery or network is performed during local classification. The
private outbox contains versioned, whitelisted payloads with independent remote
identifiers; historical text, native identifiers and credentials never enter it.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID, uuid4

import scold


TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 65536
MAX_BATCH = 100
CLIENT_VENDORS = {"codex": "OpenAI", "claude-code": "Anthropic"}
PAYLOAD_FIELDS = frozenset(("event_id", "revision", "active", "occurred_at", "actor", "identity_observed_at",
                            "actor_basis", "client_app", "client_vendor", "model_provider"))


class TransportError(Exception):
    """A safe code, never a response body, credential or connection detail."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_schema(db):
    """Create additive tables without committing the caller's transaction."""
    db.execute("""CREATE TABLE IF NOT EXISTS sharing_queue (
        event_id TEXT PRIMARY KEY REFERENCES events(event_id),
        remote_id TEXT NOT NULL UNIQUE,
        binding_fingerprint TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>=1),
        payload_json TEXT NOT NULL,
        acknowledged_revision INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged_revision>=0),
        updated_at TEXT NOT NULL
    )""")
    db.execute("CREATE TABLE IF NOT EXISTS sharing_state (key TEXT PRIMARY KEY,value TEXT NOT NULL)")


def _state(db, key):
    row = db.execute("SELECT value FROM sharing_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_state(db, key, value):
    db.execute("INSERT INTO sharing_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))


def _endpoint(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise scold.ScoldError("Endpoint must be a valid HTTPS URL, or loopback HTTP URL")
    try:
        parsed = urllib.parse.urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError()
        if parsed.scheme not in ("https", "http") or (port is not None and not 1 <= port <= 65535):
            raise ValueError()
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                raise ValueError()
        # Normalize only the host/scheme, not the receiver's case-sensitive path.
        host = host.lower()
        authority = "[" + host + "]" if ":" in host else host
        if port is not None:
            authority += ":" + str(port)
        return urllib.parse.urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", "", ""))
    except (ValueError, TypeError, UnicodeError):
        raise scold.ScoldError("Endpoint must use HTTPS (loopback HTTP allowed), without URL credentials, query or fragment") from None


def _account(value, platform):
    if not isinstance(value, dict) or value.get("source") != platform:
        return None
    email = value.get("email")
    if not isinstance(email, str) or len(email) > 320 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return None
    return {"source": platform, "email": email.casefold()}


def _fingerprint(endpoint, account):
    return hashlib.sha256(_json({"endpoint": endpoint, "account": account}).encode("utf-8")).hexdigest()


def _load(data_dir):
    path = scold._path(data_dir) / "sharing.json"
    if not path.exists():
        return None
    value = scold._read_json(path, "sharing configuration")
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise scold.ScoldError("Sharing configuration is invalid")
    return value


def _authorization(config):
    return {key: config[key] for key in ("binding_fingerprint", "enabled", "enabled_at", "identity_observed_at")}


def _validated_config(db, config, platform):
    if config is None:
        return None, "disabled"
    try:
        endpoint = _endpoint(config.get("endpoint"))
        account = _account(config.get("account"), platform)
        if account is None or type(config.get("enabled")) is not bool:
            return None, "config_invalid"
        scold._timestamp(config.get("enabled_at"), "sharing enabled_at")
        scold._timestamp(config.get("identity_observed_at"), "identity_observed_at")
        fingerprint = _fingerprint(endpoint, account)
        if config.get("binding_fingerprint") != fingerprint or _state(db, "binding_fingerprint") != fingerprint:
            return None, "binding_mismatch"
        if _state(db, "authorization") != _json(_authorization(config)):
            return None, "config_mismatch"
        return dict(config, endpoint=endpoint, account=account), "ready" if config["enabled"] else "disabled"
    except (scold.ScoldError, KeyError, TypeError):
        return None, "config_invalid"


def _read_identity(config):
    from identity import read_identity
    return read_identity(config["platform"], config["source_root"])


def _current_identity(config):
    try:
        result = _read_identity(config)
        if not isinstance(result, dict) or result.get("available") is not True:
            return None
        account = _account(result.get("account"), config["platform"])
        if account is None:
            return None
        observed_at = scold._iso(scold._timestamp(result.get("observed_at"), "identity observed_at"))
        return {"account": account, "observed_at": observed_at}
    except Exception:
        # Native CLI errors must neither print credentials nor break statistics.
        return None


def configure(data_dir, endpoint, consent=False):
    """Enable a prospective binding only after explicit consent and identity read."""
    if consent is not True:
        raise scold.ScoldError("Sharing requires explicit consent to read the host account and send event metadata")
    endpoint = _endpoint(endpoint)
    local = scold._config(data_dir)
    identity = _current_identity(local)
    if identity is None:
        raise scold.ScoldError("Sharing was not enabled: the current host account email is unavailable; no login or credential collection was attempted")
    account = identity["account"]
    fingerprint = _fingerprint(endpoint, account)
    now = scold._iso(scold._now())
    old = _load(data_dir)
    with closing(scold._database(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        ensure_schema(db)
        existing = _state(db, "binding_fingerprint")
        if existing is not None and existing != fingerprint:
            raise scold.ScoldError("Existing endpoint/account binding cannot be changed; use a separately initialized data directory and do not migrate its pending queue")
        foreign = db.execute("SELECT 1 FROM sharing_queue WHERE binding_fingerprint<>? LIMIT 1", (fingerprint,)).fetchone()
        if foreign:
            raise scold.ScoldError("Pending events belong to a different binding and cannot be rerouted")
        valid, _ = _validated_config(db, old, local["platform"])
        if valid is None or not valid["enabled"]:
            config = {"schema_version": 1, "enabled": True, "enabled_at": now,
                      "endpoint": endpoint, "account": account,
                      "identity_observed_at": identity["observed_at"], "binding_fingerprint": fingerprint}
            _set_state(db, "binding_fingerprint", fingerprint)
            _set_state(db, "authorization", _json(_authorization(config)))
            scold._write_json(scold._path(data_dir) / "sharing.json", config)
    return status(data_dir)


def disable(data_dir):
    """Disable future sync calls, retaining deliveries; in-progress sync may finish."""
    scold._config(data_dir)
    config = _load(data_dir)
    if config is not None:
        config["enabled"] = False
        with closing(scold._database(data_dir)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            ensure_schema(db)
            if all(key in config for key in ("binding_fingerprint", "enabled_at", "identity_observed_at")):
                _set_state(db, "authorization", _json(_authorization(config)))
            scold._write_json(scold._path(data_dir) / "sharing.json", config)
    return status(data_dir)


def _pending_count(db):
    return db.execute("SELECT COUNT(*) FROM sharing_queue WHERE revision>acknowledged_revision").fetchone()[0]


def status(data_dir):
    """Return masked configuration and outbox counts without probing identity."""
    local = scold._config(data_dir)
    try:
        config, read_error = _load(data_dir), None
    except scold.ScoldError:
        config, read_error = None, "config_invalid"
    with closing(scold._database(data_dir)) as db, db:
        ensure_schema(db)
        valid, reason = _validated_config(db, config, local["platform"])
        queued = _pending_count(db)
        total = db.execute("SELECT COUNT(*) FROM sharing_queue").fetchone()[0]
    result = {"enabled": bool(valid and valid["enabled"]), "queued": queued,
              "total_events": total, "reason": read_error or reason,
              "endpoint": None, "masked_email": None, "account_source": None, "enabled_at": None}
    if valid:
        email_local, domain = valid["account"]["email"].split("@", 1)
        result.update(endpoint=valid["endpoint"], masked_email=email_local[:1] + "***@" + domain,
                      account_source=valid["account"]["source"], enabled_at=valid["enabled_at"])
    return result


def queue_event(db, data_dir, event_id):
    """Atomically mirror a classification change in the existing review transaction."""
    ensure_schema(db)
    row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        raise scold.ScoldError("Cannot queue an unknown event")
    active = row["status"] == "scolding"
    queued = db.execute("SELECT * FROM sharing_queue WHERE event_id=?", (event_id,)).fetchone()
    if queued:
        payload = json.loads(queued["payload_json"])
        if payload["active"] == active:
            return {"queued": False, "changed": False}
        revision = queued["revision"] + 1
        payload.update(active=active, revision=revision)
        db.execute("UPDATE sharing_queue SET revision=?,payload_json=?,updated_at=? WHERE event_id=?",
                   (revision, _json(payload), scold._iso(scold._now()), event_id))
        return {"queued": True, "changed": True}
    if not active:
        return {"queued": False, "changed": False}
    try:
        config, _ = _validated_config(db, _load(data_dir), row["platform"])
    except scold.ScoldError:
        config = None
    if not config or not config["enabled"] or scold._timestamp(row["occurred_at"]) < scold._timestamp(config["enabled_at"]):
        return {"queued": False, "changed": False}
    remote_id = str(uuid4())
    payload = {"event_id": remote_id, "revision": 1, "active": True,
               "occurred_at": row["occurred_at"], "actor": config["account"],
               "identity_observed_at": config["identity_observed_at"],
               "actor_basis": "self_reported_account_binding", "client_app": row["platform"],
               "client_vendor": CLIENT_VENDORS[row["platform"]], "model_provider": None}
    db.execute("""INSERT INTO sharing_queue(event_id,remote_id,binding_fingerprint,revision,payload_json,updated_at)
                  VALUES(?,?,?,?,?,?)""",
               (event_id, remote_id, config["binding_fingerprint"], 1, _json(payload), scold._iso(scold._now())))
    return {"queued": True, "changed": True}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _post(endpoint, envelope):
    request = urllib.request.Request(endpoint, data=_json(envelope).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if not 200 <= response.status < 300:
                raise TransportError("receiver_error")
            content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise TransportError("response_too_large")
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise TransportError("invalid_response") from None
    except urllib.error.HTTPError as error:
        error.close()
        raise TransportError("redirect_refused" if 300 <= error.code < 400 else "receiver_error") from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise TransportError("network_error") from None


def _accepted(response, submitted):
    if not isinstance(response, dict) or not isinstance(response.get("accepted"), list):
        raise TransportError("invalid_acknowledgment")
    expected = {(event["event_id"], event["revision"]) for event in submitted}
    accepted = set()
    for item in response["accepted"]:
        if (not isinstance(item, dict) or not isinstance(item.get("event_id"), str)
                or type(item.get("revision")) is not int):
            raise TransportError("invalid_acknowledgment")
        key = (item["event_id"], item["revision"])
        if key not in expected:
            raise TransportError("invalid_acknowledgment")
        accepted.add(key)
    return accepted


def _validated_payloads(rows, config):
    """Enforce the wire whitelist again at the actual network boundary."""
    payloads = []
    for row in rows:
        try:
            value = json.loads(row["payload_json"])
            if (not isinstance(value, dict) or set(value) != PAYLOAD_FIELDS
                    or value["event_id"] != row["remote_id"] or UUID(value["event_id"]).version != 4
                    or type(value["revision"]) is not int or value["revision"] != row["revision"]
                    or type(value["active"]) is not bool or value["actor"] != config["account"]
                    or value["actor_basis"] != "self_reported_account_binding"
                    or value["client_app"] != config["account"]["source"]
                    or value["client_vendor"] != CLIENT_VENDORS[config["account"]["source"]]
                    or value["model_provider"] is not None):
                raise ValueError()
            scold._timestamp(value["occurred_at"], "occurred_at")
            scold._timestamp(value["identity_observed_at"], "identity_observed_at")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise TransportError("payload_invalid") from None
        payloads.append(value)
    return payloads


def sync(data_dir, limit=30, dry_run=False):
    """Send one bounded outbox batch after rechecking the authorized account."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_BATCH:
        raise scold.ScoldError(f"limit must be an integer between 1 and {MAX_BATCH}")
    if type(dry_run) is not bool:
        raise scold.ScoldError("dry_run must be a boolean")
    local = scold._config(data_dir)
    initial = status(data_dir)
    result = {"enabled": initial["enabled"], "dry_run": dry_run, "queued": initial["queued"],
              "submitted": 0, "acknowledged": 0, "remaining": initial["queued"], "reason": initial["reason"]}
    if not initial["enabled"]:
        return result
    if dry_run:
        return dict(result, reason="dry_run")
    if not initial["queued"]:
        return dict(result, reason="nothing_pending")
    identity = _current_identity(local)
    if identity is None:
        return dict(result, reason="identity_unavailable")
    try:
        config = _load(data_dir)
    except scold.ScoldError:
        return dict(result, reason="config_invalid")
    with closing(scold._database(data_dir)) as db:
        valid, reason = _validated_config(db, config, local["platform"])
        if not valid or not valid["enabled"]:
            return dict(result, enabled=False, reason=reason)
        if identity["account"] != valid["account"]:
            return dict(result, reason="account_mismatch")
        rows = db.execute("SELECT * FROM sharing_queue WHERE revision>acknowledged_revision ORDER BY updated_at,event_id LIMIT ?", (limit,)).fetchall()
        if any(row["binding_fingerprint"] != valid["binding_fingerprint"] for row in rows):
            return dict(result, reason="binding_mismatch")
        try:
            payloads = _validated_payloads(rows, valid)
        except TransportError as error:
            return dict(result, reason=error.code)
    if not payloads:
        return dict(result, remaining=0, reason="nothing_pending")
    result["submitted"] = len(payloads)
    try:
        response = _post(valid["endpoint"], {"schema_version": 1, "events": payloads})
        accepted = _accepted(response, payloads)
    except TransportError as error:
        return dict(result, reason=error.code)
    except (OSError, TimeoutError, ValueError, RuntimeError):
        return dict(result, reason="network_error")
    with closing(scold._database(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for remote_id, revision in accepted:
            db.execute("""UPDATE sharing_queue SET acknowledged_revision=MAX(acknowledged_revision,?)
                          WHERE remote_id=? AND revision>=?""", (revision, remote_id, revision))
        result["remaining"] = _pending_count(db)
    result.update(acknowledged=len(accepted), reason="synced")
    return result
