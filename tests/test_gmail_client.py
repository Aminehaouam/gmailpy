"""Gmail integration tested against the REAL google-api-python-client.

The Gmail service is built from the bundled discovery document; only the
HTTP transport is mocked (HttpMockSequence records every request), so these
tests check the exact HTTP calls the application would make.
"""

import base64
import json
import logging
import os
import stat
import sys
from urllib.parse import parse_qs, urlparse

import pytest
from googleapiclient.discovery import build_from_document
from googleapiclient.discovery_cache import get_static_doc
from googleapiclient.http import HttpMockSequence

import gmail_client
from config import GMAIL_SCOPES
from gmail_client import (
    CredentialsFileError,
    DraftNotFoundError,
    ForbiddenOperationError,
    GmailApiError,
    GmailAuthError,
    GmailClient,
    SafeGmailService,
    load_saved_credentials,
    obtain_credentials,
    parse_draft_resource,
    validate_client_secret_file,
)
from logging_setup import SecretRedactingFilter, redact
from secure_storage import TokenStore
import mime_samples as ms


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def make_client(responses, num_retries=0):
    http = HttpMockSequence(responses)
    service = build_from_document(get_static_doc("gmail", "v1"), http=http)
    return GmailClient(service, account_email="me@example.com", num_retries=num_retries), http


def ok(payload):
    return ({"status": "200", "content-type": "application/json"}, json.dumps(payload))


