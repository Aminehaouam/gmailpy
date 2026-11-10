"""
secure_storage.py - Local, secure storage for the Gmail OAuth token.

On Windows the token is encrypted with DPAPI (CryptProtectData) for the
current Windows user account.  Only the same Windows user on the same machine
can decrypt it; copying token.bin to another account or computer is useless.

On other platforms (only used for development/testing) the token is stored
with owner-only file permissions (0600).

The application never asks for or stores the Gmail password - only the OAuth
token issued by Google after the user signs in through the browser.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_MAGIC_DPAPI = b"BAA-DPAPI-1\n"
_MAGIC_PLAIN = b"BAA-PLAIN-1\n"


class SecureStorageError(Exception):
    """The token could not be encrypted, decrypted or written."""


# --------------------------------------------------------------------------
# Windows DPAPI via ctypes (no extra dependency)
# --------------------------------------------------------------------------

def _dpapi_available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),   # pDataIn
        wintypes.LPCWSTR,             # szDataDescr
        ctypes.POINTER(_DATA_BLOB),   # pOptionalEntropy
        ctypes.c_void_p,              # pvReserved
        ctypes.c_void_p,              # pPromptStruct
        wintypes.DWORD,               # dwFlags
        ctypes.POINTER(_DATA_BLOB),   # pDataOut
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),   # pDataIn
        ctypes.c_void_p,              # ppszDataDescr (not needed)
        ctypes.POINTER(_DATA_BLOB),   # pOptionalEntropy
        ctypes.c_void_p,              # pvReserved
        ctypes.c_void_p,              # pPromptStruct
        wintypes.DWORD,               # dwFlags
        ctypes.POINTER(_DATA_BLOB),   # pDataOut
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    _CRYPTPROTECT_UI_FORBIDDEN = 0x01
    _ENTROPY = b"BewerbungAttachAssistant/oauth-token"

    def _blob_from_bytes(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data, len(data))
        blob = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        return blob, buffer  # keep buffer alive while blob is used

    def _bytes_from_blob(blob: _DATA_BLOB) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))

    def _dpapi_encrypt(data: bytes) -> bytes:
        in_blob, _in_buf = _blob_from_bytes(data)
        entropy, _ent_buf = _blob_from_bytes(_ENTROPY)
        out_blob = _DATA_BLOB()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(in_blob), "BewerbungAttachAssistant token", ctypes.byref(entropy),
            None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
        )
        if not ok:
            raise SecureStorageError(f"CryptProtectData failed (error {ctypes.get_last_error()})")
        return _bytes_from_blob(out_blob)

    def _dpapi_decrypt(data: bytes) -> bytes:
        in_blob, _in_buf = _blob_from_bytes(data)
        entropy, _ent_buf = _blob_from_bytes(_ENTROPY)
        out_blob = _DATA_BLOB()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, ctypes.byref(entropy),
            None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
        )
        if not ok:
            raise SecureStorageError(f"CryptUnprotectData failed (error {ctypes.get_last_error()})")
        return _bytes_from_blob(out_blob)

else:

    def _dpapi_encrypt(data: bytes) -> bytes:  # pragma: no cover - never called
        raise SecureStorageError("DPAPI is only available on Windows")

    def _dpapi_decrypt(data: bytes) -> bytes:  # pragma: no cover - never called
        raise SecureStorageError("DPAPI is only available on Windows")


# --------------------------------------------------------------------------
# Token store
# --------------------------------------------------------------------------

class TokenStore:
    """Reads/writes the OAuth token (a JSON string) to an encrypted file."""

    def __init__(self, path: Path):
        self.path = path

    @property
    def encrypted(self) -> bool:
        """True if tokens are encrypted at rest on this platform."""
        return _dpapi_available()

    def exists(self) -> bool:
        return self.path.exists()

    def save(self, token_json: str) -> None:
        data = token_json.encode("utf-8")
        payload = _MAGIC_PLAIN + data
        if _dpapi_available():
            try:
                payload = _MAGIC_DPAPI + _dpapi_encrypt(data)
            except SecureStorageError as exc:  # pragma: no cover - Windows only
                # The data folder lies in the user profile and is only
                # accessible to this Windows user; keep the app usable.
                log.warning("DPAPI encryption failed (%s); storing the token as a "
                            "user-only file instead", exc)
        tmp = self.path.with_suffix(".tmp")
        try:
            # Create the temp file with owner-only permissions from the start.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
            if sys.platform != "win32":
                os.chmod(self.path, 0o600)
        except OSError as exc:
            raise SecureStorageError(f"Could not write token file: {exc}") from exc
        log.info("OAuth token saved (%s)",
                 "DPAPI-encrypted" if payload.startswith(_MAGIC_DPAPI) else "owner-only file")

    def load(self) -> str | None:
        """Return the stored token JSON, or None if there is no usable token."""
        if not self.path.exists():
            return None
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise SecureStorageError(f"Could not read token file: {exc}") from exc
        if payload.startswith(_MAGIC_DPAPI):
            if not _dpapi_available():
                log.warning("Token was encrypted on Windows and cannot be read here")
                return None
            try:
                return _dpapi_decrypt(payload[len(_MAGIC_DPAPI):]).decode("utf-8")
            except SecureStorageError:
                log.warning("Stored token cannot be decrypted (different Windows user?)")
                return None
        if payload.startswith(_MAGIC_PLAIN):
            return payload[len(_MAGIC_PLAIN):].decode("utf-8")
        log.warning("Unrecognised token file format - ignoring it")
        return None

    def delete(self) -> None:
        try:
            self.path.unlink()
            log.info("OAuth token deleted")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SecureStorageError(f"Could not delete token file: {exc}") from exc
