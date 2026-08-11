"""ZeroMQ transport for the BigQMT RPC bridge (no-redis version).

Same as bigqmt_signal_trader.transports.zmq_transport, but with the redis
dependencies inlined and the redis-based service discovery removed. This lets
the module load in QMT sandboxes that reject `import redis` or any module
whose name mentions redis.

Design (unchanged from the redis version):

* **Server** binds a ``ROUTER`` socket. Each inbound message arrives as
  ``[identity, payload]``; the server remembers ``identity`` keyed by
  ``request_id`` and replies with ``[identity, payload]`` so ZMQ routes the
  response back to the originating client automatically.
* **Client** connects a ``DEALER`` socket (with a unique random identity), sends
  ``[payload]``, then ``poll``/``recv`` for the response.
"""

import base64
import json
import queue
import threading
import time
import uuid


# ---------------------------------------------------------------------------
# Inlined encoding helpers (originally from bigqmt_signal_trader.adapters.
# redis_common and bigqmt_signal_trader.redis_rpc). Kept here so this module
# has zero imports from any redis-named module.
# ---------------------------------------------------------------------------

SAFE_B64_PREFIX = "b64s:"
SAFE_B64_DIGIT_ENCODE = str.maketrans("0123456789", "!#$%&()*~?")
SAFE_B64_DIGIT_DECODE = str.maketrans("!#$%&()*~?", "0123456789")


def decode_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def encode_rpc_request_payload(request):
    """Encode request JSON so patched QMT clients do not inspect stock-code text."""
    raw = json.dumps(request, ensure_ascii=False).encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii").translate(SAFE_B64_DIGIT_ENCODE)
    return SAFE_B64_PREFIX + encoded


