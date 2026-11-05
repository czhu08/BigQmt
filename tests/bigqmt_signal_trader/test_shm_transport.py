# coding: utf-8
"""Shared-memory transport: same-process semantics, wrap, push, drain."""

import os
import sys
import threading
import time
import unittest
import uuid


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports.base import (  # noqa: E402
    TransportTimeout,
)
from bigqmt_signal_trader.transports.factory import (  # noqa: E402
    KNOWN_TRANSPORTS, build_transport,
)
from bigqmt_signal_trader.transports.shm_transport import (  # noqa: E402
    SharedMemoryTransport, ShmQuotePushChannel, shm_push_name, shm_rpc_name,
)


WINDOWS_ONLY = unittest.skipUnless(os.name == "nt", "共享内存传输是 Windows 专有")


def _base():
    return "bigqmt_test_" + uuid.uuid4().hex[:10]


def _request(method="ping", params=None):
    return {
        "schema_version": 1,
        "request_id": uuid.uuid4().hex,
        "account_id": "t",
        "method": method,
        "params": params or {},
    }


def _echo(request):
    return {
        "schema_version": 1,
        "request_id": request.get("request_id"),
        "account_id": request.get("account_id"),
        "method": request.get("method"),
        "ok": True,
        "data": {"echo": request.get("params")},
    }


class ShmNameTest(unittest.TestCase):
    def test_the_account_id_is_part_of_the_ring_name(self):
        live = shm_rpc_name("bigqmt_shm", "8886800503")
        sim = shm_rpc_name("bigqmt_shm", "5500000001")
        self.assertNotEqual(live, sim)
        self.assertIn("8886800503", live)
        self.assertIn("_push", shm_push_name("bigqmt_shm", "8886800503"))

    def test_unsafe_characters_are_sanitized(self):
        name = shm_rpc_name("big qmt/shm", "a b:c")
        self.assertNotIn(" ", name)
        self.assertNotIn("/", name)
        self.assertNotIn(":", name)


