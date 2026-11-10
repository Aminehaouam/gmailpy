"""
gmail_client.py - Gmail API integration (official google-api-python-client).

What this module can do - and nothing more:
    * sign in with OAuth 2.0 (Desktop application flow, browser based)
    * read the account's e-mail address (users.getProfile)
    * list drafts, read drafts (users.drafts.list / users.drafts.get)
    * replace the content of an EXISTING draft (users.drafts.update)

Safety: every Gmail call goes through SafeGmailService, an allowlist wrapper
around the API client.  Sending (drafts.send, messages.send), creating
drafts, deleting drafts and every other endpoint raise
ForbiddenOperationError before any request is made.

Passwords are never requested or stored.  Only the OAuth token is stored,
encrypted with Windows DPAPI (see secure_storage.py).
"""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import hashlib
import io
import json
import logging
import shutil
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from config import GMAIL_SCOPES, AppPaths
from secure_storage import TokenStore

log = logging.getLogger(__name__)

# Retries for transient errors (HTTP 5xx, 429, rate-limit 403, timeouts).
# drafts.update is an idempotent PUT, so retrying it is safe.
NUM_RETRIES = 3
# Media uploads above this size use a resumable upload.
RESUMABLE_THRESHOLD = 5 * 1024 * 1024


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class GmailError(Exception):
    """Base class for all Gmail related errors (message is user friendly)."""


class CredentialsFileError(GmailError):
    """The OAuth client JSON file is missing or invalid."""


class GmailAuthError(GmailError):
    """Not signed in, token expired/revoked, or permission missing."""


class GmailNetworkError(GmailError):
    """Network problem - for an update the outcome may be unknown."""


class DraftNotFoundError(GmailError):
    """The draft no longer exists (sent, deleted, or never existed)."""


class GmailApiError(GmailError):
    """Gmail returned an error response."""

    def __init__(self, message: str, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


class ForbiddenOperationError(GmailError):
    """A Gmail operation outside the allowlist (send, create, delete, ...)."""


# --------------------------------------------------------------------------
# Allowlist wrapper: the application can ONLY list/get/update drafts
# --------------------------------------------------------------------------

class _Allowlist:
    """Generic proxy exposing only explicitly allowed attributes."""

    def __init__(self, target: Any, allowed: dict[str, Callable[[Any], Any] | None], path: str):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_allowed", allowed)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str) -> Any:
        allowed = object.__getattribute__(self, "_allowed")
        path = object.__getattribute__(self, "_path")
        if name not in allowed:
            raise ForbiddenOperationError(
                f"Blocked: Gmail operation '{path}.{name}' is not permitted. "
                "This application never sends e-mails, never creates drafts and never "
                "deletes drafts."
            )
        attr = getattr(object.__getattribute__(self, "_target"), name)
        wrapper = allowed[name]
        return wrapper(attr) if wrapper else attr

    def __setattr__(self, name: str, value: Any) -> None:  # pragma: no cover - defensive
        raise ForbiddenOperationError("Gmail service wrapper is read-only")


def _wrap_drafts_factory(drafts_factory: Callable[[], Any]) -> Callable[[], Any]:
    def drafts() -> Any:
        return _Allowlist(
            drafts_factory(),
            {"list": None, "list_next": None, "get": None, "update": None},
            "users().drafts()",
        )
    return drafts


def _wrap_users_factory(users_factory: Callable[[], Any]) -> Callable[[], Any]:
    def users() -> Any:
        return _Allowlist(
            users_factory(),
            {"getProfile": None, "drafts": _wrap_drafts_factory},
            "users()",
        )
    return users


class SafeGmailService(_Allowlist):
    """Wraps a googleapiclient Gmail service.  Allowed calls:

        users().getProfile
        users().drafts().list / list_next / get / update

    Everything else - in particular drafts().send, drafts().create,
    drafts().delete and the whole messages() resource - is blocked."""

    def __init__(self, service: Any):
        super().__init__(service, {"users": _wrap_users_factory}, "service")


# --------------------------------------------------------------------------
# Data classes describing drafts
# --------------------------------------------------------------------------

@dataclass
class AttachmentInfo:
    filename: str
    mime_type: str
    size: int
    is_inline: bool = False
    sha256: str | None = None  # only known when the raw message was read

    def describe(self) -> str:
        inline = ", inline" if self.is_inline else ""
        return f"{self.filename} ({human_size(self.size)}{inline})"


@dataclass
class DraftRef:
    draft_id: str
    message_id: str
    thread_id: str | None


@dataclass
class DraftInfo:
    draft_id: str
    message_id: str
    thread_id: str | None
    subject: str = ""
    to: list[tuple[str, str]] = field(default_factory=list)   # (display name, address)
    cc: list[tuple[str, str]] = field(default_factory=list)
    bcc: list[tuple[str, str]] = field(default_factory=list)
    body_text: str = ""
    attachments: list[AttachmentInfo] = field(default_factory=list)
    internal_date: datetime | None = None
    snippet: str = ""

    @property
    def recipients(self) -> list[tuple[str, str, str]]:
        """All recipients as (kind, display name, address)."""
        return (
            [("To", n, a) for n, a in self.to]
            + [("Cc", n, a) for n, a in self.cc]
            + [("Bcc", n, a) for n, a in self.bcc]
        )

    def recipients_display(self, include_kind: bool = True) -> str:
        parts = []
        for kind, group in (("To", self.to), ("Cc", self.cc), ("Bcc", self.bcc)):
            if group:
                text = ", ".join(_format_address(n, a) for n, a in group)
                parts.append(f"{kind}: {text}" if include_kind else text)
        return "; ".join(parts) if parts else "(no recipient)"

    def primary_recipient(self) -> str:
        if self.to:
            return _format_address(*self.to[0]) + (f" (+{len(self.to) - 1})" if len(self.to) > 1 else "")
        return self.recipients_display(include_kind=True)

    @property
    def date_display(self) -> str:
        if not self.internal_date:
            return "unknown"
        return self.internal_date.astimezone().strftime("%Y-%m-%d %H:%M")

    def attachments_display(self) -> str:
        if not self.attachments:
            return "(none)"
        return ", ".join(a.describe() for a in self.attachments)


@dataclass
class RawDraft:
    draft_id: str
    message_id: str
    thread_id: str | None
    raw: bytes


def _format_address(name: str, address: str) -> str:
    if name and address:
        return f"{name} <{address}>"
    return address or name or ""


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"  # pragma: no cover


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

class _HTMLTextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "title"}
    _BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "table", "section", "article", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.chunks.append(data)


