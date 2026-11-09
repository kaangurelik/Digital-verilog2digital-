"""Read bounded HTTPS ranges through Windows WinHTTP.

Enable HTTP/2 and query the negotiated protocol. The caller verifies the final
artifact and manages its files.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from functools import lru_cache
import math
import os
import time
from urllib.parse import urljoin, urlsplit


BUFFER_BYTES = 256 * 1024
MAX_REDIRECTS = 5


@lru_cache(maxsize=1)
def _winhttp():
    if os.name != "nt":
        raise OSError("WinHTTP transport requires Windows")
    api = ctypes.WinDLL("winhttp", use_last_error=True)
    handle = w.HANDLE
    signatures = {
        "WinHttpOpen": ([w.LPCWSTR, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD], handle),
        "WinHttpConnect": ([handle, w.LPCWSTR, w.WORD, w.DWORD], handle),
        "WinHttpOpenRequest": ([handle, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD], handle),
        "WinHttpSetOption": ([handle, w.DWORD, ctypes.c_void_p, w.DWORD], w.BOOL),
        "WinHttpQueryOption": ([handle, w.DWORD, ctypes.c_void_p, ctypes.POINTER(w.DWORD)], w.BOOL),
        "WinHttpSetTimeouts": ([handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int], w.BOOL),
        "WinHttpSendRequest": ([handle, w.LPCWSTR, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_size_t], w.BOOL),
        "WinHttpReceiveResponse": ([handle, ctypes.c_void_p], w.BOOL),
        "WinHttpQueryHeaders": ([handle, w.DWORD, w.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD)], w.BOOL),
        "WinHttpReadData": ([handle, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
        "WinHttpCloseHandle": ([handle], w.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes = arguments
        function.restype = result
    return api


def _checked(value, operation):
    if not value:
        # Native error numbers and operation names never contain signed URLs.
        raise OSError(ctypes.get_last_error(), operation + " failed")
    return value


def _check(deadline, stop):
    if stop.is_set():
        raise TimeoutError("Range download cancelled")
    if time.monotonic() >= deadline:
        raise TimeoutError("Range download deadline exceeded")


def _timeouts(api, target, deadline, stop):
    _check(deadline, stop)
    milliseconds = max(1, min(10000, math.ceil((deadline-time.monotonic()) * 1000)))
    _checked(api.WinHttpSetTimeouts(target, min(5000, milliseconds), milliseconds, milliseconds, milliseconds),
             "WinHttpSetTimeouts")


def _option(api, target, number, value):
    value = w.DWORD(value)
    _checked(api.WinHttpSetOption(target, number, ctypes.byref(value), ctypes.sizeof(value)), "WinHttpSetOption")


def _header(api, target, number):
    buffer = ctypes.create_unicode_buffer(16384)
    size = w.DWORD(ctypes.sizeof(buffer))
    _checked(api.WinHttpQueryHeaders(target, number, None, buffer, ctypes.byref(size), None), "WinHttpQueryHeaders")
    return buffer.value


def _endpoint(url):
    try:
        if not isinstance(url, str) or len(url) > 16384 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
            raise ValueError
        parts = urlsplit(url)
        if parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii")
        port = 443 if parts.port is None else parts.port
        if not 1 <= port <= 65535:
            raise ValueError
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        path.encode("utf-8", errors="strict")
        return host, port, path
    except (ValueError, UnicodeError):
        raise ValueError("Range download requires a bounded HTTPS URL without credentials") from None


def download_range(url, first, last, total, writer, *, deadline, stop) -> dict:
    """Write the requested range at the writer's current position.

    The caller handles seeking, flushing, closing and retries after partial
    writes. Check cancellation and deadlines around I/O; native calls have
    timeouts of at most 10 seconds.
    """
    if any(type(n) is not int for n in (first, last, total)) or not 0 <= first <= last < total <= 2**63-1:
        raise ValueError("Invalid byte range or artifact size")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("Range deadline must be a finite monotonic timestamp")
    if not callable(getattr(stop, "is_set", None)) or not callable(getattr(writer, "write", None)):
        raise ValueError("Range download requires a stop event and binary writer")
    _endpoint(url)
    _check(deadline, stop)
    api = _winhttp()
    started = time.monotonic()
    session = _checked(api.WinHttpOpen("verilog2digital-pinned-range/1", 4, None, None, 0), "WinHttpOpen")
    try:
        _timeouts(api, session, deadline, stop)
        _option(api, session, 133, 1)  # ENABLE_HTTP_PROTOCOL / HTTP2.
        _option(api, session, 84, 0x2800)  # TLS1.2/1.3; normal certificate validation.
        for redirect in range(MAX_REDIRECTS+1):
            _check(deadline, stop)
            host, port, path = _endpoint(url)
            _timeouts(api, session, deadline, stop)
            connection = _checked(api.WinHttpConnect(session, host, port, 0), "WinHttpConnect")
            request = None
            try:
                request = _checked(api.WinHttpOpenRequest(connection, "GET", path, None, None, None, 0x00800000), "WinHttpOpenRequest")
                # Handle redirects explicitly so every hop checks TLS and the
                # deadline. Never send cookies or automatic authentication.
                _option(api, request, 63, 0x7)
                headers = f"Range: bytes={first}-{last}\r\nAccept-Encoding: identity\r\n"
                _timeouts(api, request, deadline, stop)
                _checked(api.WinHttpSendRequest(request, headers, len(headers), None, 0, 0, 0), "WinHttpSendRequest")
                _timeouts(api, request, deadline, stop)
                _checked(api.WinHttpReceiveResponse(request, None), "WinHttpReceiveResponse")
                _check(deadline, stop)
                status = int(_header(api, request, 19))
                if status in {301, 302, 303, 307, 308}:
                    if redirect == MAX_REDIRECTS:
                        raise OSError("Range download redirect limit exceeded")
                    destination = urljoin(url, _header(api, request, 33))
                    _endpoint(destination)
                    url = destination
                    continue
                if status != 206:
                    raise OSError(f"Range server returned HTTP {status}; required 206")
                if _header(api, request, 53).strip() != f"bytes {first}-{last}/{total}":
                    raise OSError("Range server returned an unexpected Content-Range")
                protocol, size = w.DWORD(), w.DWORD(ctypes.sizeof(w.DWORD))
                _checked(api.WinHttpQueryOption(request, 134, ctypes.byref(protocol), ctypes.byref(size)), "WinHttpQueryOption")
                body_started = time.monotonic()
                buffer = ctypes.create_string_buffer(BUFFER_BYTES)
                remaining, transferred = last-first+1, 0
                while remaining:
                    _timeouts(api, request, deadline, stop)
                    count = w.DWORD()
                    _checked(api.WinHttpReadData(request, buffer, min(BUFFER_BYTES, remaining), ctypes.byref(count)), "WinHttpReadData")
                    _check(deadline, stop)
                    if not 0 < count.value <= min(BUFFER_BYTES, remaining):
                        raise OSError("Range body ended early or exceeded its requested read size")
                    block = buffer.raw[:count.value]
                    if writer.write(block) != len(block):
                        raise OSError("Range writer did not consume the complete block")
                    remaining -= count.value
                    transferred += count.value
                _check(deadline, stop)
                return {"http_protocol": "HTTP/2" if protocol.value & 1 else "HTTP/1.x",
                        "header_seconds": round(body_started-started, 6),
                        "body_seconds": round(time.monotonic()-body_started, 6), "bytes": transferred}
            finally:
                if request:
                    api.WinHttpCloseHandle(request)
                api.WinHttpCloseHandle(connection)
    finally:
        api.WinHttpCloseHandle(session)
