"""Signed client for the Pack'N OS tickets routes (contract packn-tickets-v1).

Phase 93. The cron gets its work list and its ticket data from the OS over
four signed routes, and reports each pass back:

    fetch_unprocessed(limit)                      GET  /api/tickets/unprocessed
    fetch_ticket(ticket_ref)                      GET  /api/tickets/<ref>
    post_classification(os_ticket_id, payload)    POST /api/tickets/<id>/classification
    post_source_shadow_observations(payload)      POST /api/tickets/source-shadow

Signing (owned by the OS verifier; change both sides together). The signature
is the lower-case hex HMAC-SHA256, under the tickets API secret, of six lines
joined by a line feed:

    packn-tickets-v1
    <METHOD, upper case>
    <path>
    <canonical query>
    <timestamp, unix seconds>
    <lower-case hex SHA-256 of the body bytes; of zero bytes for a GET>

The canonical query percent-encodes every name and value with nothing left
unencoded but letters, digits, hyphen, dot, underscore and tilde, and sorts the
encoded pairs. The request is sent with that same canonical query, and the one
buffer that was hashed is the buffer that is sent.

Rules this module keeps (they differ from scripts/post_action_items.py):
    - Fail closed. A failure raises TicketsApiError with a code; nothing here
      falls back to another source, and nothing returns None for a failure.
    - Five seconds. TIMEOUT_S is the budget handed to the transport.
    - No body in any output. A request body and a response body carry
      end-consumer text. Log lines name the route, the status and a count.
      An error carries a code and a status, never response text, and it is
      raised outside the handler that caught the transport error so nothing of
      the response rides its context either.
    - Standard library only (urllib, hmac, hashlib, json). No third-party HTTP
      or signing package, and no import of the database helper, so the signer
      can be used and tested without a database driver.

The secret is a different file and a different value from the action-ingest
secret: config/.secrets/tickets_api_secret.txt (gitignored), the same value the
owner sets as TICKETS_API_SECRET in the OS.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
SECRET_PATH = ROOT / "config" / ".secrets" / "tickets_api_secret.txt"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"

SIGNATURE_VERSION = "packn-tickets-v1"
SIGNATURE_HEADER = "x-packn-signature"
TIMESTAMP_HEADER = "x-packn-timestamp"
CONTRACT_VERSION = "1"
MIN_SECRET_LENGTH = 32
TIMEOUT_S = 5.0
MAX_LIST_LIMIT = 100
# The body error the OS answers with a 503 when its own secret is absent or short.
SECRET_NOT_CONFIGURED_ERROR = "tickets_secret_not_configured"

# The OS's verdict on one write, by status. The client keys on the status and
# reports the fixed name; it does not read the body of these answers.
_CLASSIFICATION_VERDICTS = {
    400: "bad_payload",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "signal_ahead",
}
_SOURCE_SHADOW_VERDICTS = {
    400: "bad_payload",
    413: "payload_too_large",
}

# A ticket ref is an OS uuid or a HubSpot numeric id. Anything else would need
# encoding in the path, and the path is signed exactly as it is requested.
_REF_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}")
_ERROR_BODY_CAP = 65536


class TicketsApiError(Exception):
    """A tickets route could not be used. `code` is one of:

        list_timeout        no answer inside the budget, or no connection
        list_error          an answer that is not usable (any other status, a body
                            that is not a JSON object, an unresolvable origin)
        signature_rejected  HTTP 401
        secret_missing      the secret file is absent or short on this side, or
                            the OS answered 503 naming its own unset secret

    `http_status` is the status when there was one. The message is the code.
    """

    def __init__(self, code: str, http_status: Optional[int] = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A signed request is never re-sent to a location the response chooses.

    urllib would follow a 301/302/303 and turn a POST into a GET with no body.
    Returning None makes the 3xx surface as an HTTP error, which is list_error.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _log(msg: str) -> None:
    print(f"[tickets_api] {msg}", file=sys.stderr)


# ---------------------------------------------------------------- signing


def canonical_query(pairs: list[tuple[str, str]]) -> str:
    """Percent-encode names and values with an empty safe set, sort, join with `&`.

    A duplicated name is refused: the OS verifier refuses it too.
    """
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise ValueError("a query name is duplicated")
    encoded = sorted(
        (urllib.parse.quote(str(name), safe=""), urllib.parse.quote(str(value), safe=""))
        for name, value in pairs
    )
    return "&".join(f"{name}={value}" for name, value in encoded)


def canonical_string(
    method: str,
    path: str,
    pairs: list[tuple[str, str]],
    timestamp: "str | int",
    body: bytes,
) -> str:
    """The six signed lines. `body` is the exact bytes sent (b"" for a GET)."""
    return "\n".join(
        [
            SIGNATURE_VERSION,
            method.upper(),
            path,
            canonical_query(pairs),
            str(timestamp),
            hashlib.sha256(body).hexdigest(),
        ]
    )


def sign(secret: str, canonical: str) -> str:
    """Lower-case hex HMAC-SHA256 of the canonical string under the secret."""
    return hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ------------------------------------------------------- secret and origin


def _usable_secret(text: str) -> str:
    secret = text.strip()
    if len(secret) < MIN_SECRET_LENGTH:
        _log(f"secret is shorter than {MIN_SECRET_LENGTH} characters; no request made")
        raise TicketsApiError("secret_missing")
    return secret


def read_secret(path: Path = SECRET_PATH) -> str:
    """The tickets API secret, surrounding whitespace stripped.

    A missing or unreadable file, or fewer than 32 characters after the strip,
    raises TicketsApiError("secret_missing"). The value is never logged.
    """
    try:
        text: Optional[str] = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    if text is None:
        _log(f"secret file {Path(path).name} is missing or unreadable; no request made")
        raise TicketsApiError("secret_missing")
    return _usable_secret(text)


def read_os_origin() -> str:
    """The OS origin (scheme and host) from `os_ingest_url` in config/settings.yaml.

    Same line scan as scripts/post_action_items.py (the cron venv ships no YAML
    parser), reduced to scheme and host: any path on the configured value is
    dropped. An absent, blank or placeholder value raises
    TicketsApiError("list_error") — there is no default origin.
    """
    try:
        text = SETTINGS_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    origin: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#") or ":" not in line:
            continue
        key, _, rest = line.partition(":")
        if key.strip() != "os_ingest_url":
            continue
        value = rest.split("#", 1)[0].strip().strip('"').strip("'")
        if value and "REPLACE" not in value and "example.com" not in value:
            parts = urllib.parse.urlsplit(value)
            if parts.scheme in ("http", "https") and parts.netloc:
                origin = f"{parts.scheme}://{parts.netloc}"
        break
    if origin is None:
        _log("os_ingest_url is unset or unusable in settings.yaml; no request made")
        raise TicketsApiError("list_error")
    return origin


# ------------------------------------------------------------- formatting


def iso_millis_z(dt: datetime) -> str:
    """ISO 8601 UTC with exactly three fractional digits and a trailing Z.

    The only formatter of an instant this client sends. A naive datetime is
    refused. The milliseconds are cut from the microseconds by integer division,
    so an instant is never rounded up into the next second.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("iso_millis_z needs a timezone-aware datetime")
    utc = dt.astimezone(timezone.utc)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
        f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
        f".{utc.microsecond // 1000:03d}Z"
    )