def html_to_text(html: str) -> str:
    """Visible text of an HTML body (used only for matching)."""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed HTML - fall back to crude tag stripping
        import re
        return re.sub(r"<[^>]+>", " ", html)
    return "".join(parser.chunks)


def decode_mime_words(value: str | None) -> str:
    """Decode RFC 2047 encoded words (=?UTF-8?B?...?=); idempotent."""
    if not value:
        return ""
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def parse_address_list(values: list[str]) -> list[tuple[str, str]]:
    result = []
    for name, address in getaddresses([v for v in values if v]):
        name = decode_mime_words(name).strip()
        address = address.strip()
        if name or address:
            result.append((name, address))
    return result


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _charset_from_content_type(content_type: str) -> str:
    msg = email.message.Message()
    msg["Content-Type"] = content_type or "text/plain"
    return msg.get_content_charset() or "utf-8"


def _decode_text(data: bytes, charset: str) -> str:
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def parse_draft_resource(draft: dict[str, Any]) -> tuple[DraftInfo, bool]:
    """Turn a drafts.get(format='full') response into a DraftInfo.

    Returns (info, body_incomplete); body_incomplete is True when a text body
    part was not delivered inline (very large bodies) - the caller then reads
    the raw message to obtain the full text."""
    message = draft.get("message", {}) or {}
    payload = message.get("payload", {}) or {}
    headers = {}
    for header in payload.get("headers", []) or []:
        headers.setdefault(header.get("name", "").lower(), []).append(header.get("value", ""))

    info = DraftInfo(
        draft_id=draft.get("id", ""),
        message_id=message.get("id", ""),
        thread_id=message.get("threadId"),
        subject=decode_mime_words((headers.get("subject") or [""])[0]),
        to=parse_address_list(headers.get("to", [])),
        cc=parse_address_list(headers.get("cc", [])),
        bcc=parse_address_list(headers.get("bcc", [])),
        snippet=message.get("snippet", "") or "",
    )
    internal = message.get("internalDate")
    if internal:
        try:
            info.internal_date = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            info.internal_date = None

    texts: list[str] = []
    body_incomplete = False

    def walk(part: dict[str, Any]) -> None:
        nonlocal body_incomplete
        mime_type = (part.get("mimeType") or "").lower()
        part_headers = {h.get("name", "").lower(): h.get("value", "") for h in part.get("headers", []) or []}
        filename = part.get("filename") or ""
        body = part.get("body", {}) or {}
        disposition = part_headers.get("content-disposition", "").split(";")[0].strip().lower()
        if part.get("parts"):
            for child in part["parts"]:
                walk(child)
            return
        if filename or disposition == "attachment" or (body.get("attachmentId") and not mime_type.startswith("text/")):
            info.attachments.append(AttachmentInfo(
                filename=decode_mime_words(filename) or "(unnamed attachment)",
                mime_type=mime_type,
                size=int(body.get("size", 0) or 0),
                is_inline=disposition == "inline",
            ))
            return
        if mime_type in ("text/plain", "text/html"):
            if body.get("data"):
                charset = _charset_from_content_type(part_headers.get("content-type", ""))
                text = _decode_text(_b64url_decode(body["data"]), charset)
                texts.append(html_to_text(text) if mime_type == "text/html" else text)
            elif body.get("attachmentId") or int(body.get("size", 0) or 0) > 0:
                body_incomplete = True

    if payload:
        walk(payload)
    info.body_text = "\n".join(texts)
    return info, body_incomplete