@WINDOWS_ONLY
class ShmRoundTripTest(unittest.TestCase):
    def _pair(self, base, background_threads=True, **kwargs):
        server = SharedMemoryTransport(
            account_id="t", base_name=base, poll_interval_seconds=0.005,
            connect_timeout_seconds=2.0, **kwargs)
        server.start_receiving(_echo, background_threads=background_threads)
        self.addCleanup(server.stop)
        client = SharedMemoryTransport(
            account_id="t", base_name=base, poll_interval_seconds=0.005,
            connect_timeout_seconds=2.0, **kwargs)
        self.addCleanup(client.stop)
        return server, client

    def test_round_trip_with_background_threads(self):
        _server, client = self._pair(_base(), background_threads=True)
        request = _request(params={"x": 1})
        response = client.send_request(request, 2.0)
        self.assertTrue(response["ok"])
        self.assertEqual(request["request_id"], response["request_id"])
        self.assertEqual({"x": 1}, response["data"]["echo"])

    def test_round_trip_through_drain_request_queue(self):
        server, client = self._pair(_base(), background_threads=False)
        request = _request(params={"drained": True})
        result = {}

        def call():
            try:
                result["response"] = client.send_request(request, 2.0)
            except Exception as exc:  # pragma: no cover - asserted below
                result["error"] = exc

        thread = threading.Thread(target=call)
        thread.start()
        deadline = time.time() + 2.0
        processed = 0
        while thread.is_alive() and time.time() < deadline:
            processed += server.drain_request_queue(10)
            time.sleep(0.005)
        thread.join(1.0)
        self.assertNotIn("error", result)
        self.assertGreaterEqual(processed, 1)
        self.assertEqual({"drained": True},
                         result["response"]["data"]["echo"])

    def test_background_mode_leaves_drain_a_no_op(self):
        server, _client = self._pair(_base(), background_threads=True)
        self.assertEqual(0, server.drain_request_queue(10))

    def test_missing_server_times_out(self):
        client = SharedMemoryTransport(
            account_id="t", base_name=_base(), poll_interval_seconds=0.005)
        self.addCleanup(client.stop)
        with self.assertRaises(TransportTimeout):
            client.send_request(_request(), 0.2)

    def test_two_clients_do_not_steal_each_others_replies(self):
        _server, client_a = self._pair(_base(), background_threads=True)
        client_b = SharedMemoryTransport(
            account_id="t", base_name=client_a.base_name,
            poll_interval_seconds=0.005, connect_timeout_seconds=2.0)
        self.addCleanup(client_b.stop)
        results = {}
        errors = []

        def run(key, client):
            try:
                for index in range(30):
                    response = client.send_request(
                        _request(params={"owner": key, "n": index}), 2.0)
                    if response["data"]["echo"] != {"owner": key, "n": index}:
                        raise AssertionError(response)
                results[key] = True
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append((key, exc))

        threads = [
            threading.Thread(target=run, args=("a", client_a)),
            threading.Thread(target=run, args=("b", client_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
        self.assertEqual([], errors)
        self.assertTrue(results.get("a"))
        self.assertTrue(results.get("b"))

    def test_wrapping_a_small_ring_preserves_every_frame(self):
        small = {"request_bytes": 8192, "response_bytes": 8192}
        _server, client = self._pair(_base(), background_threads=True, **small)
        payload = "x" * 3000
        for index in range(20):
            response = client.send_request(
                _request(params={"n": index, "payload": payload}), 2.0)
            self.assertEqual(index, response["data"]["echo"]["n"])
            self.assertEqual(payload, response["data"]["echo"]["payload"])


@WINDOWS_ONLY
class ShmPushTest(unittest.TestCase):
    def _subscriber(self, name, topics):
        seen = []
        subscriber = ShmQuotePushChannel(
            connect_address=name, push_bytes=256 * 1024,
            poll_interval_seconds=0.005)
        subscriber.start_subscriber(
            topics, lambda topic, data: seen.append((topic, data)))
        self.addCleanup(subscriber.stop)
        return subscriber, seen

    def test_broadcast_reaches_every_subscriber(self):
        name = shm_push_name(_base(), "t")
        publisher = ShmQuotePushChannel(
            bind_address=name, push_bytes=256 * 1024)
        publisher.start_publisher()
        self.addCleanup(publisher.stop)
        first, first_seen = self._subscriber(name, ["quote"])
        second, second_seen = self._subscriber(name, ["quote"])
        time.sleep(0.2)
        for index in range(5):
            publisher.publish("quote.600000.SH", {"i": index})
        deadline = time.time() + 2.0
        while (len(first_seen) < 5 or len(second_seen) < 5) \
                and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(5, len(first_seen), first_seen)
        self.assertEqual(5, len(second_seen), second_seen)
        self.assertEqual({"i": 4}, first_seen[-1][1])

    def test_topic_prefix_filter(self):
        name = shm_push_name(_base(), "t")
        publisher = ShmQuotePushChannel(
            bind_address=name, push_bytes=256 * 1024)
        publisher.start_publisher()
        self.addCleanup(publisher.stop)
        _subscriber, seen = self._subscriber(name, ["quote."])
        time.sleep(0.2)
        publisher.publish("exec.order", {"n": 1})
        publisher.publish("quote.600000.SH", {"n": 2})
        deadline = time.time() + 2.0
        while not seen and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual([("quote.600000.SH", {"n": 2})], seen)


@WINDOWS_ONLY
class ShmFactoryTest(unittest.TestCase):
    def test_factory_builds_shm_with_the_configured_base_name(self):
        self.assertIn("shm", KNOWN_TRANSPORTS)
        transport = build_transport(
            "shm",
            {"shm": {"base_name": "custom_shm", "request_bytes": 65536}},
            account_id="acct",
        )
        self.addCleanup(transport.stop)
        self.assertEqual("shm", transport.name)
        self.assertIn("custom_shm_acct", transport.request_name)
        self.assertEqual(65536, transport.request_bytes)

    def test_client_does_not_treat_the_default_redis_host_as_opt_in(self):
        from bigqmt_signal_trader.xtquant_compat import (
            BigQmtRpcClient, _build_quote_push_channel,
        )

        client = BigQmtRpcClient(
            account_id="acct", transport="shm",
            redis_config={"transport": "shm", "host": "127.0.0.1"})
        self.assertEqual("shm", client.transport_name)
        self.assertFalse(client._redis_explicit)
        channel = _build_quote_push_channel(client)
        self.addCleanup(channel.stop)
        self.assertIsInstance(channel, ShmQuotePushChannel)


if __name__ == "__main__":
    unittest.main()
