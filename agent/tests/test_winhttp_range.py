import ctypes
from ctypes import wintypes as w
import io
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import winhttp_range as transport


URL = "https://modelscope.cn/models/Qwen/test/resolve/pinned/model.gguf?probe=example"


def number(pointer, value):
    ctypes.cast(pointer, ctypes.POINTER(w.DWORD)).contents.value = value


class Native:
    def __init__(self, responses=None, protocol=1, stop=None, fail_read=False):
        self.responses = responses or [{19: "206", 53: "bytes 2-9/12", "body": b"abcdefgh"}]
        self.protocol, self.stop, self.fail_read = protocol, stop, fail_read
        self.opened, self.closed, self.timeouts, self.headers, self.options = [], [], [], [], []
        self.request_index, self.offset = -1, 0

    def open(self):
        self.opened.append(len(self.opened)+1)
        return self.opened[-1]

    def WinHttpOpen(self, *args): return self.open()
    def WinHttpConnect(self, *args): return self.open()
    def WinHttpOpenRequest(self, *args):
        self.request_index += 1
        self.offset = 0
        return self.open()

    def WinHttpSetOption(self, handle, option, value, size):
        self.options.append((option, ctypes.cast(value, ctypes.POINTER(w.DWORD)).contents.value))
        return 1

    def WinHttpSetTimeouts(self, handle, *values):
        self.timeouts.extend(values)
        return 1

    def WinHttpSendRequest(self, request, headers, *args):
        self.headers.append(headers)
        return 1

    def WinHttpReceiveResponse(self, *args): return 1
    def WinHttpQueryHeaders(self, request, key, name, buffer, size, index):
        buffer.value = self.responses[self.request_index][key]
        return 1

    def WinHttpQueryOption(self, request, key, result, size):
        number(result, self.protocol)
        return 1

    def WinHttpReadData(self, request, buffer, size, count):
        if self.fail_read:
            ctypes.set_last_error(5)
            return 0
        body = self.responses[self.request_index]["body"]
        data = body[self.offset:self.offset+min(size, 3)]
        self.offset += len(data)
        ctypes.memmove(buffer, data, len(data))
        number(count, len(data))
        if self.stop:
            self.stop.set()
        return 1

    def WinHttpCloseHandle(self, handle):
        self.closed.append(handle)
        return 1


class Tests(unittest.TestCase):
    def run_range(self, native, writer=None, stop=None):
        if writer is None:
            writer = io.BytesIO(b"00XXXXXXXXtail")
            writer.seek(2)
        with patch.object(transport, "_winhttp", return_value=native):
            result = transport.download_range(URL, 2, 9, 12, writer, deadline=time.monotonic()+30, stop=stop or threading.Event())
        self.assertEqual(set(native.closed), set(native.opened))
        return result, writer

    def test_success_preserves_writer_position_and_records_actual_protocol(self):
        for flag, protocol in [(1, "HTTP/2"), (0, "HTTP/1.x")]:
            native = Native(protocol=flag)
            result, writer = self.run_range(native)
            self.assertEqual(writer.getvalue(), b"00abcdefghtail")
            self.assertEqual(result["bytes"], 8)
            self.assertEqual(result["http_protocol"], protocol)
            self.assertTrue(all(0 < value <= 10000 for value in native.timeouts))
            self.assertIn((63, 7), native.options)
            self.assertIn((84, 0x2800), native.options)

    def test_redirect_preserves_range(self):
        native = Native([{19: "302", 33: "https://cdn-lfs-cn-1.modelscope.cn/file?secret=not-logged"},
                         {19: "206", 53: "bytes 2-9/12", "body": b"abcdefgh"}])
        self.run_range(native)
        self.assertEqual(len(native.headers), 2)
        self.assertTrue(all("Range: bytes=2-9\r\n" in value for value in native.headers))

    def test_http_redirect_rejected_before_body(self):
        native = Native([{19: "302", 33: "http://example.com/private?secret=not-logged"}])
        with self.assertRaises(ValueError) as error:
            self.run_range(native)
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(set(native.closed), set(native.opened))

    def test_wrong_status_or_range_never_writes(self):
        for response in [{19: "200"}, {19: "206", 53: "bytes 0-7/12"}]:
            native, writer = Native([response]), io.BytesIO()
            with self.assertRaises(OSError): self.run_range(native, writer)
            self.assertEqual(writer.getvalue(), b"")
            self.assertEqual(set(native.closed), set(native.opened))

    def test_cancel_before_native_work(self):
        stop = threading.Event(); stop.set()
        native = Native()
        with self.assertRaises(TimeoutError): self.run_range(native, stop=stop)
        self.assertEqual(native.opened, [])

    def test_cancel_during_read_closes_every_handle(self):
        stop = threading.Event(); native = Native(stop=stop); writer = io.BytesIO()
        with self.assertRaises(TimeoutError): self.run_range(native, writer, stop)
        self.assertEqual(writer.getvalue(), b"")
        self.assertEqual(set(native.closed), set(native.opened))

    def test_native_failure_closes_every_handle(self):
        native = Native(fail_read=True)
        with self.assertRaises(OSError): self.run_range(native)
        self.assertEqual(set(native.closed), set(native.opened))

    def test_short_body_and_short_writer_fail(self):
        native = Native([{19: "206", 53: "bytes 2-9/12", "body": b"ab"}])
        with self.assertRaises(OSError): self.run_range(native)
        self.assertEqual(set(native.closed), set(native.opened))
        class Short:
            def write(self, block): return len(block)-1
        native = Native()
        with self.assertRaises(OSError): self.run_range(native, Short())
        self.assertEqual(set(native.closed), set(native.opened))

    def test_bad_deadline_or_url_or_range_never_opens_native(self):
        for values in [(URL, 2, 9, 12, time.monotonic()-1), (URL, True, 9, 12, time.monotonic()+30),
                       ("http://example.com", 2, 9, 12, time.monotonic()+30),
                       ("https://modelscope.cn:0/model.gguf", 2, 9, 12, time.monotonic()+30)]:
            with patch.object(transport, "_winhttp", side_effect=AssertionError("Native work was reached")):
                with self.assertRaises((ValueError, TimeoutError)):
                    transport.download_range(*values[:4], io.BytesIO(), deadline=values[4], stop=threading.Event())