@dataclass
class ParsedRawMessage:
    """Information extracted from a raw RFC 822 message."""

    subject: str
    to: list[tuple[str, str]]
    cc: list[tuple[str, str]]
    bcc: list[tuple[str, str]]
    body_text: str
    attachments: list[AttachmentInfo]


def parse_raw_message(raw: bytes) -> ParsedRawMessage:
    """Parse a raw message (drafts.get format='raw') with the stdlib."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    def header_values(name: str) -> list[str]:
        return [str(v) for v in (msg.get_all(name) or [])]

    texts: list[str] = []
    attachments: list[AttachmentInfo] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_type = part.get_content_type()
        if filename or disposition == "attachment":
            data = part.get_payload(decode=True) or b""
            attachments.append(AttachmentInfo(
                filename=filename or "(unnamed attachment)",
                mime_type=content_type,
                size=len(data),
                is_inline=disposition == "inline",
                sha256=hashlib.sha256(data).hexdigest(),
            ))
        elif content_type in ("text/plain", "text/html"):
            try:
                text = part.get_content()
            except (LookupError, UnicodeError, AssertionError):
                text = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
            texts.append(html_to_text(text) if content_type == "text/html" else text)

    return ParsedRawMessage(
        subject=str(msg.get("Subject", "") or ""),
        to=parse_address_list(header_values("To")),
        cc=parse_address_list(header_values("Cc")),
        bcc=parse_address_list(header_values("Bcc")),
        body_text="\n".join(texts),
        attachments=attachments,
    )


# --------------------------------------------------------------------------
# OAuth helpers
# --------------------------------------------------------------------------

def validate_client_secret_file(path: Path) -> dict[str, Any]:
    """Check that *path* is a Google OAuth client JSON for a Desktop app."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise CredentialsFileError(f"File not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise CredentialsFileError(f"The file is not a valid JSON file: {exc}") from exc
    if not isinstance(data, dict):
        raise CredentialsFileError("The file does not contain OAuth client credentials.")
    if "web" in data and "installed" not in data:
        raise CredentialsFileError(
            "This OAuth client is of type 'Web application'. Please create an OAuth client "
            "of type 'Desktop app' in the Google Cloud Console and download its JSON file."
        )
    installed = data.get("installed")
    if not isinstance(installed, dict):
        raise CredentialsFileError(
            "The file does not contain Desktop app OAuth credentials (missing 'installed' section)."
        )
    missing = [k for k in ("client_id", "client_secret", "auth_uri", "token_uri") if not installed.get(k)]
    if missing:
        raise CredentialsFileError(f"The OAuth client file is incomplete (missing: {', '.join(missing)}).")
    return data


def install_client_secret_file(source: Path, paths: AppPaths) -> None:
    """Validate the user's OAuth client JSON and copy it into the data folder."""
    validate_client_secret_file(source)
    if source.resolve() != paths.client_secret_file.resolve():
        shutil.copyfile(source, paths.client_secret_file)
    log.info("OAuth client credentials installed")


