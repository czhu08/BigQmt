# coding: utf-8
"""Same-host shared-memory RPC transport and quote-push channel.

Why shared memory
-----------------
QMT terminals often run with an import whitelist that rejects ``socket`` and
forbid installing packages into the bundled Python.  Redis and ZMQ are then
unavailable; the named-pipe transport already covers that case.  Shared memory
is the other zero-dependency same-host wire: ``mmap`` + ``kernel32`` are both
standard library, no third-party package is imported, and nothing opens a
socket.

Windows only, same host only.  Deployments that need Linux or cross-machine
access keep Redis or ZMQ.

Wire layout
-----------
Each channel is a named pagefile-backed mapping::

    +--------------------------------------------------------------+
    | 64-byte header: magic/version/capacity/head/tail/ready/gen    |
    +--------------------------------------------------------------+
    | length-prefixed frames (4-byte LE length, then UTF-8 payload) |
    | ...                                                          |
    +--------------------------------------------------------------+

The ring is monotonic: ``head`` and ``tail`` are byte offsets that only grow.
``pos = offset % capacity``.  A frame never crosses the wrap boundary; when
the tail of the mapping is too small for the next frame the writer stores a
zero-length skip marker and starts the frame at offset 0.  Readers see the
marker and jump to the next boundary.

RPC uses one request ring (all clients write, the server reads) and one
response ring per client process/transport instance (the server writes, that
client reads).  Per-client response rings are what make multiple client
processes safe: two clients never compete for each other's replies.

Server-to-client push uses a broadcast ring.  Every subscriber keeps its own
local read offset; the writer only advances the global ``tail`` when it must
drop the oldest frames to make room.  A subscriber that falls behind detects
``local < tail`` and resumes at the oldest retained frame.
"""

import collections
import ctypes
import json
import mmap
import os
import queue
import struct
import threading
import time
import uuid

from .base import RpcTransport, TransportError, TransportTimeout


_MAGIC = b"BQSHM001"
_VERSION = 1
_HEADER_BYTES = 64

_OFF_MAGIC = 0
_OFF_VERSION = 8
_OFF_HEADER = 12
_OFF_CAPACITY = 16
_OFF_HEAD = 24
_OFF_TAIL = 32
_OFF_READY = 40
_OFF_FLAGS = 44
_OFF_GENERATION = 48
_OFF_PID = 56

_PAGE_READWRITE = 0x04
_FILE_MAP_ALL_ACCESS = 0x000F001F
_ERROR_ALREADY_EXISTS = 183
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_WAIT_TIMEOUT = 0x102
_INFINITE = 0xFFFFFFFF

_MIN_RING_BYTES = 4096
_MAX_RING_BYTES = 512 * 1024 * 1024
_DEFAULT_REQUEST_BYTES = 8 * 1024 * 1024
_DEFAULT_RESPONSE_BYTES = 16 * 1024 * 1024
_DEFAULT_PUSH_BYTES = 64 * 1024 * 1024
_CONNECT_POLL_SECONDS = 0.005

_SERVERS_LOCK = threading.RLock()
_SERVERS_BY_NAME = {}
_PUBLISHERS_LOCK = threading.RLock()
_PUBLISHERS_BY_NAME = {}


def _monotonic():
    try:
        return time.monotonic()
    except AttributeError:  # pragma: no cover - Python 3.6 still has it
        return time.time()


def _safe_name(value):
    text = str(value or "")
    out = []
    for char in text:
        if char.isalnum() or char in ("_", "-"):
            out.append(char)
        else:
            out.append("_")
    return "".join(out) or "default"


def shm_base_name(base_name="bigqmt_shm", account_id=""):
    """Stable base for every mapping belonging to one account."""
    return "%s_%s" % (_safe_name(base_name or "bigqmt_shm"),
                      _safe_name(account_id or "default"))


def shm_rpc_name(base_name="bigqmt_shm", account_id=""):
    return shm_base_name(base_name, account_id) + "_rpc"


def shm_push_name(base_name="bigqmt_shm", account_id=""):
    return shm_base_name(base_name, account_id) + "_push"


def _kernel32():
    if os.name != "nt":
        raise TransportError(
            "shared-memory transport is Windows-only (os.name=%r). Use redis "
            "or zmq for Linux/macOS deployments." % os.name)
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateFileMappingW.restype = ctypes.c_void_p
    dll.CreateFileMappingW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_wchar_p]
    dll.OpenFileMappingW.restype = ctypes.c_void_p
    dll.OpenFileMappingW.argtypes = [
        ctypes.c_uint32, ctypes.c_int32, ctypes.c_wchar_p]
    dll.CreateMutexW.restype = ctypes.c_void_p
    dll.CreateMutexW.argtypes = [
        ctypes.c_void_p, ctypes.c_int32, ctypes.c_wchar_p]
    dll.WaitForSingleObject.restype = ctypes.c_uint32
    dll.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    dll.ReleaseMutex.restype = ctypes.c_int32
    dll.ReleaseMutex.argtypes = [ctypes.c_void_p]
    dll.CreateEventW.restype = ctypes.c_void_p
    dll.CreateEventW.argtypes = [
        ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32, ctypes.c_wchar_p]
    dll.SetEvent.restype = ctypes.c_int32
    dll.SetEvent.argtypes = [ctypes.c_void_p]
    dll.ResetEvent.restype = ctypes.c_int32
    dll.ResetEvent.argtypes = [ctypes.c_void_p]
    dll.CloseHandle.restype = ctypes.c_int32
    dll.CloseHandle.argtypes = [ctypes.c_void_p]
    return dll


