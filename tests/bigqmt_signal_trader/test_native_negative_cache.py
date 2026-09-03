# coding: utf-8
"""A dead native xtdata SDK must not be re-dialed on every call (#160).

@heimo88's zmq slow-handler log: ``get_trading_dates 21644ms`` on the first
call after a strategy start. Measured on the live Guojin bridge: even warm,
EVERY get_trading_dates call cost ~2.1s because the handler re-tried the
native SDK, which blocks dialing its (nonexistent) quote service before
raising, then falls back to ContextInfo anyway.

In Big QMT the SDK never gains a quote service mid-process, so a native
failure is now remembered per function for NATIVE_FAILURE_CACHE_SECONDS;
calls in the window go straight to ContextInfo, and a success clears the
mark.
"""

import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider


class _FakeNativeModule(object):
    """Stands in for the xtdata SDK: records calls, raises or answers."""

    def __init__(self, fail=True):
        self.fail = fail
        self.calls = []

    def get_trading_dates(self, *args):
        self.calls.append(args)
        if self.fail:
            raise RuntimeError("无法连接行情服务")
        return ["20260901", "20260902"]


class _FakeContext(object):
    def get_trading_dates(self, *args):
        return ["20260901", "20260902", "20260903"]


class NativeNegativeCacheTest(unittest.TestCase):
    def _provider(self, native):
        return BigQmtMarketDataProvider(_FakeContext(), native_xtdata=native)

    def test_failed_native_is_not_retried_within_the_window(self):
        native = _FakeNativeModule(fail=True)
        provider = self._provider(native)

        first = provider.get_trading_dates("SH", "", "", 5)
        second = provider.get_trading_dates("SH", "", "", 5)

        self.assertEqual(first, ["20260901", "20260902", "20260903"])
        self.assertEqual(second, ["20260901", "20260902", "20260903"])
        self.assertEqual(len(native.calls), 1,
                         "the dead SDK must be dialed once, not per call")

    def test_cache_is_per_function(self):
        native = _FakeNativeModule(fail=True)
        native.get_holidays = lambda: ["20261001"]
        provider = self._provider(native)

        provider.get_trading_dates("SH", "", "", 5)   # fails -> marked
        holidays = provider.get_holidays()            # different fn -> tried

        self.assertEqual(holidays, ["20261001"])
        self.assertEqual(len(native.calls), 1)

    def test_success_clears_the_mark(self):
        native = _FakeNativeModule(fail=True)
        provider = self._provider(native)

        provider.get_trading_dates("SH", "", "", 5)   # fails -> marked
        native.fail = False
        # Pretend the window expired so the native path is re-probed.
        provider._native_dead_marks_dict["get_trading_dates"] -= (
            provider.NATIVE_FAILURE_CACHE_SECONDS + 1)

        result = provider.get_trading_dates("SH", "", "", 5)

        self.assertEqual(result, ["20260901", "20260902"])
        self.assertNotIn("get_trading_dates", provider._native_dead_marks_dict)

    def test_window_expiry_reprobes(self):
        native = _FakeNativeModule(fail=True)
        provider = self._provider(native)

        provider.get_trading_dates("SH", "", "", 5)
        provider._native_dead_marks_dict["get_trading_dates"] -= (
            provider.NATIVE_FAILURE_CACHE_SECONDS + 1)
        provider.get_trading_dates("SH", "", "", 5)

        self.assertEqual(len(native.calls), 2, "an expired mark must re-probe")

    def test_no_sdk_goes_straight_to_context_without_marking(self):
        provider = BigQmtMarketDataProvider(_FakeContext(), native_xtdata=None)
        provider._native_xtdata = None
        # _native() would try a real import; force "no SDK" by stubbing it.
        provider._native = lambda: None

        result = provider.get_trading_dates("SH", "", "", 5)

        self.assertEqual(result, ["20260901", "20260902", "20260903"])
        self.assertEqual(provider._native_dead_marks(), {})


if __name__ == "__main__":
    unittest.main()
