"""
Test doubles: an in-memory Gmail backend that mimics the googleapiclient
resource interface (service.users().drafts().get(...).execute()) and Gmail's
draft semantics:

* drafts keep their id; every update creates a NEW message id
* format=full returns a Gmail-style payload tree (attachments carry an
  attachmentId instead of inline data, text parts carry base64url data)
* format=raw returns the exact stored RFC 822 bytes
* drafts.update accepts a message/rfc822 media upload (as the real client
  sends it) or a 'raw' field

drafts.send removes the draft and records it in backend.sent.  Any access
to create / delete / messages() is recorded and raises, so a test fails
immediately if the application ever tries it.
"""

from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import itertools
import json
import time
from typing import Any, Callable

import httplib2
from googleapiclient.errors import HttpError


class ForbiddenCall(AssertionError):
    pass


def http_error(status: int, reason: str = "error", message: str = "Error") -> HttpError:
    content = json.dumps({"error": {"code": status, "message": message,
                                    "errors": [{"reason": reason, "message": message}]}}).encode()
    return HttpError(httplib2.Response({"status": status}), content, uri="https://gmail.googleapis.com/fake")


class FakeGmailBackend:
    def __init__(self, account: str = "amine.test@gmail.com", page_size: int = 2):
        self.account = account
        self.page_size = page_size
        self.drafts: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.forbidden: list[str] = []
        self.fail_before: dict[str, list[BaseException]] = {}
        self.fail_after: dict[str, list[BaseException]] = {}
        self.on_get_raw: Callable[[str], None] | None = None
        self.sent: list[dict[str, Any]] = []  # drafts that were sent
        self._ids = itertools.count(1)

    # -- setup helpers ----------------------------------------------------
    def add_draft(self, raw: bytes, *, draft_id: str | None = None, thread_id: str | None = None,
                  internal_date_ms: int | None = None) -> str:
        n = next(self._ids)
        draft_id = draft_id or f"r-{n:04d}"
        self.drafts[draft_id] = {
            "message_id": f"m-{n:04d}",
            "thread_id": thread_id or f"t-{n:04d}",
            "raw": raw,
            "internal_date": internal_date_ms or int(time.time() * 1000) - n * 1000,
        }
        self.order.insert(0, draft_id)  # Gmail lists newest first
        return draft_id

    def edit_draft_externally(self, draft_id: str, raw: bytes) -> None:
        """Simulate the user editing the draft in the Gmail web UI."""
        n = next(self._ids)
        self.drafts[draft_id]["raw"] = raw
        self.drafts[draft_id]["message_id"] = f"m-{n:04d}"

    def raw_of(self, draft_id: str) -> bytes:
        return self.drafts[draft_id]["raw"]

    def method_calls(self, name: str) -> list[dict]:
        return [kw for method, kw in self.calls if method == name]

    # -- behaviour --------------------------------------------------------
    def _maybe_fail(self, table: dict[str, list[BaseException]], method: str) -> None:
        queue = table.get(method)
        if queue:
            raise queue.pop(0)

    def _not_found(self) -> HttpError:
        return http_error(404, "notFound", "Requested entity was not found.")

    def draft_resource(self, draft_id: str, fmt: str) -> dict:
        if draft_id not in self.drafts:
            raise self._not_found()
        d = self.drafts[draft_id]
        message: dict[str, Any] = {
            "id": d["message_id"], "threadId": d["thread_id"], "labelIds": ["DRAFT"],
            "internalDate": str(d["internal_date"]), "sizeEstimate": len(d["raw"]),
            "historyId": "1234", "snippet": "",
        }
        if fmt == "raw":
            if self.on_get_raw:
                self.on_get_raw(draft_id)
            message["raw"] = base64.urlsafe_b64encode(d["raw"]).decode("ascii")
        elif fmt in ("full", "metadata"):
            msg = email.message_from_bytes(d["raw"], policy=email.policy.default)
            payload = _payload(msg, "")
            if fmt == "metadata":
                payload.pop("parts", None)
                payload["body"] = {"size": 0}
            message["payload"] = payload
        return {"id": draft_id, "message": message}

    def update(self, draft_id: str, body: dict | None, media_body: Any) -> dict:
        if draft_id not in self.drafts:
            raise self._not_found()
        if media_body is not None:
            raw = media_body.getbytes(0, media_body.size())
            assert media_body.mimetype() == "message/rfc822"
        else:
            raw = base64.urlsafe_b64decode((body or {})["message"]["raw"])
        email.message_from_bytes(raw, policy=email.policy.default)  # must parse
        n = next(self._ids)
        d = self.drafts[draft_id]
        d["raw"] = raw
        d["message_id"] = f"m-{n:04d}"
        d["internal_date"] = int(time.time() * 1000)
        thread = ((body or {}).get("message") or {}).get("threadId")
        if thread:
            d["thread_id"] = thread
        return {"id": draft_id, "message": {"id": d["message_id"], "threadId": d["thread_id"],
                                            "labelIds": ["DRAFT"]}}


    def send(self, body: dict) -> dict:
        draft_id = (body or {}).get("id")
        if draft_id not in self.drafts:
            raise self._not_found()
        d = self.drafts.pop(draft_id)
        self.order.remove(draft_id)
        n = next(self._ids)
        self.sent.append({"draft_id": draft_id, "raw": d["raw"], "message_id": f"s-{n:04d}"})
        return {"id": f"s-{n:04d}", "threadId": d["thread_id"], "labelIds": ["SENT"]}