def _last_error():
    return ctypes.get_last_error()


def _create_mapping(name, size):
    """Create-or-open a named mapping. Returns ``(mmap, kernel_handle, existed)``."""
    dll = _kernel32()
    high = (int(size) >> 32) & 0xFFFFFFFF
    low = int(size) & 0xFFFFFFFF
    handle = dll.CreateFileMappingW(
        ctypes.c_void_p(_INVALID_HANDLE_VALUE), None, _PAGE_READWRITE,
        high, low, name)
    if not handle:
        raise TransportError(
            "CreateFileMappingW(%r, %d) failed (err=%s)"
            % (name, int(size), _last_error()))
    existed = _last_error() == _ERROR_ALREADY_EXISTS
    try:
        view = mmap.mmap(-1, int(size), tagname=name)
    except Exception as exc:
        try:
            dll.CloseHandle(handle)
        except Exception:
            pass
        raise TransportError(
            "cannot map shared memory %r (%d bytes): %s. The server and "
            "client must use the same ring size." % (name, int(size), exc))
    return view, handle, existed


def _try_open_mapping(name, size):
    """Open an existing mapping, or return None. Never creates one silently."""
    dll = _kernel32()
    probe = dll.OpenFileMappingW(_FILE_MAP_ALL_ACCESS, False, name)
    if not probe:
        return None
    try:
        dll.CloseHandle(probe)
    except Exception:
        pass
    try:
        return mmap.mmap(-1, int(size), tagname=name)
    except Exception:
        return None


def _close_mapping(view, handle=None):
    if view is not None:
        try:
            view.close()
        except Exception:
            pass
    if handle:
        try:
            _kernel32().CloseHandle(handle)
        except Exception:
            pass


class _NamedMutex(object):
    def __init__(self, name):
        self.name = str(name)
        self.handle = _kernel32().CreateMutexW(None, False, self.name)
        if not self.handle:
            raise TransportError(
                "CreateMutexW(%r) failed (err=%s)" % (self.name, _last_error()))
        self._dll = _kernel32()

    def acquire(self, timeout_seconds):
        if timeout_seconds is None:
            milliseconds = _INFINITE
        else:
            milliseconds = int(max(0.0, float(timeout_seconds)) * 1000.0)
            if milliseconds == 0 and timeout_seconds > 0:
                milliseconds = 1
        result = self._dll.WaitForSingleObject(self.handle, milliseconds)
        if result in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            return True
        if result == _WAIT_TIMEOUT:
            return False
        raise TransportError(
            "WaitForSingleObject(%r) failed (result=%s, err=%s)"
            % (self.name, result, _last_error()))

    def release(self):
        try:
            self._dll.ReleaseMutex(self.handle)
        except Exception:
            pass

    def __enter__(self):
        self.acquire(None)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    def close(self):
        handle, self.handle = self.handle, None
        if handle:
            try:
                self._dll.CloseHandle(handle)
            except Exception:
                pass


class _NamedEvent(object):
    """Auto-reset event. Multiple waiters are okay because callers also poll."""

    def __init__(self, name):
        self.name = str(name)
        self.handle = _kernel32().CreateEventW(None, False, False, self.name)
        if not self.handle:
            raise TransportError(
                "CreateEventW(%r) failed (err=%s)" % (self.name, _last_error()))
        self._dll = _kernel32()

    def set(self):
        if self.handle:
            try:
                self._dll.SetEvent(self.handle)
            except Exception:
                pass

    def reset(self):
        if self.handle:
            try:
                self._dll.ResetEvent(self.handle)
            except Exception:
                pass

    def wait(self, timeout_seconds):
        if not self.handle:
            return False
        if timeout_seconds is None:
            milliseconds = _INFINITE
        else:
            milliseconds = int(max(0.0, float(timeout_seconds)) * 1000.0)
        result = self._dll.WaitForSingleObject(self.handle, milliseconds)
        return result in (_WAIT_OBJECT_0, _WAIT_ABANDONED)

    def close(self):
        handle, self.handle = self.handle, None
        if handle:
            try:
                self._dll.CloseHandle(handle)
            except Exception:
                pass


