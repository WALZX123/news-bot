"""User-scoped Windows DPAPI storage for model API keys."""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

_REFERENCES = re.compile(r"[0-9a-f]{32}")
_DEFAULT_ENDPOINT = "https://api.deepseek.com"


class _DataBlob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.c_void_p)]


def _protect(data, *, encrypt):
    if os.name != "nt":
        raise OSError("Windows vault is unavailable")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    arguments = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                 ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    operation = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    operation.argtypes = arguments
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.addressof(buffer))
    result = _DataBlob()
    try:
        if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(result.data, result.length)
    finally:
        ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))
        if result.data:
            ctypes.memset(result.data, 0, result.length)
            kernel.LocalFree(result.data)


def vault_file(reference):
    if not isinstance(reference, str) or not _REFERENCES.fullmatch(reference):
        raise ValueError("Invalid vault reference")
    location = os.environ.get("LOCALAPPDATA")
    if not location:
        raise OSError("Windows user data directory unavailable")
    return Path(location) / "NewsBot" / "secrets" / f"{reference}.dpapi"


def _origin(base_url):
    parsed = urlsplit(base_url or _DEFAULT_ENDPOINT)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid model endpoint")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}:{parsed.hostname.lower()}:{port}"


def store_model_key(key, base_url):
    if not isinstance(key, str) or not 1 <= len(key) <= 4096 or any(c.isspace() for c in key):
        raise ValueError("Invalid model key")
    reference = uuid4().hex
    path = vault_file(reference)
    raw = key.encode("utf-8")
    origin = _origin(base_url)
    # Protect the endpoint and the key together. A modified record cannot move a key to another host.
    sealed = _protect(json.dumps({"origin": origin, "key": key}).encode("utf-8"), encrypt=True)
    record = {"version": 1, "sha256": hashlib.sha256(raw).hexdigest(),
              "dpapi": base64.b64encode(sealed).decode("ascii")}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return reference


def load_model_key(reference, base_url):
    record = json.loads(vault_file(reference).read_text(encoding="utf-8"))
    if record["version"] != 1:
        raise ValueError("Invalid vault record")
    clear = _protect(base64.b64decode(record["dpapi"], validate=True), encrypt=False)
    secret = json.loads(clear)
    key = secret["key"]
    if secret["origin"] != _origin(base_url) or not hmac.compare_digest(
            hashlib.sha256(key.encode("utf-8")).hexdigest(), record["sha256"]):
        raise ValueError("Vault record did not match the configured model endpoint")
    return key