def _payload(part: email.message.EmailMessage, part_id: str) -> dict:
    headers = [{"name": k, "value": str(v)} for k, v in part.items()]
    mime = part.get_content_type()
    filename = part.get_filename() or ""
    result: dict[str, Any] = {"partId": part_id, "mimeType": mime, "filename": filename,
                              "headers": headers, "body": {"size": 0}}
    if part.is_multipart():
        result["parts"] = [
            _payload(child, f"{part_id}.{i}" if part_id else str(i))
            for i, child in enumerate(part.get_payload())
        ]
        return result
    data = part.get_payload(decode=True) or b""
    result["body"]["size"] = len(data)
    if filename or part.get_content_disposition() == "attachment" or not mime.startswith("text/"):
        result["body"]["attachmentId"] = "ANGjdJ_" + hashlib.sha1(data).hexdigest()
    else:
        # Gmail returns base64url; strip padding to exercise the decoder.
        result["body"]["data"] = base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
    return result


class _Request:
    def __init__(self, backend: FakeGmailBackend, method: str, kwargs: dict, fn: Callable[[], Any]):
        self.backend = backend
        self.method = method
        self.kwargs = kwargs
        self.fn = fn

    def execute(self, num_retries: int = 0) -> Any:
        self.backend.calls.append((self.method, self.kwargs))
        self.backend._maybe_fail(self.backend.fail_before, self.method)
        result = self.fn()
        self.backend._maybe_fail(self.backend.fail_after, self.method)
        return result


class _Forbidden:
    def _forbid(self, name: str) -> Callable[..., Any]:
        def call(*_args: Any, **_kwargs: Any) -> Any:
            self.backend.forbidden.append(name)
            raise ForbiddenCall(f"{name} must never be called")
        return call


class _Drafts(_Forbidden):
    def __init__(self, backend: FakeGmailBackend):
        self.backend = backend

    def list(self, userId: str, maxResults: int = 100, pageToken: str | None = None, **kw: Any) -> _Request:
        assert userId == "me"
        page = min(self.backend.page_size, maxResults)

        def fn() -> dict:
            start = int(pageToken or 0)
            ids = self.backend.order[start:start + page]
            response: dict[str, Any] = {
                "drafts": [{"id": i, "message": {"id": self.backend.drafts[i]["message_id"],
                                                 "threadId": self.backend.drafts[i]["thread_id"]}} for i in ids],
                "resultSizeEstimate": len(self.backend.order),
            }
            if start + page < len(self.backend.order):
                response["nextPageToken"] = str(start + page)
            return response

        return _Request(self.backend, "drafts.list", {"pageToken": pageToken, "maxResults": maxResults}, fn)

    def list_next(self, previous_request: _Request, previous_response: dict) -> _Request | None:
        token = previous_response.get("nextPageToken")
        if not token:
            return None
        return self.list(userId="me", maxResults=previous_request.kwargs["maxResults"], pageToken=token)

    def get(self, userId: str, id: str, format: str = "full") -> _Request:
        assert userId == "me"
        return _Request(self.backend, "drafts.get", {"id": id, "format": format},
                        lambda: self.backend.draft_resource(id, format))

    def update(self, userId: str, id: str, body: dict | None = None, media_body: Any = None) -> _Request:
        assert userId == "me"
        return _Request(self.backend, "drafts.update", {"id": id, "body": body, "media_body": media_body},
                        lambda: self.backend.update(id, body, media_body))

    def send(self, userId: str, body: dict) -> _Request:
        assert userId == "me"
        return _Request(self.backend, "drafts.send", {"body": body}, lambda: self.backend.send(body))

    def __getattr__(self, name: str) -> Any:
        if name in ("create", "delete"):
            return self._forbid(f"drafts.{name}")
        raise AttributeError(name)


class _Users(_Forbidden):
    def __init__(self, backend: FakeGmailBackend):
        self.backend = backend

    def getProfile(self, userId: str) -> _Request:
        return _Request(self.backend, "getProfile", {}, lambda: {"emailAddress": self.backend.account})

    def drafts(self) -> _Drafts:
        return _Drafts(self.backend)

    def __getattr__(self, name: str) -> Any:
        if name in ("messages", "threads", "settings", "labels", "history"):
            return self._forbid(f"users.{name}")
        raise AttributeError(name)


class FakeGmailService:
    def __init__(self, backend: FakeGmailBackend):
        self.backend = backend

    def users(self) -> _Users:
        return _Users(self.backend)