class _ByteRing(object):
    """Length-prefixed byte ring over one named mapping."""

    def __init__(self, view, mapping_bytes):
        self.view = view
        self.mapping_bytes = int(mapping_bytes)
        if self.mapping_bytes <= _HEADER_BYTES + 4:
            raise ValueError("shared-memory ring is too small")
        # ``capacity`` is the payload area, excluding the 64-byte header.
        self.capacity = self.mapping_bytes - _HEADER_BYTES

    # -- header helpers ---------------------------------------------------
    def _u32(self, offset):
        return struct.unpack_from("<I", self.view, offset)[0]

    def _u64(self, offset):
        return struct.unpack_from("<Q", self.view, offset)[0]

    def _set_u32(self, offset, value):
        struct.pack_into("<I", self.view, offset, int(value) & 0xFFFFFFFF)

    def _set_u64(self, offset, value):
        struct.pack_into("<Q", self.view, offset, int(value) & 0xFFFFFFFFFFFFFFFF)

    def initialize(self):
        generation = uuid.uuid4().int & 0xFFFFFFFFFFFFFFFF
        self.view[0:_HEADER_BYTES] = b"\x00" * _HEADER_BYTES
        self.view[_OFF_MAGIC:_OFF_MAGIC + 8] = _MAGIC
        self._set_u32(_OFF_VERSION, _VERSION)
        self._set_u32(_OFF_HEADER, _HEADER_BYTES)
        self._set_u64(_OFF_CAPACITY, self.capacity)
        self._set_u64(_OFF_HEAD, 0)
        self._set_u64(_OFF_TAIL, 0)
        self._set_u32(_OFF_READY, 0)
        self._set_u32(_OFF_FLAGS, 0)
        self._set_u64(_OFF_GENERATION, generation)
        self._set_u32(_OFF_PID, os.getpid() & 0xFFFFFFFF)
        return generation

    def validate(self, capacity=None):
        try:
            if bytes(self.view[_OFF_MAGIC:_OFF_MAGIC + 8]) != _MAGIC:
                return False
            if self._u32(_OFF_VERSION) != _VERSION:
                return False
            if self._u32(_OFF_HEADER) != _HEADER_BYTES:
                return False
            if self._u64(_OFF_CAPACITY) != self.capacity:
                return False
            if capacity is not None and self._u64(_OFF_CAPACITY) != int(capacity):
                return False
            return True
        except Exception:
            return False

    def mark_ready(self, ready=True):
        self._set_u32(_OFF_READY, 1 if ready else 0)

    def is_ready(self):
        try:
            return self._u32(_OFF_READY) == 1
        except Exception:
            return False

    def generation(self):
        try:
            return self._u64(_OFF_GENERATION)
        except Exception:
            return 0

    def head(self):
        try:
            return self._u64(_OFF_HEAD)
        except Exception:
            return 0

    def tail(self):
        try:
            return self._u64(_OFF_TAIL)
        except Exception:
            return 0

    # -- framing ----------------------------------------------------------
    def _frame_end(self, offset, head):
        ring_position = offset % self.capacity
        position = _HEADER_BYTES + ring_position
        remaining = self.capacity - ring_position
        if remaining < 4:
            return offset + remaining
        length = self._u32(position)
        if length == 0:
            return offset + remaining
        if length > self.capacity - 4:
            return head
        end = offset + 4 + length
        if end > head:
            return head
        if end <= offset:
            return head
        return end

    def write(self, payload, mutex, timeout_seconds, overwrite=False):
        """Write one frame. False means full (or mutex timeout)."""
        length = len(payload)
        need = 4 + length
        if need > self.capacity - 4:
            return False
        if not mutex.acquire(timeout_seconds):
            return False
        try:
            head = self._u64(_OFF_HEAD)
            tail = self._u64(_OFF_TAIL)
            ring_position = head % self.capacity
            position = _HEADER_BYTES + ring_position
            wrap = (self.capacity - ring_position) < need
            required = need if not wrap else (self.capacity - ring_position) + need
            used = head - tail
            if used + required > self.capacity:
                if not overwrite and used != 0:
                    return False
                if overwrite:
                    while used + required > self.capacity and tail < head:
                        tail = self._frame_end(tail, head)
                        used = head - tail
                if used + required > self.capacity:
                    # The wrap marker alone cannot fit. With an empty ring
                    # (or in overwrite mode) start the next frame at the next
                    # boundary instead of waiting forever for space that can
                    # never appear.
                    boundary = head + (self.capacity - ring_position)
                    head = boundary
                    tail = boundary
                    position = _HEADER_BYTES
                    wrap = False
                    required = need
            if wrap:
                self.view[position:position + 4] = struct.pack("<I", 0)
                head += self.capacity - ring_position
                position = _HEADER_BYTES
            self.view[position:position + 4] = struct.pack("<I", length)
            if length:
                self.view[position + 4:position + 4 + length] = payload
            head += need
            self._set_u64(_OFF_HEAD, head)
            self._set_u64(_OFF_TAIL, tail)
            return True
        finally:
            mutex.release()

    def pop(self, mutex, max_items=20, max_bytes=0, timeout_seconds=1.0):
        """Consume frames and advance the global tail (single-consumer RPC)."""
        if not mutex.acquire(timeout_seconds):
            return []
        try:
            head = self._u64(_OFF_HEAD)
            tail = self._u64(_OFF_TAIL)
            if tail > head:
                tail = head
            frames = []
            used_bytes = 0
            while tail < head and len(frames) < int(max_items):
                ring_position = tail % self.capacity
                position = _HEADER_BYTES + ring_position
                remaining = self.capacity - ring_position
                if remaining < 4:
                    tail += remaining
                    continue
                length = self._u32(position)
                if length == 0:
                    tail += remaining
                    continue
                if length > self.capacity - 4:
                    tail = head
                    break
                frame_bytes = 4 + length
                if max_bytes and frames and used_bytes + frame_bytes > int(max_bytes):
                    break
                frames.append(bytes(self.view[position + 4:position + 4 + length]))
                used_bytes += frame_bytes
                tail += frame_bytes
            self._set_u64(_OFF_TAIL, tail)
            return frames
        finally:
            mutex.release()

    def read_from(self, offset, mutex, max_items=64, max_bytes=0,
                  timeout_seconds=0.5):
        """Broadcast read: local cursor, never advances the global tail."""
        if not mutex.acquire(timeout_seconds):
            return [], offset, False
        try:
            head = self._u64(_OFF_HEAD)
            tail = self._u64(_OFF_TAIL)
            dropped = False
            if offset < tail:
                offset = tail
                dropped = True
            if offset > head:
                offset = head
                dropped = True
            frames = []
            used_bytes = 0
            while offset < head and len(frames) < int(max_items):
                ring_position = offset % self.capacity
                position = _HEADER_BYTES + ring_position
                remaining = self.capacity - ring_position
                if remaining < 4:
                    offset += remaining
                    continue
                length = self._u32(position)
                if length == 0:
                    offset += remaining
                    continue
                if length > self.capacity - 4:
                    offset = head
                    break
                frame_bytes = 4 + length
                if max_bytes and frames and used_bytes + frame_bytes > int(max_bytes):
                    break
                frames.append(bytes(self.view[position + 4:position + 4 + length]))
                used_bytes += frame_bytes
                offset += frame_bytes
            return frames, offset, dropped
        finally:
            mutex.release()