def error(status, reason="error", message="Error"):
    return ({"status": str(status), "content-type": "application/json"},
            json.dumps({"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}))


def assert_never_sends(http):
    for uri, method, _body, _headers in http.request_sequence:
        assert "/send" not in uri, uri
        assert method != "DELETE", uri
        assert not (method == "POST" and urlparse(uri).path.rstrip("/").endswith("/drafts")), uri


FULL_DRAFT = {
    "id": "r-1",
    "message": {
        "id": "m-1", "threadId": "t-1", "labelIds": ["DRAFT"], "internalDate": "1794301200000",
        "snippet": "Sehr geehrte",
        "payload": {
            "partId": "", "mimeType": "multipart/mixed", "filename": "",
            "headers": [
                {"name": "Subject", "value": "=?UTF-8?Q?Bewerbung_=E2=80=93_Trompetter_GmbH?="},
                {"name": "To", "value": '"Trompetter GmbH" <bewerbung@trompetter.de>, hr@trompetter.de'},
                {"name": "Cc", "value": "cc@example.com"},
            ],
            "body": {"size": 0},
            "parts": [
                {"partId": "0", "mimeType": "multipart/alternative", "filename": "", "headers": [],
                 "body": {"size": 0}, "parts": [
                     {"partId": "0.0", "mimeType": "text/plain", "filename": "",
                      "headers": [{"name": "Content-Type", "value": 'text/plain; charset="UTF-8"'}],
                      "body": {"size": 20, "data": b64url("Grüße von Amine".encode()).rstrip("=")}},
                     {"partId": "0.1", "mimeType": "text/html", "filename": "",
                      "headers": [{"name": "Content-Type", "value": "text/html; charset=ISO-8859-1"}],
                      "body": {"size": 40, "data": b64url("<p>Sehr geehrte <b>Damen</b> M\xfcller</p>".encode("latin-1"))}},
                 ]},
                {"partId": "1", "mimeType": "application/pdf", "filename": "Lebenslauf.pdf",
                 "headers": [{"name": "Content-Disposition", "value": 'attachment; filename="Lebenslauf.pdf"'}],
                 "body": {"size": 12345, "attachmentId": "ANGjdJ_abc"}},
            ],
        },
    },
}


def test_list_drafts_paginates():
    client, http = make_client([
        ok({"drafts": [{"id": "r-1", "message": {"id": "m-1", "threadId": "t-1"}}], "nextPageToken": "p2"}),
        ok({"drafts": [{"id": "r-2", "message": {"id": "m-2", "threadId": "t-2"}}]}),
    ])
    refs, truncated = client.list_draft_refs()
    assert [r.draft_id for r in refs] == ["r-1", "r-2"]
    assert not truncated
    first, second = http.request_sequence
    assert first[1] == "GET" and "/gmail/v1/users/me/drafts" in first[0]
    assert parse_qs(urlparse(second[0]).query)["pageToken"] == ["p2"]
    assert_never_sends(http)


def test_list_drafts_respects_limit():
    client, _http = make_client([
        ok({"drafts": [{"id": f"r-{i}", "message": {"id": f"m-{i}"}} for i in range(3)], "nextPageToken": "x"}),
    ])
    refs, truncated = client.list_draft_refs(max_drafts=3)
    assert len(refs) == 3 and truncated


def test_get_draft_info_parses_full_format():
    client, http = make_client([ok(FULL_DRAFT)])
    info = client.get_draft_info("r-1")
    assert parse_qs(urlparse(http.request_sequence[0][0]).query)["format"] == ["full"]
    assert info.subject == "Bewerbung – Trompetter GmbH"
    assert info.to == [("Trompetter GmbH", "bewerbung@trompetter.de"), ("", "hr@trompetter.de")]
    assert info.cc == [("", "cc@example.com")]
    assert "Grüße von Amine" in info.body_text
    assert "Sehr geehrte Damen Müller" in " ".join(info.body_text.split())  # HTML stripped, latin-1 decoded
    assert [(a.filename, a.size) for a in info.attachments] == [("Lebenslauf.pdf", 12345)]
    assert info.internal_date is not None
    assert info.message_id == "m-1" and info.thread_id == "t-1"


def test_get_draft_raw_decodes_base64url():
    raw = ms.alternative_draft("Bewerbung", "a@b.de")
    client, http = make_client([ok({"id": "r-1", "message": {"id": "m-1", "threadId": "t-1", "raw": b64url(raw)}})])
    result = client.get_draft_raw("r-1")
    assert result.raw == raw
    assert parse_qs(urlparse(http.request_sequence[0][0]).query)["format"] == ["raw"]


def test_update_uses_put_upload_of_existing_draft():
    raw = ms.alternative_draft("Bewerbung", "a@b.de")
    client, http = make_client([ok({"id": "r-1", "message": {"id": "m-2", "threadId": "t-1"}})])
    response = client.update_draft_raw("r-1", "t-1", raw)
    assert response["id"] == "r-1"
    (uri, method, body, headers), = http.request_sequence
    parsed = urlparse(uri)
    assert method == "PUT"                                     # update, not create (POST)
    assert parsed.path == "/upload/gmail/v1/users/me/drafts/r-1"
    assert parse_qs(parsed.query)["uploadType"] == ["multipart"]
    body_bytes = body if isinstance(body, bytes) else body.encode()
    assert b"message/rfc822" in body_bytes
    assert raw in body_bytes                                   # the complete message is uploaded
    assert b'"threadId": "t-1"' in body_bytes and b'"id": "r-1"' in body_bytes
    assert_never_sends(http)


def test_large_update_uses_resumable_upload(monkeypatch):
    monkeypatch.setattr(gmail_client, "RESUMABLE_THRESHOLD", 100)
    raw = ms.alternative_draft("Bewerbung", "a@b.de")
    assert len(raw) > 100
    client, http = make_client([
        ({"status": "200", "location": "https://gmail.googleapis.com/resumable/session-1"}, ""),
        ok({"id": "r-1", "message": {"id": "m-2", "threadId": "t-1"}}),
    ])
    assert client.update_draft_raw("r-1", None, raw)["id"] == "r-1"
    first, second = http.request_sequence
    assert first[1] == "PUT" and urlparse(first[0]).path == "/upload/gmail/v1/users/me/drafts/r-1"
    assert parse_qs(urlparse(first[0]).query)["uploadType"] == ["resumable"]
    assert first[3]["X-Upload-Content-Type"] == "message/rfc822"
    assert second[1] == "PUT" and second[0] == "https://gmail.googleapis.com/resumable/session-1"
    chunk = second[2].read() if hasattr(second[2], "read") else second[2]
    assert chunk == raw                                        # the complete message is uploaded
    assert_never_sends(http)


def test_update_must_confirm_same_draft():
    client, _ = make_client([ok({"id": "r-OTHER", "message": {"id": "m-2"}})])
    with pytest.raises(GmailApiError):
        client.update_draft_raw("r-1", None, b"Subject: x\r\n\r\nx")


@pytest.mark.parametrize("status, reason, expected", [
    (404, "notFound", DraftNotFoundError),
    (401, "authError", GmailAuthError),
    (403, "insufficientPermissions", GmailAuthError),
    (400, "invalidArgument", GmailApiError),
    (429, "rateLimitExceeded", GmailApiError),
    (500, "backendError", GmailApiError),
])
def test_http_errors_are_mapped(status, reason, expected):
    client, _ = make_client([error(status, reason)])
    with pytest.raises(expected):
        client.get_draft_info("r-1")


def test_transient_errors_are_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    client, http = make_client([error(503, "backendError"), ok(FULL_DRAFT)], num_retries=3)
    assert client.get_draft_info("r-1").draft_id == "r-1"
    assert len(http.request_sequence) == 2


@pytest.mark.parametrize("path", [
    lambda s: s.users().drafts().send,
    lambda s: s.users().drafts().create,
    lambda s: s.users().drafts().delete,
    lambda s: s.users().messages,
    lambda s: s.users().threads,
    lambda s: s.users().settings,
    lambda s: s.users().labels,
])
def test_safe_service_blocks_everything_but_drafts_read_update(path):
    http = HttpMockSequence([])  # any real request would fail with IndexError
    service = SafeGmailService(build_from_document(get_static_doc("gmail", "v1"), http=http))
    with pytest.raises(ForbiddenOperationError):
        path(service)(userId="me", body={})
    assert http.request_sequence == []


def test_client_wraps_service_in_allowlist():
    client, http = make_client([])
    with pytest.raises(ForbiddenOperationError):
        client._service.users().drafts().send(userId="me", body={"id": "r-1"}).execute()
    assert http.request_sequence == []


def test_parse_draft_resource_flags_large_bodies():
    resource = json.loads(json.dumps(FULL_DRAFT))
    plain = resource["message"]["payload"]["parts"][0]["parts"][0]
    plain["body"] = {"size": 999999, "attachmentId": "big"}
    info, incomplete = parse_draft_resource(resource)
    assert incomplete
    assert info.attachments[0].filename == "Lebenslauf.pdf"  # text part is not an attachment


# --------------------------------------------------------------------------
# OAuth credentials, token storage, log redaction
# --------------------------------------------------------------------------

def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_validate_client_secret_file(tmp_path):
    good = write_json(tmp_path / "good.json", {"installed": {
        "client_id": "123.apps.googleusercontent.com", "client_secret": "GOCSPX-abc",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"]}})
    assert validate_client_secret_file(good)["installed"]["client_id"].startswith("123")
    web = write_json(tmp_path / "web.json", {"web": {"client_id": "x"}})
    with pytest.raises(CredentialsFileError, match="Desktop app"):
        validate_client_secret_file(web)
    incomplete = write_json(tmp_path / "inc.json", {"installed": {"client_id": "x"}})
    with pytest.raises(CredentialsFileError, match="incomplete"):
        validate_client_secret_file(incomplete)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(CredentialsFileError):
        validate_client_secret_file(broken)
    with pytest.raises(CredentialsFileError):
        validate_client_secret_file(tmp_path / "missing.json")


def token_json(scopes=GMAIL_SCOPES, expired=True):
    return json.dumps({
        "token": "ya29.a0-secret-access", "refresh_token": "1//0g-secret-refresh",
        "client_id": "123.apps.googleusercontent.com", "client_secret": "GOCSPX-secret",
        "token_uri": "https://oauth2.googleapis.com/token", "scopes": scopes,
        "expiry": "2000-01-01T00:00:00Z" if expired else "2999-01-01T00:00:00Z",
    })


def test_token_store_round_trip_and_permissions(tmp_path):
    store = TokenStore(tmp_path / "token.bin")
    assert store.load() is None
    store.save(token_json())
    assert json.loads(store.load())["refresh_token"] == "1//0g-secret-refresh"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    store.delete()
    assert not store.exists()


def test_token_without_required_scope_is_ignored(tmp_path):
    store = TokenStore(tmp_path / "token.bin")
    store.save(token_json(scopes=["https://www.googleapis.com/auth/gmail.readonly"]))
    assert load_saved_credentials(store) is None
    store.save("{corrupt")
    assert load_saved_credentials(store) is None


def test_valid_token_is_used_without_browser(paths, tmp_path):
    store = TokenStore(tmp_path / "token.bin")
    store.save(token_json(expired=False))
    creds = obtain_credentials(paths, store, interactive=False)
    assert creds.valid


def test_expired_refresh_token_is_deleted_and_requires_sign_in(paths, tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    def refresh_fails(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", refresh_fails)
    store = TokenStore(tmp_path / "token.bin")
    store.save(token_json(expired=True))
    with pytest.raises(GmailAuthError, match="Connect Gmail"):
        obtain_credentials(paths, store, interactive=False)
    assert not store.exists()


def test_refreshed_token_is_saved(paths, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from google.oauth2.credentials import Credentials

    def refresh_ok(self, request):
        self.token = "ya29.new-token"
        # google-auth compares expiry as a naive UTC datetime
        self.expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)

    monkeypatch.setattr(Credentials, "refresh", refresh_ok)
    store = TokenStore(tmp_path / "token.bin")
    store.save(token_json(expired=True))
    creds = obtain_credentials(paths, store, interactive=False)
    assert creds.token == "ya29.new-token"
    assert json.loads(store.load())["token"] == "ya29.new-token"


def test_interactive_sign_in_requires_client_file(paths, tmp_path):
    with pytest.raises(CredentialsFileError):
        obtain_credentials(paths, TokenStore(tmp_path / "t.bin"), interactive=True)


def test_redaction_of_oauth_secrets():
    text = ('Authorization: Bearer ya29.a0AfH6SMBx-secret refresh_token=1//0gSecret '
            '{"client_secret": "GOCSPX-abcdef", "access_token": "xyz"} '
            'http://localhost:8080/?state=abc&code=4/0AX4XfWsecret&scope=gmail')
    cleaned = redact(text)
    for secret in ("ya29.a0AfH6SMBx-secret", "1//0gSecret", "GOCSPX-abcdef", "4/0AX4XfWsecret", '"xyz"'):
        assert secret not in cleaned
    assert "HTTP status code=404" == redact("HTTP status code=404")  # no over-redaction


def test_log_filter_redacts_records():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "token %s", ("ya29.secret-value",), None)
    SecretRedactingFilter().filter(record)
    assert "secret-value" not in record.getMessage()