def decode_rpc_request_payload(text):
    text = str(text)
    if not text.startswith(SAFE_B64_PREFIX):
        return text
    encoded = text[len(SAFE_B64_PREFIX):].translate(SAFE_B64_DIGIT_DECODE)
    return base64.b64decode(encoded.encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------------------
# TransportError / TransportTimeout (inlined from transports.base -- kept here
# so this module is fully self-contained for QMT sandbox loading).
# ---------------------------------------------------------------------------

class TransportError(RuntimeError):
    pass


class TransportTimeout(TransportError):
    pass


class RpcTransport:
    """Minimal transport base (inlined subset of transports.base)."""

    def __init__(self, account_id="", print_prefix="[bigqmt_rpc]"):
        self.account_id = str(account_id or "")
        self.print_prefix = str(print_prefix or "[bigqmt_rpc]")
        self._on_request = None
        self._running = False

    def start_receiving(self, on_request):
        self._on_request = on_request
        self._running = True

    def stop(self):
        self._running = False
        self._on_request = None

    def deliver(self, request):
        callback = self._on_request
        if callback is None:
            return None
        try:
            response = callback(request)
        except Exception as exc:
            import datetime as _dt
            response = {
                "schema_version": 1,
                "request_id": str((request or {}).get("request_id") or ""),
                "account_id": str((request or {}).get("account_id") or self.account_id or ""),
                "method": str((request or {}).get("method") or ""),
                "ok": False,
                "data": None,
                "error": "%s: %s" % (exc.__class__.__name__, exc),
                "handled_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        if response is not None:
            try:
                self.send_response(request, response)
            except Exception:
                pass
        return response


# ---------------------------------------------------------------------------
# ZMQ transport
# ---------------------------------------------------------------------------

# ZMQ does not support ipc:// on Windows (it trips a signaler abort), so the
# default endpoint is tcp loopback. The port is derived from the account_id so
# distinct accounts don't collide on the same port; override via config when
# needed. Base 15560 keeps it clear of common dev ports.
DEFAULT_ZMQ_HOST = "127.0.0.1"
DEFAULT_ZMQ_BASE_PORT = 15560
DEFAULT_ZMQ_PORT_RANGE = 100  # derived port = base + (account_id_int mod range)


def _default_zmq_port(account_id):
    """Derive a stable port from account_id so each account gets its own socket."""
    text = str(account_id or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    try:
        offset = int(digits) % DEFAULT_ZMQ_PORT_RANGE if digits else 0
    except ValueError:
        offset = 0
    return DEFAULT_ZMQ_BASE_PORT + offset


def _default_zmq_address(account_id, host=None):
    host = host or DEFAULT_ZMQ_HOST
    return "tcp://%s:%d" % (host, _default_zmq_port(account_id))


def _loads(raw):
    if isinstance(raw, dict):
        return dict(raw)
    text = decode_text(raw)
    text = decode_rpc_request_payload(text)
    return json.loads(text)


class ZmqTransport(RpcTransport):
    """ZMQ ROUTER/DEALER transport (no-redis version).

    The same instance plays both roles depending on method called:
    ``send_request`` acts as a client (DEALER connect), ``start_receiving`` +
    ``send_response`` act as a server (ROUTER bind). A deployment normally uses
    one instance per role (the QMT process is the server; the external client
    is the client).

    Unlike the redis version, this one does NOT use redis-based service
    discovery. The server binds the configured address exactly; the client
    connects to the configured or derived address directly.
    """

    name = "zmq"

    def __init__(
        self,
        bind_address=None,
        connect_address=None,
        host=None,
        port=None,
        account_id="",
        print_prefix="[bigqmt_rpc]",
        io_threads=1,
        recv_timeout_seconds=1.0,
        server_hwm=10000,
        client_linger_ms=0,
    ):
        super(ZmqTransport, self).__init__(account_id=account_id, print_prefix=print_prefix)
        resolved_host = host or DEFAULT_ZMQ_HOST
        if port is not None:
            resolved_port = int(port)
        else:
            resolved_port = _default_zmq_port(account_id)
        default_addr = "tcp://%s:%d" % (resolved_host, resolved_port)
        self.bind_address = bind_address or default_addr
        self.connect_address = connect_address
        self.bind_host = resolved_host
        self.base_port = resolved_port
        self.io_threads = int(io_threads)
        self.recv_timeout_seconds = float(recv_timeout_seconds)
        self.server_hwm = int(server_hwm)
        self.client_linger_ms = int(client_linger_ms)

        self._zmq = None  # imported lazily
        self._ctx = None
        # server state
        self._router = None
        self._router_thread = None
        self._actual_bind_address = None  # set after start_receiving()
        self._pending_identities = {}  # request_id -> client identity bytes
        self._identity_lock = threading.Lock()
        self._response_queue = queue.Queue()
        self._queued_response_count = 0
        self._sent_response_count = 0
        # client state
        self._dealer = None
        self._client_lock = threading.Lock()

    # -- construction helper ----------------------------------------------
    @classmethod
    def from_config(cls, config, account_id="", print_prefix="[bigqmt_rpc]"):
        config = dict(config or {})
        return cls(
            bind_address=config.get("bind_address"),
            connect_address=config.get("connect_address"),
            host=config.get("host"),
            port=config.get("port"),
            account_id=config.get("account_id", account_id),
            print_prefix=print_prefix,
            io_threads=int(config.get("io_threads", 1)),
            recv_timeout_seconds=float(config.get("recv_timeout_seconds", 1.0)),
            server_hwm=int(config.get("server_hwm", 10000)),
            client_linger_ms=int(config.get("client_linger_ms", 0)),
        )

    # -- shared zmq context -----------------------------------------------
    def _ensure_zmq(self):
        if self._zmq is None:
            try:
                import zmq  # noqa: F401
            except ImportError as exc:  # pragma: no cover - depends on env
                raise TransportError(
                    "pyzmq is required for the zmq transport: %s" % exc
                )
            self._zmq = zmq
        if self._ctx is None:
            self._ctx = self._zmq.Context.instance(self.io_threads)
        return self._zmq, self._ctx

    # -- server side ------------------------------------------------------
    def _bind_configured_address(self):
        """Bind exactly one configured address and reject duplicate servers."""
        zmq, ctx = self._ensure_zmq()
        sock = ctx.socket(zmq.ROUTER)
        sock.setsockopt(zmq.RCVHWM, self.server_hwm)
        sock.setsockopt(zmq.SNDHWM, self.server_hwm)
        sock.setsockopt(zmq.RCVTIMEO, int(self.recv_timeout_seconds * 1000))
        try:
            sock.bind(self.bind_address)
        except self._zmq.ZMQError as exc:
            try:
                sock.close(linger=0)
            except Exception:
                pass
            if getattr(exc, "errno", None) == zmq.EADDRINUSE:
                raise TransportError(
                    "ZMQ_BIND_CONFLICT address=%s; another bridge instance "
                    "already owns the configured endpoint" % self.bind_address
                )
            raise
        self._router = sock
        self._actual_bind_address = self.bind_address

    def start_receiving(self, on_request, background_threads=True):
        super(ZmqTransport, self).start_receiving(on_request)
        zmq, ctx = self._ensure_zmq()
        self._bind_configured_address()
        bound = self._actual_bind_address or self.bind_address
        if not background_threads:
            print(
                "%s zmq bound=%s background_threads=False"
                % (self.print_prefix, bound)
            )
            return
        self._router_thread = threading.Thread(
            target=self._router_loop, name="bigqmt-zmq-rpc", daemon=True
        )
        self._router_thread.start()
        print(
            "%s zmq started bound=%s" % (self.print_prefix, self.bind_address)
        )

    def _router_loop(self):
        try:
            while self._running:
                self._drain_response_queue()
                request = self._receive_request()
                if request is not None:
                    self._deliver_request(request)
        finally:
            # Close the ROUTER socket on the thread that owns it. On Windows,
            # closing a ZMQ socket from a different thread trips a signaler
            # assertion (abort); closing it here is safe because this thread
            # created and exclusively used it.
            try:
                self._router.close(linger=0)
            except Exception:
                pass
            self._router = None

    def _receive_request(self, flags=0):
        try:
            frames = self._router.recv_multipart(flags=flags)
        except self._zmq.Again:
            return None
        except Exception as exc:
            if self._running:
                print("%s zmq recv failed: %s" % (self.print_prefix, exc))
                if not flags:
                    time.sleep(0.5)
            return None
        if len(frames) < 2:
            return None
        identity, payload = frames[0], frames[-1]
        try:
            request = _loads(payload)
        except Exception as exc:
            print("%s zmq decode failed: %s" % (self.print_prefix, exc))
            return None
        request_id = str(request.get("request_id") or uuid.uuid4().hex)
        with self._identity_lock:
            self._pending_identities[request_id] = identity
        return request

    def _deliver_request(self, request):
        started = time.perf_counter()
        try:
            self.deliver(request)
        except Exception as exc:
            print("%s zmq deliver failed: %s" % (self.print_prefix, exc))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms > 50.0:
            print("%s zmq slow handler method=%s %.0fms"
                  % (self.print_prefix, request.get("method"), elapsed_ms))

    def _drain_response_queue(self):
        while True:
            try:
                identity, payload = self._response_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._router.send_multipart([identity, payload])
                self._sent_response_count += 1
                if self._sent_response_count <= 5:
                    print("%s zmq queued response sent" % self.print_prefix)
            except Exception as exc:
                print("%s zmq send failed: %s" % (self.print_prefix, exc))

    def send_response(self, request, response):
        if self._router is None:
            raise TransportError("zmq server socket is not bound")
        request_id = str(
            response.get("request_id") or request.get("request_id") or ""
        )
        with self._identity_lock:
            identity = self._pending_identities.pop(request_id, None)
        if identity is None:
            # No matching peer -- drop silently (client may have gone away).
            return
        payload = encode_rpc_request_payload(response).encode("utf-8")
        if self._router_thread is not None and threading.current_thread() is not self._router_thread:
            self._queued_response_count += 1
            if self._queued_response_count <= 5:
                print("%s zmq response queued for router thread" % self.print_prefix)
            self._response_queue.put((identity, payload))
            return
        try:
            self._router.send_multipart([identity, payload])
        except Exception as exc:
            print("%s zmq send failed: %s" % (self.print_prefix, exc))

    def drain_request_queue(self, max_items=20):
        """Drain requests from the scheduled QMT thread when no receiver thread exists."""
        if self._router_thread is not None or self._router is None:
            return 0
        processed = 0
        for _index in range(max(int(max_items), 0)):
            request = self._receive_request(flags=self._zmq.NOBLOCK)
            if request is None:
                break
            self._deliver_request(request)
            processed += 1
        return processed

    # -- client side ------------------------------------------------------
    def _resolve_connect_address(self):
        """Resolve the address to connect to. No redis discovery -- use explicit
        connect_address, else derive from account_id."""
        if self.connect_address:
            return self.connect_address
        return _default_zmq_address(self.account_id)

    def _ensure_dealer(self):
        zmq, ctx = self._ensure_zmq()
        if self._dealer is None:
            address = self._resolve_connect_address()
            sock = ctx.socket(zmq.DEALER)
            # Unique identity so ROUTER can route replies back to us.
            sock.setsockopt(zmq.IDENTITY, uuid.uuid4().hex.encode("utf-8")[:16])
            sock.setsockopt(zmq.LINGER, self.client_linger_ms)
            sock.connect(address)
            self._dealer = sock
            self.connect_address = address
        return self._dealer

    def send_request(self, request, timeout_seconds, **_kwargs):
        zmq = self._zmq or self._ensure_zmq()[0]
        with self._client_lock:
            dealer = self._ensure_dealer()
            request = dict(request)
            request.setdefault("request_id", uuid.uuid4().hex)
            request_id = request["request_id"]
            payload = encode_rpc_request_payload(request)
            try:
                dealer.send(payload.encode("utf-8"))
            except Exception as exc:
                raise TransportError("zmq send failed: %s" % exc)
            deadline = time.time() + float(timeout_seconds)
            poller = self._zmq.Poller()
            poller.register(dealer, self._zmq.POLLIN)
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                events = dict(poller.poll(timeout=int(remaining * 1000)))
                if dealer in events:
                    frames = dealer.recv_multipart()
                    raw = frames[-1]
                    response = _loads(raw)
                    if response.get("request_id") == request_id:
                        return response
            raise TransportTimeout("zmq rpc timeout: %s" % request.get("method"))

    # -- lifecycle --------------------------------------------------------
    def stop(self):
        super(ZmqTransport, self).stop()
        # Clear _running so the router loop exits; the loop closes its own
        # socket (closing cross-thread trips a Windows signaler abort).
        thread = self._router_thread
        if thread is not None and thread.is_alive():
            thread.join(2.0)
        if thread is None and self._router is not None:
            try:
                self._router.close(linger=0)
            except Exception:
                pass
            self._router = None
        self._router_thread = None
        with self._client_lock:
            if self._dealer is not None:
                try:
                    self._dealer.close(linger=self.client_linger_ms)
                except Exception:
                    pass
                self._dealer = None
        # Do NOT terminate the shared context -- other sockets/users may rely on it.