def _ring_bytes(value, default):
    try:
        size = int(value)
    except Exception:
        size = int(default)
    if size < _HEADER_BYTES + _MIN_RING_BYTES:
        size = _HEADER_BYTES + _MIN_RING_BYTES
    if size > _MAX_RING_BYTES:
        raise TransportError(
            "shared-memory ring size %d exceeds the %d-byte limit"
            % (size, _MAX_RING_BYTES))
    return size


def _open_existing_mapping(name, size, timeout_seconds):
    deadline = _monotonic() + max(0.0, float(timeout_seconds))
    while True:
        view = _try_open_mapping(name, size)
        if view is not None:
            return view
        if _monotonic() >= deadline:
            return None
        time.sleep(_CONNECT_POLL_SECONDS)


class _Waiter(object):
    __slots__ = ("event", "response", "error")

    def __init__(self):
        self.event = threading.Event()
        self.response = None
        self.error = None


class _ReplyTarget(object):
    __slots__ = ("name", "view", "mutex", "event", "ring", "last_used")

    def __init__(self, name, view, mutex, event, ring):
        self.name = name
        self.view = view
        self.mutex = mutex
        self.event = event
        self.ring = ring
        self.last_used = _monotonic()

    def close(self):
        _close_mapping(self.view)
        self.view = None
        if self.mutex is not None:
            self.mutex.close()
            self.mutex = None
        if self.event is not None:
            self.event.close()
            self.event = None


