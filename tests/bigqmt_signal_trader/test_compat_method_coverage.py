"""兼容层不能漏掉服务端已经支持的 MiniQMT 方法（issue #130）。

#130 的现象是 `xtdata.download_sector_data()` 抛 AttributeError。查下来服务端
适配器和 RPC 白名单里一直都有这个方法，缺的只是客户端 BigQmtXtData 上那层包装。
同一类缺口当时一共 6 个，光靠人工比对迟早会再漏。

所以这里钉一条不变式：

    白名单（MARKET_DATA_METHODS）里的每个方法，要么兼容层有同名包装，
    要么明确写进 CALL_METHOD_ONLY —— 二选一，不能悄悄漏掉。

`CALL_METHOD_ONLY` 是大 QMT 独有的 ContextInfo 扩展，MiniQMT 本来就没有这些
方法，按设计走 `xtdata.call_method()` 兜底（README「通用 RPC 兜底」一节）。
往白名单加新方法时，这个测试会强制你做一次选择。

注意不要用 `hasattr(xtquant.xtdata, name)` 当判据：本仓库 `src/xtquant/` 有个
shim，测试里 `from xtquant import xtdata` 命中的是兼容层自己，那样断言等于
自己跟自己比，恒真。
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import MARKET_DATA_METHODS
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData, BigQmtXtTrader


# 大 QMT 独有能力（ContextInfo 扩展），MiniQMT 没有对应方法，不需要同名包装。
# 调用方式：xtdata.call_method("get_float_caps", stockcode="000001.SZ")
CALL_METHOD_ONLY = frozenset({
    "get_ETF_list",
    "get_bvol",
    "get_contract_expire_date",
    "get_contract_multiplier",
    "get_float_caps",
    "get_last_close",
    "get_last_volume",
    "get_open_date",
    "get_risk_free_rate",
    "get_svol",
    "get_total_share",
    "get_turn_over_rate",
    "get_weight_in_index",
})


def _exposed(name):
    return hasattr(BigQmtXtData, name) or hasattr(BigQmtXtTrader, name)


class MethodCoverageTest(unittest.TestCase):
    def test_every_whitelisted_method_is_wrapped_or_declared_call_method_only(self):
        missing = sorted(name for name in MARKET_DATA_METHODS
                         if not _exposed(name) and name not in CALL_METHOD_ONLY)
        self.assertEqual(
            missing, [],
            "服务端白名单里有，但兼容层既没包装也没声明为 call_method 专用：%s\n"
            "MiniQMT 也有的方法 -> 补一个 self._call(\"<name>\", ...) 包装；\n"
            "大 QMT 独有的能力 -> 加进本文件的 CALL_METHOD_ONLY 并说明。\n"
            "（issue #130 就是漏了包装撞出来的 AttributeError）" % missing)

    def test_call_method_only_entries_are_really_in_the_whitelist(self):
        # 清单过期了要能发现：方法从白名单删掉后，这里也该跟着删
        stale = sorted(name for name in CALL_METHOD_ONLY
                       if name not in MARKET_DATA_METHODS)
        self.assertEqual(stale, [], "CALL_METHOD_ONLY 里有白名单已经没有的方法：%s" % stale)

    def test_call_method_only_entries_are_not_secretly_wrapped(self):
        # 反过来：包装上了就该从清单里挪走，免得清单变成谎话
        wrapped = sorted(name for name in CALL_METHOD_ONLY if _exposed(name))
        self.assertEqual(
            wrapped, [],
            "这些已经有包装了，应当从 CALL_METHOD_ONLY 移除：%s" % wrapped)

    def test_the_generic_fallback_exists(self):
        # CALL_METHOD_ONLY 成立的前提
        self.assertTrue(hasattr(BigQmtXtData, "call_method"))


class ReportedGapTest(unittest.TestCase):
    """issue #130 报的那批：服务端一直支持，客户端漏了包装。"""

    REPORTED = ("download_sector_data", "download_cb_data",
                "download_index_weight", "download_history_contracts",
                "get_stock_type", "subscribe_l2thousand")

    def test_all_of_them_are_now_exposed(self):
        for name in self.REPORTED:
            self.assertTrue(_exposed(name), name)

    def test_all_of_them_are_whitelisted_server_side(self):
        for name in self.REPORTED:
            self.assertIn(name, MARKET_DATA_METHODS, name)


class _Recorder(BigQmtXtData):
    """记录 _call 的参数，不真的发 RPC。"""

    def __init__(self):
        self.calls = []

    def _call(self, method, **params):
        self.calls.append((method, params))
        return "ok"


class DownloadWrapperTest(unittest.TestCase):
    def setUp(self):
        self.data = _Recorder()

    def test_download_methods_forward_their_own_name_without_params(self):
        for name in ("download_sector_data", "download_cb_data",
                     "download_index_weight"):
            self.data.calls = []
            self.assertEqual(getattr(self.data, name)(), "ok")
            self.assertEqual(self.data.calls, [(name, {})], name)

    def test_download_history_contracts_keeps_the_xtdata_signature(self):
        # xtdata 是 download_history_contracts(incrementally=True)；大 QMT 侧
        # 没有增量参数，形参保留只为签名一致，不往下转发
        self.assertEqual(self.data.download_history_contracts(), "ok")
        self.assertEqual(self.data.download_history_contracts(incrementally=False), "ok")
        self.assertEqual(
            self.data.calls,
            [("download_history_contracts", {}), ("download_history_contracts", {})])


class StockTypeWrapperTest(unittest.TestCase):
    """get_stock_type 是这批里唯一一个不能直接转发的。

    实盘验证发现服务端 ContextInfo.get_stock_type 对任何代码都返回 0
    （股票 / ETF / 债券 / 期权都一样），转发等于把一个假分类递给调用方。
    详见 test_get_stock_type_unavailable.py。
    """

    def test_it_refuses_instead_of_forwarding(self):
        data = _Recorder()

        with self.assertRaises(NotImplementedError):
            data.get_stock_type("600000.SH")

        self.assertEqual(data.calls, [])

class L2ThousandWrapperTest(unittest.TestCase):
    def setUp(self):
        self.data = _Recorder()

    def test_gear_num_defaults_to_zero_like_the_server(self):
        self.data.subscribe_l2thousand("600000.SH")
        self.assertEqual(
            self.data.calls,
            [("subscribe_l2thousand", {"stock_code": "600000.SH", "gear_num": 0})])

    def test_callback_is_accepted_but_not_forwarded(self):
        # RPC 模型下没有回调通道，服务端也会忽略；要推送用 subscribe_whole_quote
        self.data.subscribe_l2thousand("600000.SH", gear_num=5, callback=lambda d: None)
        self.assertEqual(
            self.data.calls,
            [("subscribe_l2thousand", {"stock_code": "600000.SH", "gear_num": 5})])


if __name__ == "__main__":
    unittest.main()