class _UrlCapturingFlow(InstalledAppFlow):
    """InstalledAppFlow that reports the sign-in URL (so the GUI can offer it
    if the browser does not open automatically)."""

    url_callback: Callable[[str], None] | None = None

    def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
        url, state = super().authorization_url(**kwargs)
        if self.url_callback:
            try:
                self.url_callback(url)
            except Exception:  # pragma: no cover - GUI callback problems must not break sign-in
                log.debug("url_callback failed", exc_info=True)
        return url, state


def _token_has_required_scopes(info: dict[str, Any]) -> bool:
    scopes = info.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return all(scope in scopes for scope in GMAIL_SCOPES)


def load_saved_credentials(token_store: TokenStore) -> Credentials | None:
    token_json = token_store.load()
    if not token_json:
        return None
    try:
        info = json.loads(token_json)
    except ValueError:
        log.warning("Stored OAuth token is corrupt - a new sign-in is required")
        return None
    if not _token_has_required_scopes(info):
        log.warning("Stored OAuth token lacks the required Gmail permission - a new sign-in is required")
        return None
    try:
        return Credentials.from_authorized_user_info(info, GMAIL_SCOPES)
    except (ValueError, KeyError):
        log.warning("Stored OAuth token is invalid - a new sign-in is required")
        return None


def obtain_credentials(
    paths: AppPaths,
    token_store: TokenStore,
    *,
    interactive: bool,
    timeout_seconds: int = 300,
    url_callback: Callable[[str], None] | None = None,
) -> Credentials:
    """Return valid credentials: stored token, refreshed token, or (if
    *interactive*) a new browser sign-in.  Raises GmailAuthError otherwise."""
    creds = load_saved_credentials(token_store)

    if creds and creds.valid:
        return creds

    if creds and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_store.save(creds.to_json())
            log.info("OAuth access token refreshed")
            return creds
        except RefreshError:
            # Refresh token expired (7 days for apps in "Testing" mode) or revoked.
            log.warning("OAuth refresh token expired or revoked - deleting stored token")
            token_store.delete()
            creds = None
        except TransportError as exc:
            raise GmailNetworkError(f"Could not reach Google to refresh the sign-in: {exc}") from exc

    if not interactive:
        raise GmailAuthError("Gmail is not connected or the authorization has expired. "
                             "Click 'Connect Gmail' to sign in again.")

    if not paths.client_secret_file.exists():
        raise CredentialsFileError(
            "No OAuth client file configured. Select the client JSON file you downloaded "
            "from the Google Cloud Console."
        )
    validate_client_secret_file(paths.client_secret_file)

    flow = _UrlCapturingFlow.from_client_secrets_file(str(paths.client_secret_file), scopes=GMAIL_SCOPES)
    flow.url_callback = url_callback
    log.info("Starting browser sign-in (timeout %ss)", timeout_seconds)
    try:
        creds = flow.run_local_server(
            host="localhost",
            port=0,
            open_browser=True,
            authorization_prompt_message=None,
            success_message=(
                "Bewerbung Attach Assistant is now connected to Gmail. "
                "You can close this browser tab and return to the application."
            ),
            timeout_seconds=timeout_seconds,
        )
    except WSGITimeoutError as exc:
        raise GmailAuthError("The browser sign-in was not completed in time. Please try again.") from exc
    except Exception as exc:  # oauthlib raises various errors on denial/mismatch
        raise GmailAuthError(f"The browser sign-in failed: {exc}") from exc

    if not creds or not creds.refresh_token:
        log.warning("Sign-in did not return a refresh token; re-authentication may be needed later")
    if creds.scopes is not None and not all(s in (creds.scopes or []) for s in GMAIL_SCOPES):
        raise GmailAuthError(
            "Gmail permission was not granted. Please allow 'manage drafts' when signing in."
        )
    token_store.save(creds.to_json())
    return creds


def revoke_token(token_store: TokenStore) -> None:
    """Best effort: revoke the token at Google, then delete it locally."""
    creds = load_saved_credentials(token_store)
    if creds is not None:
        token = creds.refresh_token or creds.token
        if token:
            try:
                import requests
                requests.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": token},
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
                log.info("OAuth token revoked at Google")
            except Exception as exc:  # network problems: still delete locally
                log.warning("Could not revoke token at Google: %s", type(exc).__name__)
    token_store.delete()