class SharedMemoryTransport(RpcTransport):
    """Request/response over Windows named shared memory.

    Client side: one private response ring per transport instance, so several
    client processes can share the request ring without stealing each other's
    replies. Server side: one request ring, a background receiver (or the
    adjust-thread drain), and a small worker pool when background mode is on.
    """

    name = "shm"

    def __init__(self, account_id="", print_prefix="[bigqmt_rpc]",
                 base_name="bigqmt_shm",
                 request_bytes=_DEFAULT_REQUEST_BYTES,
                 response_bytes=_DEFAULT_RESPONSE_BYTES,
                 connect_timeout_seconds=5.0,
                 write_timeout_seconds=2.0,
                 poll_interval_seconds=0.05,
                 worker_count=4,
                 max_request_batch=64,
                 client_id=None,
                 **kwargs):
        super(SharedMemoryTransport, self).__init__(
            account_id=account_id, print_prefix=print_prefix)
        self.base_name = _safe_name(base_name or "bigqmt_shm")
        self.request_bytes = _ring_bytes(request_bytes, _DEFAULT_REQUEST_BYTES)
        self.response_bytes = _ring_bytes(response_bytes, _DEFAULT_RESPONSE_BYTES)
        self.connect_timeout_seconds = max(0.0, float(connect_timeout_seconds or 0.0))
        self.write_timeout_seconds = max(0.0, float(write_timeout_seconds or 0.0))
        self.poll_interval_seconds = max(0.001, float(poll_interval_seconds or 0.05))
        self.worker_count = max(0, int(worker_count or 0))
        self.max_request_batch = max(1, int(max_request_batch or 1))

        self.request_name = shm_rpc_name(self.base_name, account_id)
        self.request_mutex_name = self.request_name + "_mtx"
        self.request_event_name = self.request_name + "_evt"
        self.client_id = _safe_name(
            client_id or ("%d_%s" % (os.getpid(), uuid.uuid4().hex[:12])))
        self.response_name = self.request_name + "_resp_" + self.client_id
        self.response_mutex_name = self.response_name + "_mtx"
        self.response_event_name = self.response_name + "_evt"

        # client side
        self._resp_view = None
        self._resp_handle = None
        self._resp_mutex = None
        self._resp_event = None
        self._resp_ring = None
        self._request_client_mutex = None
        self._request_client_event = None
        self._client_running = False
        self._client_reader = None
        self._waiters = {}
        self._waiters_lock = threading.Lock()

        # server side
        self._server_running = False
        self._background_threads = True
        self._server_view = None
        self._server_handle = None
        self._server_mutex = None
        self._server_event = None
        self._server_ring = None
        self._request_thread = None
        self._request_queue = None
        self._workers = []
        self._reply_targets = collections.OrderedDict()
        self._reply_lock = threading.RLock()
        self._published_count = 0

    # -- client side -------------------------------------------------------
    def _ensure_client(self):
        if self._resp_view is not None:
            return
        view, handle, existed = _create_mapping(
            self.response_name, self.response_bytes)
        mutex = _NamedMutex(self.response_mutex_name)
        event = _NamedEvent(self.response_event_name)
        ring = _ByteRing(view, self.response_bytes)
        try:
            mutex.acquire(None)
            try:
                ring.initialize()
                ring.mark_ready()
            finally:
                mutex.release()
        except Exception:
            _close_mapping(view, handle)
            mutex.close()
            event.close()
            raise
        self._resp_view = view
        self._resp_handle = handle
        self._resp_mutex = mutex
        self._resp_event = event
        self._resp_ring = ring
        self._request_client_mutex = _NamedMutex(self.request_mutex_name)
        self._request_client_event = _NamedEvent(self.request_event_name)
        self._client_running = True
        thread = threading.Thread(
            target=self._client_reader_loop, name="bigqmt-shm-resp")
        thread.daemon = True
        thread.start()
        self._client_reader = thread

    def _client_reader_loop(self):
        while self._client_running:
            try:
                self._resp_event.wait(self.poll_interval_seconds)
            except Exception:
                pass
            if not self._client_running:
                return
            frames = []
            try:
                frames = self._resp_ring.pop(
                    self._resp_mutex, max_items=64,
                    max_bytes=4 * 1024 * 1024, timeout_seconds=1.0)
            except Exception:
                frames = []
            for raw in frames:
                try:
                    response = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                if not isinstance(response, dict):
                    continue
                request_id = str(response.get("request_id") or "")
                if not request_id:
                    continue
                with self._waiters_lock:
                    waiter = self._waiters.pop(request_id, None)
                if waiter is not None:
                    waiter.response = response
                    waiter.event.set()

    def _open_request_channel(self, deadline):
        while True:
            if not self._client_running:
                raise TransportError("shared-memory transport is stopped")
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise TransportTimeout(
                    "shared-memory server %r is not ready" % self.request_name)
            view = _try_open_mapping(self.request_name, self.request_bytes)
            if view is not None:
                ring = _ByteRing(view, self.request_bytes)
                mutex = self._request_client_mutex
                valid = False
                if mutex is not None and mutex.acquire(min(0.5, remaining)):
                    try:
                        valid = ring.validate() and ring.is_ready()
                    finally:
                        mutex.release()
                if valid:
                    return view, ring
                try:
                    view.close()
                except Exception:
                    pass
            time.sleep(min(_CONNECT_POLL_SECONDS,
                           max(0.0, deadline - _monotonic())))

    def _write_request_frame(self, ring, payload, deadline):
        while True:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise TransportTimeout("shared-memory request queue is full")
            if ring.write(payload, self._request_client_mutex,
                          min(0.05, remaining), overwrite=False):
                return
            time.sleep(0.001)

    def send_request(self, request, timeout_seconds):
        if timeout_seconds is None:
            timeout_seconds = 30.0
        timeout_seconds = max(0.001, float(timeout_seconds))
        deadline = _monotonic() + timeout_seconds
        self._ensure_client()

        request = dict(request or {})
        request_id = str(request.get("request_id") or uuid.uuid4().hex)
        request["request_id"] = request_id
        request["_shm_reply"] = {
            "name": self.response_name,
            "bytes": self.response_bytes,
            "mutex": self.response_mutex_name,
            "event": self.response_event_name,
        }
        payload = json.dumps(
            request, ensure_ascii=False, default=str).encode("utf-8")
        if len(payload) + 4 > self.request_bytes - _HEADER_BYTES - 4:
            raise TransportError(
                "shared-memory request is %d bytes, larger than the %d-byte "
                "request ring; raise rpc.shm.request_bytes"
                % (len(payload) + 4, self.request_bytes))

        waiter = _Waiter()
        with self._waiters_lock:
            self._waiters[request_id] = waiter
        try:
            view, ring = self._open_request_channel(deadline)
            try:
                self._write_request_frame(ring, payload, deadline)
                if self._request_client_event is not None:
                    self._request_client_event.set()
            finally:
                try:
                    view.close()
                except Exception:
                    pass
            remaining = deadline - _monotonic()
            if remaining <= 0 or not waiter.event.wait(remaining):
                raise TransportTimeout(
                    "shared-memory rpc timeout after %.3fs (method=%s)"
                    % (timeout_seconds, request.get("method")))
            if waiter.error is not None:
                raise waiter.error
            if waiter.response is None:
                raise TransportTimeout(
                    "shared-memory rpc returned no response (method=%s)"
                    % request.get("method"))
            return waiter.response
        finally:
            with self._waiters_lock:
                self._waiters.pop(request_id, None)

    def _stop_client(self):
        self._client_running = False
        if self._resp_event is not None:
            self._resp_event.set()
        with self._waiters_lock:
            waiters = list(self._waiters.values())
            self._waiters.clear()
        for waiter in waiters:
            waiter.error = TransportError("shared-memory transport stopped")
            waiter.event.set()
        thread = self._client_reader
        if thread is not None and thread.is_alive():
            thread.join(1.0)
        self._client_reader = None
        _close_mapping(self._resp_view, self._resp_handle)
        self._resp_view = None
        self._resp_handle = None
        self._resp_ring = None
        if self._resp_mutex is not None:
            self._resp_mutex.close()
            self._resp_mutex = None
        if self._resp_event is not None:
            self._resp_event.close()
            self._resp_event = None
        if self._request_client_mutex is not None:
            self._request_client_mutex.close()
            self._request_client_mutex = None
        if self._request_client_event is not None:
            self._request_client_event.close()
            self._request_client_event = None

    # -- server side -------------------------------------------------------
    def start_receiving(self, on_request, **kwargs):
        super(SharedMemoryTransport, self).start_receiving(on_request)
        background = kwargs.get("background_threads")
        self._background_threads = True if background is None else bool(background)
        try:
            with _SERVERS_LOCK:
                previous = _SERVERS_BY_NAME.get(self.request_name)
                if previous is not None and previous is not self \
                        and previous._server_running:
                    try:
                        previous.stop()
                    except Exception:
                        pass
                _SERVERS_BY_NAME[self.request_name] = self
            view, handle, existed = _create_mapping(
                self.request_name, self.request_bytes)
            mutex = _NamedMutex(self.request_mutex_name)
            event = _NamedEvent(self.request_event_name)
            ring = _ByteRing(view, self.request_bytes)
            try:
                mutex.acquire(None)
                try:
                    ring.initialize()
                    ring.mark_ready()
                finally:
                    mutex.release()
            except Exception:
                _close_mapping(view, handle)
                mutex.close()
                event.close()
                raise
            self._server_view = view
            self._server_handle = handle
            self._server_mutex = mutex
            self._server_event = event
            self._server_ring = ring
            self._server_running = True
            if existed:
                print("%s reusing shared-memory ring %s"
                      % (self.print_prefix, self.request_name))
            if self._background_threads:
                self._start_background_receivers()
        except Exception:
            self._running = False
            raise

    def _start_background_receivers(self):
        if self.worker_count > 0:
            self._request_queue = queue.Queue(
                maxsize=max(64, self.worker_count * 64))
            for index in range(self.worker_count):
                worker = threading.Thread(
                    target=self._worker_loop,
                    name="bigqmt-shm-worker-%d" % index)
                worker.daemon = True
                worker.start()
                self._workers.append(worker)
        thread = threading.Thread(
            target=self._receive_loop, name="bigqmt-shm-recv")
        thread.daemon = True
        thread.start()
        self._request_thread = thread

    def _decode_request(self, raw):
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        return request if isinstance(request, dict) else None

    def _receive_loop(self):
        while self._server_running:
            try:
                self._server_event.wait(self.poll_interval_seconds)
            except Exception:
                pass
            if not self._server_running:
                return
            try:
                frames = self._server_ring.pop(
                    self._server_mutex, max_items=self.max_request_batch,
                    timeout_seconds=1.0)
            except Exception:
                continue
            for raw in frames:
                request = self._decode_request(raw)
                if request is None:
                    continue
                if self._request_queue is None:
                    self.deliver(request)
                    continue
                while self._server_running:
                    try:
                        self._request_queue.put(request, timeout=1.0)
                        break
                    except queue.Full:
                        continue

    def _worker_loop(self):
        while True:
            try:
                request = self._request_queue.get(timeout=0.5)
            except queue.Empty:
                if not self._server_running:
                    return
                continue
            if request is None:
                return
            self.deliver(request)

    def drain_request_queue(self, max_items=20):
        if self._background_threads or not self._server_running:
            return 0
        if self._server_ring is None:
            return 0
        processed = 0
        while processed < int(max_items):
            try:
                frames = self._server_ring.pop(
                    self._server_mutex, max_items=1, timeout_seconds=0.0)
            except Exception:
                break
            if not frames:
                break
            request = self._decode_request(frames[0])
            if request is not None:
                self.deliver(request)
            processed += 1
        return processed

    def _reply_target(self, hint):
        name = str((hint or {}).get("name") or "")
        if not name:
            raise TransportError("request has no shared-memory reply route")
        size = _ring_bytes(
            (hint or {}).get("bytes") or self.response_bytes,
            self.response_bytes)
        mutex_name = str((hint or {}).get("mutex") or (name + "_mtx"))
        event_name = str((hint or {}).get("event") or (name + "_evt"))
        key = (name, size)
        with self._reply_lock:
            target = self._reply_targets.get(key)
            if target is not None:
                target.last_used = _monotonic()
                return target
        view = _open_existing_mapping(
            name, size, max(0.2, self.connect_timeout_seconds))
        if view is None:
            raise TransportError(
                "shared-memory reply ring %r disappeared" % name)
        ring = _ByteRing(view, size)
        mutex = _NamedMutex(mutex_name)
        event = _NamedEvent(event_name)
        valid = False
        if mutex.acquire(1.0):
            try:
                valid = ring.validate() and ring.is_ready()
            finally:
                mutex.release()
        if not valid:
            _close_mapping(view)
            mutex.close()
            event.close()
            raise TransportError(
                "shared-memory reply ring %r is not ready" % name)
        target = _ReplyTarget(name, view, mutex, event, ring)
        with self._reply_lock:
            existing = self._reply_targets.get(key)
            if existing is not None:
                target.close()
                return existing
            self._reply_targets[key] = target
            while len(self._reply_targets) > 64:
                _, stale = self._reply_targets.popitem(last=False)
                stale.close()
        return target

    def send_response(self, request, response):
        target = self._reply_target((request or {}).get("_shm_reply"))
        payload = json.dumps(
            response, ensure_ascii=False, default=str).encode("utf-8")
        if len(payload) + 4 > target.ring.capacity - 4:
            raise TransportError(
                "shared-memory response is %d bytes, larger than the %d-byte "
                "response ring; raise rpc.shm.response_bytes"
                % (len(payload) + 4, target.ring.capacity))
        deadline = _monotonic() + max(0.001, self.write_timeout_seconds)
        while True:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise TransportTimeout(
                    "shared-memory client is not draining replies")
            if target.ring.write(payload, target.mutex,
                                 min(0.05, remaining), overwrite=False):
                target.event.set()
                self._published_count += 1
                return
            time.sleep(0.001)

    def _stop_server(self):
        self._server_running = False
        if self._server_event is not None:
            self._server_event.set()
        thread = self._request_thread
        if thread is not None and thread.is_alive():
            thread.join(1.0)
        self._request_thread = None
        if self._request_queue is not None:
            for _ in self._workers:
                try:
                    self._request_queue.put_nowait(None)
                except Exception:
                    pass
        for worker in self._workers:
            if worker.is_alive():
                worker.join(1.0)
        self._workers = []
        self._request_queue = None
        with self._reply_lock:
            targets = list(self._reply_targets.values())
            self._reply_targets = {}
        for target in targets:
            target.close()
        _close_mapping(self._server_view, self._server_handle)
        self._server_view = None
        self._server_handle = None
        self._server_ring = None
        if self._server_mutex is not None:
            self._server_mutex.close()
            self._server_mutex = None
        if self._server_event is not None:
            self._server_event.close()
            self._server_event = None
        with _SERVERS_LOCK:
            if _SERVERS_BY_NAME.get(self.request_name) is self:
                _SERVERS_BY_NAME.pop(self.request_name, None)

    def stop(self):
        self._stop_server()
        self._stop_client()
        try:
            super(SharedMemoryTransport, self).stop()
        except Exception:
            self._running = False

    def __repr__(self):
        return "<SharedMemoryTransport account_id=%r ring=%r>" % (
            self.account_id, self.request_name)


