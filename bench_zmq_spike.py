# coding: utf-8
"""Measure ZMQ GIL-spike rate at different request rates.

Sends N get_full_tick requests at a fixed interval, records per-call
latency, reports how many exceed thresholds (50/200/500/1000ms).
"""
import sys
import time

sys.path.insert(0, r"D:\gjzqqmt\xtquant_big_convert\src")
sys.path.insert(0, r"D:\国金证券QMT交易端_lemo\python")

import bigqmt_signal_trader.xtquant_compat as compat

compat.configure()
client = compat.get_default_client()
print("account:", client.account_id, "| transport:", client.transport_name)
print("=" * 60)

N = 40
INTERVAL_MS = 50  # 请求间隔 50ms = 20 QPS

latencies = []
for i in range(N):
    t0 = time.time()
    try:
        client.call("get_full_tick", {"codes": ["000001.SZ"]})
        ms = (time.time() - t0) * 1000
        latencies.append(ms)
    except Exception as e:
        ms = (time.time() - t0) * 1000
        latencies.append(ms)
        print("  [%2d] FAIL %.0fms %s" % (i, ms, str(e)[:40]))
    # 控制频率
    elapsed = (time.time() - t0)
    sleep = max(0, INTERVAL_MS / 1000.0 - elapsed)
    if sleep > 0:
        time.sleep(sleep)

latencies.sort()
n = len(latencies)
print("\n=== %d requests @ %dms interval (%.0f QPS) ===" % (n, INTERVAL_MS, 1000.0/INTERVAL_MS))
print("min=%.1f  p50=%.1f  p90=%.1f  p99=%.1f  max=%.1f" % (
    latencies[0], latencies[n//2], latencies[int(n*0.9)], latencies[int(n*0.99)], latencies[-1]))

# 尖峰分布
thresholds = [10, 50, 100, 200, 500, 1000]
print("\n=== 延迟分布 ===")
for t in thresholds:
    cnt = sum(1 for l in latencies if l > t)
    print("  >%5dms : %2d / %d  (%.0f%%)" % (t, cnt, n, 100.0*cnt/n))