# ---------------------------------------------------------------- transport


def _ticket_path_segment(ref: str) -> str:
    text = str(ref)
    if not _REF_PATTERN.fullmatch(text):
        raise ValueError("a ticket ref must be an OS uuid or a HubSpot numeric id")
    return text


def _names_unconfigured_secret(raw: bytes) -> bool:
    """True only for a JSON object whose `error` is the OS's unset-secret error."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get("error") == SECRET_NOT_CONFIGURED_ERROR


def _send(
    route: str,
    method: str,
    path: str,
    pairs: list[tuple[str, str]],
    body: bytes,
    *,
    base_url: Optional[str],
    secret: Optional[str],
    now: Optional[Callable[[], float]],
    opener: Optional[Callable[..., Any]],
) -> tuple[int, bytes]:
    """Sign and send one request. Returns (status, body bytes of a 2xx).

    Raises TicketsApiError for 401, for a 503 of either kind and for a transport
    failure. Every other status is returned with an empty body for the caller to
    classify; the body of a non-2xx answer is dropped here and goes no further.
    """
    key = _usable_secret(secret) if secret is not None else read_secret(SECRET_PATH)
    origin = base_url.rstrip("/") if base_url is not None else read_os_origin()
    timestamp = str(int((now if now is not None else time.time)()))

    query = canonical_query(pairs)
    signature = sign(key, canonical_string(method, path, pairs, timestamp, body))
    url = origin + path + (f"?{query}" if query else "")

    request = urllib.request.Request(url, data=body if method != "GET" else None, method=method)
    request.add_header(SIGNATURE_HEADER, signature)
    request.add_header(TIMESTAMP_HEADER, timestamp)
    if method != "GET":
        request.add_header("Content-Type", "application/json")

    send = opener if opener is not None else _OPENER.open
    status: Optional[int] = None
    raw = b""
    failure: Optional[str] = None
    try:
        with send(request, timeout=TIMEOUT_S) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = int(error.code)
        if status == 503:
            # Read once more, for one key, to tell the OS's unset secret from a
            # gateway answering during a redeploy. The bytes are dropped below.
            try:
                raw = error.read(_ERROR_BODY_CAP)
            except Exception:  # noqa: BLE001 - an unreadable body is "any other 503"
                raw = b""
        try:
            error.close()
        except Exception:  # noqa: BLE001
            pass
    except http.client.HTTPException:
        failure = "list_error"  # an answer arrived, and it was not valid HTTP
    except (urllib.error.URLError, TimeoutError, OSError):
        failure = "list_timeout"

    # Raised here, outside the handlers, so the transport exception (which can
    # hold the response) is not kept as this error's context.
    if failure is not None:
        _log(f"{route} failed code={failure}")
        raise TicketsApiError(failure)
    assert status is not None
    if status == 401:
        _log(f"{route} -> HTTP 401 code=signature_rejected")
        raise TicketsApiError("signature_rejected", 401)
    if status == 503:
        code = "secret_missing" if _names_unconfigured_secret(raw) else "list_error"
        _log(f"{route} -> HTTP 503 code={code}")
        raise TicketsApiError(code, 503)
    if 200 <= status < 300:
        return status, raw
    return status, b""


def _json_object(route: str, status: int, raw: bytes) -> dict:
    """The JSON object of a 2xx body; anything else is list_error."""
    try:
        parsed: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        _log(f"{route} -> HTTP {status} code=list_error (not a JSON object)")
        raise TicketsApiError("list_error", status)
    return parsed


def _refused(route: str, status: int) -> TicketsApiError:
    _log(f"{route} -> HTTP {status} code=list_error")
    return TicketsApiError("list_error", status)


def _post(route: str, path: str, payload: dict, verdicts: dict[int, str], **seams: Any) -> dict:
    # Serialise once: these exact bytes are hashed into the signature and sent.
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    status, raw = _send(route, "POST", path, [], body, **seams)
    if 200 <= status < 300:
        flag = _json_object(route, status, raw).get("replayed")
        _log(f"{route} -> HTTP {status}")
        return {
            "status": status,
            "error": None,
            "replayed": flag if isinstance(flag, bool) else None,
        }
    if status in verdicts:
        _log(f"{route} -> HTTP {status} error={verdicts[status]}")
        return {"status": status, "error": verdicts[status], "replayed": None}
    raise _refused(route, status)


# ------------------------------------------------------------ the four calls


def fetch_unprocessed(
    limit: int,
    *,
    base_url: Optional[str] = None,
    secret: Optional[str] = None,
    now: Optional[Callable[[], float]] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> dict:
    """The OS's list of tickets to process: the parsed response object
    (`contract_version`, `generated_at`, `tickets`).

    `limit` is clamped into 1..MAX_LIST_LIMIT before the query is built, so the
    clamped value is what is signed and sent. Raises TicketsApiError on every
    failure, including a 400.
    """
    clamped = max(1, min(MAX_LIST_LIMIT, int(limit)))
    pairs = [("limit", str(clamped)), ("contract_version", CONTRACT_VERSION)]
    route = "unprocessed"
    status, raw = _send(
        route, "GET", "/api/tickets/unprocessed", pairs, b"",
        base_url=base_url, secret=secret, now=now, opener=opener,
    )
    if not 200 <= status < 300:
        raise _refused(route, status)
    parsed = _json_object(route, status, raw)
    tickets = parsed.get("tickets")
    _log(f"{route} -> HTTP {status} count={len(tickets) if isinstance(tickets, list) else 0}")
    return parsed


def fetch_ticket(
    ticket_ref: str,
    *,
    base_url: Optional[str] = None,
    secret: Optional[str] = None,
    now: Optional[Callable[[], float]] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> Optional[dict]:
    """One ticket by OS id or HubSpot id: the parsed response object
    (`contract_version`, `ticket`, `unprocessed`), or None when the OS answers
    404 (unknown or deleted). Every other failure raises TicketsApiError.

    The request is exactly `/api/tickets/<ref>?contract_version=1`.
    """
    path = f"/api/tickets/{_ticket_path_segment(ticket_ref)}"
    pairs = [("contract_version", CONTRACT_VERSION)]
    route = "ticket"
    status, raw = _send(
        route, "GET", path, pairs, b"",
        base_url=base_url, secret=secret, now=now, opener=opener,
    )
    if status == 404:
        _log(f"{route} -> HTTP 404")
        return None
    if not 200 <= status < 300:
        raise _refused(route, status)
    parsed = _json_object(route, status, raw)
    _log(f"{route} -> HTTP {status} count=1")
    return parsed


def post_classification(
    os_ticket_id: str,
    payload: dict,
    *,
    base_url: Optional[str] = None,
    secret: Optional[str] = None,
    now: Optional[Callable[[], float]] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> dict:
    """Report one pass to `/api/tickets/<os_ticket_id>/classification`.

    Returns `{"status", "error", "replayed"}` for the statuses that are the OS's
    verdict on this one request:

        200  error None, replayed = the body's flag
        400  bad_payload         404  not_found        409  conflict
        413  payload_too_large   422  signal_ahead

    Raises TicketsApiError for 401 (signature_rejected), a 503 naming the unset
    secret (secret_missing), any other 503 or 5xx or status (list_error), a 2xx
    whose body is not a JSON object (list_error) and a transport failure
    (list_timeout).
    """
    path = f"/api/tickets/{_ticket_path_segment(os_ticket_id)}/classification"
    return _post(
        "classification", path, payload, _CLASSIFICATION_VERDICTS,
        base_url=base_url, secret=secret, now=now, opener=opener,
    )


def post_source_shadow_observations(
    payload: dict,
    *,
    base_url: Optional[str] = None,
    secret: Optional[str] = None,
    now: Optional[Callable[[], float]] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> dict:
    """Send one run's source comparison to `/api/tickets/source-shadow`.

    Same signing and the same result shape as post_classification. 200, 400
    (bad_payload) and 413 (payload_too_large) come back as results; everything
    else raises TicketsApiError.
    """
    return _post(
        "source-shadow", "/api/tickets/source-shadow", payload, _SOURCE_SHADOW_VERDICTS,
        base_url=base_url, secret=secret, now=now, opener=opener,
    )
