# coding: utf-8
"""A deployment must be able to say it has no redis (#147).

Every consumer of the redis client already guards the same way:

    redis_config = dict(config.get("redis") or {})
    if not redis_config:
        return None

and every one of those guards was dead code, because configure_runtime emitted
the block unconditionally from module defaults:

    redis={"host": REDIS_HOST,     # "127.0.0.1"
           "port": REDIS_PORT,     # 6379
           ...}

So config["redis"] was never empty and "I have no redis" was not expressible.
The reporter of #145 -- whose broker QMT does not whitelist the redis import at
all -- had no lever except patching _exec_event_redis to key off the transport,
which would have killed four unrelated features on zmq (the order-identity
store and download jobs were made transport-independent one day earlier, in
f20c58c and 053c9dc, for exactly the opposite reason).

redis_enabled=False now empties the block, and the existing guards do the rest:
nothing dials redis even once. transport=redis overrides it, because there the
bridge itself needs redis and honouring the switch would break the RPC rather
than the optional extras.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader_redis_rpc_runtime as runtime


class _RuntimeState(unittest.TestCase):
    KEYS = ("REDIS_ENABLED", "RPC_TRANSPORT", "REDIS_HOST", "REDIS_PORT", "REDIS_DB")

    def setUp(self):
        self._saved = {k: getattr(runtime, k) for k in self.KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(runtime, k, v)

    def _configure(self, enabled, transport):
        runtime.REDIS_ENABLED = enabled
        runtime.RPC_TRANSPORT = transport


class RedisBlockTest(_RuntimeState):
    def test_enabled_emits_the_settings(self):
        self._configure(True, "zmq")

        block = runtime._redis_block()

        self.assertEqual(block["host"], runtime.REDIS_HOST)
        self.assertEqual(block["port"], runtime.REDIS_PORT)

    def test_disabled_on_zmq_emits_nothing(self):
        """An empty dict is what makes every `if not redis_config` guard fire."""
        self._configure(False, "zmq")

        self.assertEqual(runtime._redis_block(), {})

    def test_disabled_on_mysql_and_shm_too(self):
        for transport in ("mysql", "shm"):
            self._configure(False, transport)

            self.assertEqual(runtime._redis_block(), {}, transport)

    def test_the_redis_transport_overrides_the_switch(self):
        """That deployment has no bridge at all without redis; honouring the
        switch would break the RPC instead of the optional extras."""
        for transport in ("redis", "", "default"):
            self._configure(False, transport)

            self.assertNotEqual(runtime._redis_block(), {}, transport)

    def test_the_default_keeps_existing_deployments_unchanged(self):
        self.assertTrue(self._saved["REDIS_ENABLED"])


class ConfigReadingTest(_RuntimeState):
    def test_configure_runtime_redis_reads_the_switch(self):
        runtime.configure_runtime_redis({"redis_enabled": False, "transport": "zmq"})

        self.assertFalse(runtime.REDIS_ENABLED)

    def test_it_defaults_to_on_when_absent(self):
        runtime.REDIS_ENABLED = True
        runtime.configure_runtime_redis({"transport": "zmq"})

        self.assertTrue(runtime.REDIS_ENABLED)

    def test_setting_it_true_explicitly_works(self):
        runtime.REDIS_ENABLED = False
        runtime.configure_runtime_redis({"redis_enabled": True, "transport": "zmq"})

        self.assertTrue(runtime.REDIS_ENABLED)


class ConsumersSeeAnEmptyBlockTest(_RuntimeState):
    """The point of an empty dict: the guards that were dead now fire."""

    def test_the_strategy_builds_no_client_from_an_empty_block(self):
        import bigqmt_signal_trader_strategy as strategy

        saved = strategy._exec_event_redis_client
        service = strategy._rpc_service
        try:
            strategy._exec_event_redis_client = None
            strategy._rpc_service = None

            self.assertIsNone(strategy._exec_event_redis({"redis": {}}))
            self.assertIsNone(strategy._exec_event_redis({}))
        finally:
            strategy._exec_event_redis_client = saved
            strategy._rpc_service = service

    def test_a_populated_block_still_builds_one(self):
        """The switch must not break deployments that do have redis."""
        import bigqmt_signal_trader_strategy as strategy

        saved = strategy._exec_event_redis_client
        service = strategy._rpc_service
        try:
            strategy._exec_event_redis_client = None
            strategy._rpc_service = None

            client = strategy._exec_event_redis(
                {"redis": {"host": "127.0.0.1", "port": 6379, "db": 5}})

            self.assertIsNotNone(client)
        finally:
            strategy._exec_event_redis_client = saved
            strategy._rpc_service = service


class ExampleConfigTest(unittest.TestCase):
    def test_the_switch_is_documented_where_people_will_look(self):
        import io

        path = os.path.join(ROOT, "src",
                            "bigqmt_signal_trader_local_config.example.py")
        text = io.open(path, encoding="utf-8").read()

        self.assertIn("redis_enabled", text)

    def test_the_example_states_what_it_costs(self):
        """Turning it on silently loses strategy_name backfill (#133); saying
        so in the config is cheaper than another issue."""
        import io

        path = os.path.join(ROOT, "src",
                            "bigqmt_signal_trader_local_config.example.py")
        text = io.open(path, encoding="utf-8").read()

        self.assertIn("strategy_name", text)

    def test_it_ships_commented_out(self):
        """Uncommenting is a decision; defaulting to it would change every
        existing deployment."""
        import io

        path = os.path.join(ROOT, "src",
                            "bigqmt_signal_trader_local_config.example.py")
        for line in io.open(path, encoding="utf-8"):
            if "redis_enabled" in line and "#" not in line.split("redis_enabled")[0]:
                self.fail("redis_enabled is live in the example: %r" % line)


class NoRedisBuildTest(unittest.TestCase):
    """The variant whose whole reason to exist is "redis is not importable here".

    It already forced transport=zmq, but not redis_enabled -- so the runtime
    filled the block in from its defaults and every consumer dialled
    127.0.0.1:6379 anyway. That is the build most likely to hit #145.

    Two files, because the forcing block is hand-copied into the single-file
    builder as well. PR #134 was about exactly this class of drift, so pin
    both rather than trusting them to stay in step.
    """

    FILES = (
        os.path.join("bigqmt_no_redis", "DRYRUN_no_redis.py"),
        os.path.join("tools", "build_no_redis_single_file_flat.py"),
    )

    def _read(self, relative):
        import io

        with io.open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
            return handle.read()

    def test_both_entries_disable_redis(self):
        for relative in self.FILES:
            self.assertIn('"redis_enabled"', self._read(relative), relative)

    def test_they_set_it_to_False_not_merely_mention_it(self):
        for relative in self.FILES:
            text = self._read(relative)
            self.assertTrue(
                'BIGQMT_REDIS_CONFIG["redis_enabled"] = False' in text
                or '"redis_enabled": False' in text,
                relative)

    def test_they_still_force_zmq(self):
        """The new line must not have displaced the existing one."""
        for relative in self.FILES:
            self.assertIn('"transport"] = "zmq"', self._read(relative), relative)

    def test_the_entry_says_what_it_costs(self):
        """A silent loss of strategy_name backfill is how #133 gets reopened."""
        self.assertIn("strategy_name", self._read(self.FILES[0]))


if __name__ == "__main__":
    unittest.main()