# --------------------------------------------------------------------------
# Gmail client
# --------------------------------------------------------------------------

def _http_error_reason(exc: HttpError) -> str:
    try:
        data = json.loads(exc.content.decode("utf-8"))
        error = data.get("error", {})
        errors = error.get("errors") or []
        if errors and errors[0].get("reason"):
            return str(errors[0]["reason"])
        if error.get("status"):
            return str(error["status"])
    except Exception:
        pass
    return ""


def _http_error_message(exc: HttpError) -> str:
    try:
        data = json.loads(exc.content.decode("utf-8"))
        return str(data.get("error", {}).get("message") or exc.reason or "")
    except Exception:
        return str(getattr(exc, "reason", "") or "")


class GmailClient:
    """High-level, safety-restricted access to Gmail drafts."""

    def __init__(self, service: Any, account_email: str = "", num_retries: int = NUM_RETRIES):
        # Every call goes through the allowlist wrapper.
        self._service = service if isinstance(service, SafeGmailService) else SafeGmailService(service)
        self.account_email = account_email
        self.num_retries = num_retries
        # Cache of parsed drafts keyed by message id.  Gmail creates a new
        # message id whenever a draft is edited, so cached entries never
        # become stale - an edited draft simply has a new id.
        self._cache: dict[str, DraftInfo] = {}

    # -- construction -----------------------------------------------------
    @classmethod
    def connect(
        cls,
        paths: AppPaths,
        token_store: TokenStore,
        *,
        interactive: bool = True,
        timeout_seconds: int = 300,
        url_callback: Callable[[str], None] | None = None,
    ) -> "GmailClient":
        creds = obtain_credentials(
            paths, token_store, interactive=interactive,
            timeout_seconds=timeout_seconds, url_callback=url_callback,
        )
        try:
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        except Exception as exc:
            raise GmailError(f"Could not initialise the Gmail API client: {exc}") from exc
        client = cls(service)
        try:
            profile = client._execute(client._service.users().getProfile(userId="me"), "read profile")
        except GmailAuthError:
            token_store.delete()
            raise
        client.account_email = profile.get("emailAddress", "")
        log.info("Connected to Gmail as %s", client.account_email)
        return client

    # -- request execution with error mapping -----------------------------
    def _execute(self, request: Any, what: str) -> Any:
        try:
            return request.execute(num_retries=self.num_retries)
        except ForbiddenOperationError:
            raise
        except RefreshError as exc:
            raise GmailAuthError(
                "Your Gmail authorization has expired or was revoked. "
                "Click 'Connect Gmail' to sign in again."
            ) from exc
        except HttpError as exc:
            status = int(getattr(exc.resp, "status", 0) or 0)
            reason = _http_error_reason(exc)
            message = _http_error_message(exc)
            log.warning("Gmail API error during %s: HTTP %s %s %s", what, status, reason, message)
            if status == 401:
                raise GmailAuthError(
                    "Gmail rejected the authorization (expired or revoked). "
                    "Click 'Connect Gmail' to sign in again."
                ) from exc
            if status == 403 and reason in (
                "insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "PERMISSION_DENIED",
                "forbidden",
            ):
                raise GmailAuthError(
                    "The Gmail permission is missing. Disconnect and connect Gmail again, "
                    "and allow access to drafts."
                ) from exc
            if status == 404:
                raise DraftNotFoundError(
                    "The draft no longer exists in Gmail (it may have been sent or deleted)."
                ) from exc
            if status == 429 or reason in ("rateLimitExceeded", "userRateLimitExceeded"):
                raise GmailApiError(
                    "Gmail's rate limit was reached. Please wait a minute and try again.",
                    status, reason,
                ) from exc
            raise GmailApiError(f"Gmail API error during {what} (HTTP {status}): {message or reason}",
                                status, reason) from exc
        except TransportError as exc:
            raise GmailNetworkError(f"Network error during {what}: {exc}") from exc
        except (socket.timeout, TimeoutError, ConnectionError, ssl.SSLError,
                httplib2.HttpLib2Error, OSError) as exc:
            raise GmailNetworkError(
                f"Network error during {what}: {type(exc).__name__}: {exc}"
            ) from exc

    # -- drafts -------------------------------------------------------------
    def list_draft_refs(self, max_drafts: int = 500) -> tuple[list[DraftRef], bool]:
        """All drafts (id, message id, thread id).  Returns (refs, truncated)."""
        drafts = self._service.users().drafts()
        refs: list[DraftRef] = []
        truncated = False
        request = drafts.list(userId="me", maxResults=min(500, max(1, max_drafts)))
        while request is not None:
            response = self._execute(request, "list drafts")
            for item in response.get("drafts", []) or []:
                message = item.get("message", {}) or {}
                refs.append(DraftRef(item.get("id", ""), message.get("id", ""), message.get("threadId")))
            if len(refs) >= max_drafts:
                truncated = len(refs) > max_drafts or bool(response.get("nextPageToken"))
                break
            request = drafts.list_next(request, response)
        return refs[:max_drafts], truncated

    def get_draft_info(self, draft_id: str) -> DraftInfo:
        """Read one draft (headers, text, attachment list) - format=full."""
        response = self._execute(
            self._service.users().drafts().get(userId="me", id=draft_id, format="full"),
            "read draft",
        )
        info, body_incomplete = parse_draft_resource(response)
        if body_incomplete:
            # Very large text bodies are not delivered inline; read raw instead.
            raw = self.get_draft_raw(draft_id)
            if raw.message_id == info.message_id:
                info.body_text = parse_raw_message(raw.raw).body_text
        return info

    def fetch_all_drafts(
        self,
        max_drafts: int = 500,
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[list[DraftInfo], bool]:
        """Read every draft.  Unchanged drafts come from the cache."""
        refs, truncated = self.list_draft_refs(max_drafts)
        result: list[DraftInfo] = []
        for index, ref in enumerate(refs, 1):
            if progress:
                progress(index, len(refs))
            cached = self._cache.get(ref.message_id)
            if cached is not None and cached.draft_id == ref.draft_id:
                result.append(cached)
                continue
            try:
                info = self.get_draft_info(ref.draft_id)
            except DraftNotFoundError:
                # Sent or deleted while we were reading - skip it.
                log.info("Draft %s disappeared while reading drafts", ref.draft_id)
                continue
            self._cache[info.message_id] = info
            result.append(info)
        return result, truncated

    def get_draft_message_id(self, draft_id: str) -> str:
        """Current message id of a draft (cheap format=minimal request)."""
        response = self._execute(
            self._service.users().drafts().get(userId="me", id=draft_id, format="minimal"),
            "check draft",
        )
        return (response.get("message", {}) or {}).get("id", "")

    def get_draft_raw(self, draft_id: str) -> RawDraft:
        """The complete RFC 822 message of a draft, including attachments."""
        response = self._execute(
            self._service.users().drafts().get(userId="me", id=draft_id, format="raw"),
            "read draft content",
        )
        message = response.get("message", {}) or {}
        raw_b64 = message.get("raw")
        if not raw_b64:
            raise GmailApiError("Gmail returned the draft without its content.")
        try:
            raw = _b64url_decode(raw_b64)
        except (binascii.Error, ValueError) as exc:
            raise GmailApiError(f"Gmail returned undecodable draft content: {exc}") from exc
        return RawDraft(response.get("id", draft_id), message.get("id", ""), message.get("threadId"), raw)

    def update_draft_raw(self, draft_id: str, thread_id: str | None, raw: bytes) -> dict[str, Any]:
        """Replace the content of the EXISTING draft *draft_id* with *raw*.

        Uses users.drafts.update (HTTP PUT) with a message/rfc822 media
        upload.  The draft id stays the same; Gmail assigns a new message id.
        This never sends the e-mail and never creates a new draft."""
        if not draft_id:
            raise ValueError("draft_id is required")
        media = MediaIoBaseUpload(
            io.BytesIO(raw), mimetype="message/rfc822", resumable=len(raw) > RESUMABLE_THRESHOLD
        )
        message: dict[str, Any] = {}
        if thread_id:
            message["threadId"] = thread_id
        request = self._service.users().drafts().update(
            userId="me", id=draft_id, body={"id": draft_id, "message": message}, media_body=media
        )
        response = self._execute(request, "update draft")
        if not isinstance(response, dict) or response.get("id") != draft_id:
            raise GmailApiError("Gmail did not confirm the update of the expected draft.")
        log.info("Gmail confirmed update of draft %s (new message id %s)",
                 draft_id, (response.get("message") or {}).get("id"))
        return response