class ShmQuotePushChannel(object):
    """Broadcast quote/exec push over one named shared-memory ring per account.

    Server: ``start_publisher`` + ``publish``. Client: ``start_subscriber``.
    Multiple subscriber processes/threads are safe: each keeps its own read
    offset, and the writer only advances the global tail when it must drop the
    oldest frames to make room.
    """

    def __init__(self, bind_address=None, connect_address=None,
                 print_prefix="[bigqmt_quote_push]",
                 push_bytes=_DEFAULT_PUSH_BYTES,
                 extra_bind_addresses=None,
                 poll_interval_seconds=0.05,
                 subscriber_join_seconds=1.0):
        self.bind_address = str(bind_address) if bind_address else None
        self.connect_address = str(connect_address) if connect_address else None
        self.print_prefix = print_prefix
        self.push_bytes = _ring_bytes(push_bytes, _DEFAULT_PUSH_BYTES)
        extras = []
        for address in (extra_bind_addresses or []):
            text = str(address or "")
            if text and text != self.bind_address and text not in extras:
                extras.append(text)
        self.extra_bind_addresses = extras
        self.poll_interval_seconds = max(0.001, float(poll_interval_seconds or 0.05))
        self.subscriber_join_seconds = max(0.0, float(subscriber_join_seconds or 0.0))
        self._running = False
        self._publishers = []
        self._sub_thread = None
        self._subscriber_stop = None
        self._stale_subscriber_threads = []

    def _publisher_names(self):
        names = []
        if self.bind_address:
            names.append(self.bind_address)
        for address in self.extra_bind_addresses:
            if address not in names:
                names.append(address)
        return names

    # -- server side -------------------------------------------------------
    def start_publisher(self):
        if not self.bind_address:
            raise ValueError("bind_address is required to start a publisher")
        if self._running:
            return
        for name in self._publisher_names():
            with _PUBLISHERS_LOCK:
                previous = _PUBLISHERS_BY_NAME.get(name)
                if previous is not None and previous is not self:
                    try:
                        previous.stop()
                    except Exception:
                        pass
                _PUBLISHERS_BY_NAME[name] = self
            view, handle, existed = _create_mapping(name, self.push_bytes)
            mutex = _NamedMutex(name + "_mtx")
            event = _NamedEvent(name + "_evt")
            ring = _ByteRing(view, self.push_bytes)
            try:
                mutex.acquire(None)
                try:
                    ring.initialize()
                    ring.mark_ready()
                finally:
                    mutex.release()
            except Exception:
                _close_mapping(view, handle)
                mutex.close()
                event.close()
                raise
            self._publishers.append(
                _ReplyTarget(name, view, mutex, event, ring))
            if existed:
                print("%s reusing push ring %s"
                      % (self.print_prefix, name))
        self._running = True

    def publish(self, topic, data):
        if not self._running or not self._publishers:
            return
        # Fixed JSON, not msgpack: the QMT-side Python and the external client
        # may have different optional packages. A server WITH msgpack and a
        # client WITHOUT it would otherwise emit bytes the client cannot read.
        payload = json.dumps(
            {"topic": str(topic), "data": data},
            ensure_ascii=False, default=str).encode("utf-8")
        for target in self._publishers:
            if len(payload) + 4 > target.ring.capacity - 4:
                print("%s push frame is %d bytes, larger than the %d-byte "
                      "ring; frame dropped"
                      % (self.print_prefix, len(payload) + 4,
                         target.ring.capacity))
                continue
            try:
                if target.ring.write(payload, target.mutex, 0.1,
                                     overwrite=True):
                    target.event.set()
            except Exception as exc:
                print("%s push write failed: %s"
                      % (self.print_prefix, exc))

    # -- client side -------------------------------------------------------
    def start_subscriber(self, topics, on_msg):
        if not self.connect_address:
            raise ValueError("connect_address is required to subscribe")
        self._stop_subscriber()
        stop_event = threading.Event()
        self._subscriber_stop = stop_event
        thread = threading.Thread(
            target=self._sub_loop, args=(topics, on_msg, stop_event),
            name="bigqmt-shm-push-sub")
        thread.daemon = True
        thread.start()
        self._sub_thread = thread

    def _open_subscriber_channel(self, stop_event):
        view = _open_existing_mapping(
            self.connect_address, self.push_bytes,
            max(0.05, self.poll_interval_seconds * 2.0))
        if view is None:
            return None
        mutex = _NamedMutex(self.connect_address + "_mtx")
        event = _NamedEvent(self.connect_address + "_evt")
        ring = _ByteRing(view, self.push_bytes)
        valid = False
        if mutex.acquire(0.5):
            try:
                valid = ring.validate() and ring.is_ready()
            finally:
                mutex.release()
        if not valid:
            _close_mapping(view)
            mutex.close()
            event.close()
            return None
        return view, mutex, event, ring

    def _sub_loop(self, topics, on_msg, stop_event):
        while not stop_event.is_set():
            opened = self._open_subscriber_channel(stop_event)
            if opened is None:
                stop_event.wait(self.poll_interval_seconds)
                continue
            view, mutex, event, ring = opened
            try:
                generation = ring.generation()
                local = ring.head()
                while not stop_event.is_set():
                    frames, local, _dropped = ring.read_from(
                        local, mutex, max_items=64,
                        max_bytes=8 * 1024 * 1024,
                        timeout_seconds=0.5)
                    for raw in frames:
                        try:
                            message = json.loads(raw.decode("utf-8"))
                        except Exception:
                            continue
                        if not isinstance(message, dict):
                            continue
                        topic = str(
                            message.get("topic")
                            or message.get("combo_key") or "")
                        if topics and not any(
                                topic.startswith(prefix)
                                for prefix in topics):
                            continue
                        try:
                            on_msg(topic, message.get("data"))
                        except Exception as exc:
                            print("%s subscriber callback failed: %s"
                                  % (self.print_prefix, exc))
                    new_generation = ring.generation()
                    if new_generation != generation:
                        # Server restarted and reinitialized the ring: do not
                        # replay frames from the previous generation.
                        generation = new_generation
                        local = ring.head()
                    if not frames:
                        event.wait(self.poll_interval_seconds)
            except Exception:
                pass
            finally:
                _close_mapping(view)
                mutex.close()
                event.close()
            if not stop_event.is_set():
                stop_event.wait(self.poll_interval_seconds)

    def _stop_subscriber(self):
        stop_event = self._subscriber_stop
        if stop_event is not None:
            stop_event.set()
        thread = self._sub_thread
        if thread is not None and thread.is_alive():
            thread.join(self.subscriber_join_seconds)
            if thread.is_alive():
                self._stale_subscriber_threads.append(thread)
                print("%s subscriber did not stop within %.1fs; it will exit "
                      "when its callback returns"
                      % (self.print_prefix, self.subscriber_join_seconds))
        self._sub_thread = None
        self._subscriber_stop = None

    def active_subscribers(self):
        alive = [thread for thread in self._stale_subscriber_threads
                 if thread.is_alive()]
        self._stale_subscriber_threads = alive
        current = self._sub_thread
        return len(alive) + (
            1 if current is not None and current.is_alive() else 0)

    # -- both --------------------------------------------------------------
    def stop(self):
        self._running = False
        self._stop_subscriber()
        publishers = self._publishers
        self._publishers = []
        for target in publishers:
            with _PUBLISHERS_LOCK:
                if _PUBLISHERS_BY_NAME.get(target.name) is self:
                    _PUBLISHERS_BY_NAME.pop(target.name, None)
            target.close()
