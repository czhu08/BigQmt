import os
import shutil
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.local_cache import LocalMarketCache


def _has_pyarrow():
    try:
        import pyarrow  # noqa: F401

        return True
    except Exception:
        return False


class LocalMarketCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_read_merge_dedupe(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("600000.SH", "1d", pd.DataFrame({"stime": ["20260101", "20260102"], "close": [1.0, 2.0]}))
        # overlapping second write: 20260102 should be replaced (keep last), 20260103 appended
        c.write("600000.SH", "1d", pd.DataFrame({"stime": ["20260102", "20260103"], "close": [2.5, 3.0]}))

        df = c.read("600000.SH", "1d")
        self.assertEqual(list(df["stime"]), ["20260101", "20260102", "20260103"])
        self.assertEqual(df[df["stime"] == "20260102"]["close"].iloc[0], 2.5)

    def test_range_and_count_filters(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102", "20260103"], "close": [1, 2, 3]}))

        self.assertEqual(list(c.read("X", "1d", start_time="20260102")["stime"]), ["20260102", "20260103"])
        self.assertEqual(list(c.read("X", "1d", end_time="20260102")["stime"]), ["20260101", "20260102"])
        self.assertEqual(list(c.read("X", "1d", count=1)["stime"]), ["20260103"])
        self.assertIsNone(c.read("MISSING", "1d"))
        self.assertEqual(c.covered("X", "1d"), ("20260101", "20260103", 3))

    def test_drops_zero_fill_placeholder_rows(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        df = pd.DataFrame(
            {"stime": ["20200101", "20200102", "20260701"], "close": [0.0, 0.0, 8.65], "open": [0.0, 0.0, 8.58]}
        )
        c.write("X", "1d", df)
        self.assertEqual(list(c.read("X", "1d")["stime"]), ["20260701"])  # 0-fill dropped

        # an all-placeholder write must not create/overwrite a cache file
        self.assertEqual(c.write("Y", "1d", pd.DataFrame({"stime": ["20200101"], "close": [0.0]})), 0)
        self.assertIsNone(c.read("Y", "1d"))

    def test_dividend_type_keeps_separate_caches(self):
        import pandas as pd

        c = LocalMarketCache(self.dir)
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101"], "close": [10.0]}), dividend_type="none")
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101"], "close": [9.0]}), dividend_type="front")

        self.assertEqual(c.read("X", "1d", dividend_type="none")["close"].iloc[0], 10.0)
        self.assertEqual(c.read("X", "1d", dividend_type="front")["close"].iloc[0], 9.0)
        self.assertIsNone(c.read("X", "1d", dividend_type="back"))

    def test_pickle_format_roundtrip(self):
        import pandas as pd

        c = LocalMarketCache(self.dir, fmt="pkl")
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102"], "close": [1.0, 2.0]}))
        self.assertTrue(c.path("X", "1d").endswith(".pkl"))
        self.assertEqual(list(c.read("X", "1d")["close"]), [1.0, 2.0])

    @unittest.skipUnless(_has_pyarrow(), "pyarrow not installed")
    def test_parquet_format_roundtrip(self):
        import pandas as pd

        c = LocalMarketCache(self.dir, fmt="parquet")
        c.write("X", "1d", pd.DataFrame({"stime": ["20260101", "20260102"], "close": [1.0, 2.0]}))
        self.assertTrue(c.path("X", "1d").endswith(".parquet"))
        self.assertEqual(list(c.read("X", "1d")["close"]), [1.0, 2.0])

    @unittest.skipUnless(_has_pyarrow(), "pyarrow not installed")
    def test_migrates_pickle_to_parquet(self):
        import pandas as pd

        LocalMarketCache(self.dir, fmt="pkl").write("X", "1d", pd.DataFrame({"stime": ["20260101"], "close": [1.0]}))
        pq = LocalMarketCache(self.dir, fmt="parquet")
        self.assertEqual(list(pq.read("X", "1d")["close"]), [1.0])  # reads the old pkl
        pq.write("X", "1d", pd.DataFrame({"stime": ["20260102"], "close": [2.0]}))
        self.assertTrue(os.path.isfile(pq.path("X", "1d")))  # parquet now exists
        self.assertFalse(os.path.isfile(pq.path("X", "1d")[:-8] + ".pkl"))  # old pkl removed
        self.assertEqual(list(pq.read("X", "1d")["close"]), [1.0, 2.0])  # merged across formats


class FakeClient:
    def __init__(self, cache_dir, fallback_rpc=False):
        self.account_id = "acct"
        self.calls = []
        self.call_params = []
        self.local_cache_config = {"enabled": True, "dir": cache_dir, "fallback_rpc": fallback_rpc}

    def _redis(self):
        return None

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append(method)
        self.call_params.append((method, params))
        if method == "get_market_data_ex":
            import pandas as pd

            codes = (params or {}).get("stock_list") or []
            return {c: pd.DataFrame({"stime": ["20260626", "20260629"], "close": [8.76, 8.73]}) for c in codes}
        if method == "download_history_data2":
            # Server-side raw download (raw bars + dividend factors).
            return True
        raise AssertionError("unexpected rpc: %s" % method)


class LocalCacheClientTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _xt(self, fallback_rpc=False):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        return BigQmtXtData(FakeClient(self.dir, fallback_rpc=fallback_rpc))

    def test_download_caches_then_get_local_reads_without_rpc(self):
        xt = self._xt()
        progress = []
        res = xt.download_history_data2(["600000.SH", "000001.SZ"], "1d", callback=lambda d: progress.append(d))

        self.assertEqual(res, {"finished": 2, "total": 2})
        self.assertEqual(len(progress), 2)
        self.assertEqual(progress[-1]["stockcode"], "000001.SZ")
        self.assertEqual(progress[-1]["finished"], 2)
        calls_after_download = list(xt.client.calls)

        data = xt.get_local_data(stock_list=["600000.SH", "000001.SZ"], period="1d")

        self.assertIn("600000.SH", data)
        self.assertIn("000001.SZ", data)
        self.assertEqual(list(data["600000.SH"]["close"]), [8.76, 8.73])
        # get_local_data must NOT issue any further RPC — pure local read.
        self.assertEqual(xt.client.calls, calls_after_download)

    def test_get_market_data_ex_caches_through(self):
        xt = self._xt()
        # a plain live read must also populate the cache (cache-through)
        xt.get_market_data_ex(field_list=["close"], stock_list=["600000.SH"], period="1d")
        n = len(xt.client.calls)

        data = xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertIn("600000.SH", data)
        self.assertEqual(len(xt.client.calls), n)  # served from cache, no extra RPC

    def test_get_local_miss_returns_empty_and_no_rpc(self):
        xt = self._xt()
        data = xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertEqual(data, {})
        self.assertEqual(xt.client.calls, [])

    def test_get_local_fallback_rpc_fetches_and_caches(self):
        xt = self._xt(fallback_rpc=True)
        data = xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertIn("600000.SH", data)
        self.assertIn("get_market_data_ex", xt.client.calls)  # fetched on miss
        # second read is served from cache — no new RPC
        n = len(xt.client.calls)
        xt.get_local_data(stock_list=["600000.SH"], period="1d")
        self.assertEqual(len(xt.client.calls), n)


class AdjustedDownloadTest(unittest.TestCase):
    """Adjusted (front/back) downloads must trigger the server-side raw
    download FIRST: Big QMT computes adjusted bars from raw bars + dividend
    factors, and without the server-side download the adjusted result is
    all zeros (verified live with 600654.SH)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _xt(self):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        return BigQmtXtData(FakeClient(self.dir))

    def test_front_download_triggers_server_side_raw_download_first(self):
        xt = self._xt()
        xt.download_history_data2(["600000.SH"], "1d", start_time="20200101", dividend_type="front")

        # The server-side raw download must run BEFORE the adjusted pull.
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertIn("download_history_data2", method_calls)
        self.assertIn("get_market_data_ex", method_calls)
        self.assertLess(
            method_calls.index("download_history_data2"),
            method_calls.index("get_market_data_ex"),
            "server-side raw download must precede the adjusted pull",
        )
        # The raw download carries the same codes/period/window.
        raw_call = next(p for m, p in xt.client.call_params if m == "download_history_data2")
        self.assertEqual(raw_call["stock_list"], ["600000.SH"])
        self.assertEqual(raw_call["period"], "1d")
        self.assertEqual(raw_call["start_time"], "20200101")

    def test_none_download_skips_server_side_raw_download(self):
        xt = self._xt()
        xt.download_history_data2(["600000.SH"], "1d", dividend_type="none")

        # Unadjusted pulls need no server-side download.
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertNotIn("download_history_data2", method_calls)
        self.assertIn("get_market_data_ex", method_calls)

    def test_front_download_survives_server_download_failure(self):
        # Deployments without the QMT global must still get the adjusted pull
        # (best-effort raw download, never fatal).
        xt = self._xt()
        original_call = xt.client.call

        def failing_download(method, params=None, account_id=None, timeout_seconds=None):
            if method == "download_history_data2":
                raise RuntimeError("global not available")
            return original_call(method, params, account_id=account_id, timeout_seconds=timeout_seconds)

        xt.client.call = failing_download
        result = xt.download_history_data2(["600000.SH"], "1d", dividend_type="front")
        self.assertEqual(result, {"finished": 1, "total": 1})  # adjusted pull still ran


class _AllZeroThenRealClient(FakeClient):
    """First adjusted get_market_data_ex returns all-zero bars (server lacks
    raw data); after a server-side raw download, subsequent pulls are real."""

    def __init__(self, cache_dir):
        super(_AllZeroThenRealClient, self).__init__(cache_dir)
        self._downloaded = False

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        self.calls.append(method)
        self.call_params.append((method, params))
        import pandas as pd

        if method == "download_history_data2":
            self._downloaded = True
            return True
        if method == "get_market_data_ex":
            codes = (params or {}).get("stock_list") or []
            if self._downloaded:
                return {c: pd.DataFrame({"stime": ["20260626", "20260629"], "close": [8.76, 8.73]}) for c in codes}
            # all-zero symptom: head zeros, last bar live
            return {c: pd.DataFrame({"stime": ["20260626", "20260629"], "close": [0.0, 8.73]}) for c in codes}
        raise AssertionError("unexpected rpc: %s" % method)


class AdjustedReadSelfHealTest(unittest.TestCase):
    """Reading adjusted bars that come back all-zero must self-heal:
    trigger a server-side raw download, wait, and retry once."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _xt(self):
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        return BigQmtXtData(_AllZeroThenRealClient(self.dir))

    def test_front_read_self_heals_all_zero_to_real(self):
        xt = self._xt()
        data = xt.get_market_data_ex(
            field_list=["close"], stock_list=["600000.SH"], period="1d",
            dividend_type="front",
        )
        # After self-heal the retry returns real (non-zero) bars.
        self.assertEqual(list(data["600000.SH"]["close"]), [8.76, 8.73])
        # The heal path must have triggered a server-side raw download.
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertIn("download_history_data2", method_calls)
        # get_market_data_ex called twice: initial all-zero pull + retry.
        self.assertEqual(method_calls.count("get_market_data_ex"), 2)

    def test_none_read_does_not_self_heal(self):
        xt = self._xt()
        data = xt.get_market_data_ex(
            field_list=["close"], stock_list=["600000.SH"], period="1d",
            dividend_type="none",
        )
        # none returns whatever the server sent (no heal, single pull).
        self.assertEqual(list(data["600000.SH"]["close"]), [0.0, 8.73])
        method_calls = [m for m, _ in xt.client.call_params]
        self.assertNotIn("download_history_data2", method_calls)
        self.assertEqual(method_calls.count("get_market_data_ex"), 1)


if __name__ == "__main__":
    unittest.main()
