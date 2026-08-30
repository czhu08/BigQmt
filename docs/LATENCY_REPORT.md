# 延迟测试报告

> 测试环境为同一台本地机器和同一套 QMT 环境，仅用于判断量级，不代表固定承诺。
> 最后更新：2026-08-30（v0.3.0）

## 方法论

- **端到端**：客户端发起 RPC → 传输层 → QMT 策略处理 → 返回，含全部编解码和网络开销。
- **样本量**：每种方法 n=15~100。
- **交易时段影响**：非交易时间、午间休市或 QMT 刚启动时，行情源、柜台连接和回调节奏不活跃，请求耗时明显高于交易时段。本文数字如未注明均为交易时段测得。
- **QMT 主线程 GIL**：读取类请求在 QMT 主线程（adjust/drain）上串行处理，与行情回调共享 GIL——这是 ZMQ 尖峰的来源，不是网络问题。

## 传输层对比（端到端，真实 QMT 进程）

| 传输 | ping p50 | get_full_tick p50 | 成功率 | 尖峰来源 |
|------|---------|------------------|--------|---------|
| **Redis** | 13ms | 15ms | 100% | 偶发 245ms（网络抖动） |
| **ZMQ**（同机） | 0.7ms* | 0.7ms* | 100% | 30% 请求撞 ~500ms（QMT adjust GIL 调度） |
| **MySQL** | 104ms | 110ms | 100% | 轮询开销 |

\* ZMQ fast-path（避开 GIL 尖峰的请求）；overall p90 ~498ms。

**结论**：生产推荐 **Redis**（稳定、跨机、无 GIL 问题、QMT 端零额外依赖）。ZMQ 理论最快但受 GIL 调度影响，适合同机且能接受偶发尖峰的场景。MySQL 仅作兜底。

## FormulaServer 直连快速路径（只读行情）

大 QMT 的 FormulaServer（58600 端口）可以绕过策略进程直接读行情：

| 路径 | 典型耗时 | 说明 |
|------|---------|------|
| FormulaServer 直连 | **~0.07ms** | 仅 dividend_type=none 的只读方法（10 个） |
| Redis RPC 同方法 | ~13ms | 走策略进程 |

约 **180 倍**差距。默认开启，失败自动回落 RPC 桥。tick/L2 周期和复权读取不在此路径（v0.2.9 起拒绝路由，由 RPC 桥正确回答）。

## 下单链路

| 环节 | 耗时 | 说明 |
|------|------|------|
| `order_stock_async` 返回 seq | **<1ms** | 本地排队，不碰网络（#50） |
| 提交到 QMT（passorder 执行） | RPC 单跳（zmq ~1ms / redis ~13ms）+ adjust 等待 | 下单 RPC 在 QMT 主线程串行处理 |
| 委托号回填（结算） | 通常 <1s | 停放应答 + adjust tick 轮询（#44），有 deadline 兜底 |
| `on_order_stock_async_response` 回调 | 提交后 ~0.3~2s | 等屏障从推送事件学到委托号（#72，bounded 2s） |
| `on_stock_order`/`on_stock_trade` 推送 | 柜台回报后 ~ms 级 | Redis pub/sub，保序在 async_response 之后（#51） |

**注意**：下单类 RPC 无法并行化——`get_trade_detail_data` 离开 QMT 主线程返回空，结算只能在 adjust tick 上做。批量提交用 `order_stock_batch`；读密集流量用 `call_async`（v0.2.6+）叠加往返延迟。

## 复现

```powershell
python bench_latency.py            # Redis 单传输延迟
python bench_transports.py -n 100  # Redis vs ZMQ 对比
python bench_zmq_spike.py          # ZMQ GIL 尖峰分析
```

## 与社区方案的量级参考

cfquant（named pipe 同机桥）公布的数字：普通 QMT 下单 ~176ms、ctypes 交易通道 ~20ms、极速交易端 ~1ms。与本项目不同，其下单走独立通道脚本而非主策略线程；本项目下单经 QMT 主策略进程（受 adjust 节奏约束），读路径有 FormulaServer 直连 0.07ms 的优势项。两边数字的测量口径不同（其报告为 2026-08-13 实测，见对方仓库 docs/），仅作量级参考。
