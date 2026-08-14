"""
Secret-at-rest protection for PIIPS.

Sensitive configuration values (currently the SQL Server connection string,
which contains a password) are stored on disk protected with the Windows
Data Protection API (DPAPI) instead of plaintext. DPAPI is used at
LOCAL_MACHINE scope, so any account on this server — including the IIS
application-pool / service account that runs the app — can decrypt it, while
the value on disk is opaque and cannot be moved to another machine and read.

No third-party package is required: DPAPI is called through crypt32.dll via
ctypes.

Stored form:  "enc:dpapi:<base64 of the DPAPI blob>"
A value without that prefix is treated as legacy plaintext (and gets
protected the next time it is written).

NOTE: user passwords are NOT handled here. They are stored as one-way salted
PBKDF2-SHA256 hashes (see database.hash_password), which is stronger than
reversible encryption and must not be turned into a decryptable form.
"""

import base64
import ctypes
from ctypes import wintypes


MARKER = "enc:dpapi:"

_CRYPTPROTECT_LOCAL_MACHINE = 0x4


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _protect_raw(text: str) -> bytes:
    src = _blob(text.encode("utf-8"))
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(src), None, None, None, None,
        _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return _blob_bytes(out)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _unprotect_raw(raw: bytes) -> str:
    src = _blob(raw)
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None,
        _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return _blob_bytes(out).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def is_protected(value) -> bool:
    """True if `value` is an already-protected string."""
    return isinstance(value, str) and value.startswith(MARKER)


def protect(text):
    """Return the protected (encrypted) form of `text`. Empty/None and
    already-protected values are returned unchanged."""
    if not text or is_protected(text):
        return text
    return MARKER + base64.b64encode(_protect_raw(text)).decode("ascii")


def unprotect(value):
    """Return the plaintext for a protected value; pass through anything that
    isn't protected (legacy plaintext / empty)."""
    if not is_protected(value):
        return value
    raw = base64.b64decode(value[len(MARKER):])
    return _unprotect_raw(raw)
