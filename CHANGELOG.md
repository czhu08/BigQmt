# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 和 [语义化版本](https://semver.org/)。

## [0.2.1] - 2026-08-17

### 修复

- **正常下单误报 on_order_error(-1)**（Issue #38）：passorder 提交成功但委托号异步分配，客户端把「暂无 order_sys_id」误判为失败。服务端 `_handle_submit_order` 按唯一 `user_order_id`(remark) 匹配并回填 `order_sys_id`；顺带修掉校验代码对无 `.get()` 方法的 `OrderSnapshot` 调 `.get()` 的死代码（server_error 之前从未生效）。客户端 `call()` 不再丢弃 `server_error`，委托未进系统时转成异常，`order_stock_async` 携带真实原因回调 `on_order_error`。实盘验证：async 下单回调带真实委托号、提交阶段零误报（302 个测试通过，新增 5 个）。
- **query_stock_orders 查不到委托**（strategy_name 陷阱）：客户端别名默认 `"bigqmt_signal_trader"` 与服务端默认 `""` 不一致，改用其他策略名下单后别名查询返回空。默认改 `""`（返回全部）并对齐测试。

### 新增

- **qmt-trader skill 首次部署引导**：客户端装包、QMT 端文件同步、私有配置模板、入口启动验证、部署排错速查，零上下文也能从零跑通。
- **PyPI 发布**：`BIGQMT_REDIS_DRYRUN` 入口模块补进 py-modules，`pip install xtquant-big-convert` 即可获得完整包（wheel/sdist 均通过 twine check）。

### 变更

- README 头部加 PyPI / Python 版本 / License 徽章，新增「AI 助手 Skill：qmt-trader」专节（启用方式、命令概览、安全设计）。

---

## [0.2.0] - 2026-08-15

### 新增（Features）

- **qmt-trader skill**：统一 CLI 驱动全部 QMT API（`qmt-trader/scripts/qmt.py`），46 个子命令覆盖行情/持仓/委托/下单/撤单/财务/期权/两融/北向/龙虎榜等，含通用 `rpc` 兜底命令 + 25 个高频快捷命令。
- **异步回报回调**：`XtQuantTraderCallback` 全链路（`on_account_status` / `on_order_stock_async_response` / `on_stock_order` / `on_stock_trade` / `on_order_error` / `on_cancel_error` / `on_cancel_order_stock_async_response`），对齐 MiniQMT 原生语义，实盘验证。
- **全推行情订阅**（`subscribe_whole_quote` 真推送）：服务端引用计数管理 + PUB/SUB 数据面通道（redis/zmq）+ 客户端心跳 + 推送静默检测 + 服务端重启恢复。
- **完整 xtconstant 枚举**：91 个常量全量覆盖（账号类型/委托类型-股票期货信用期权/报价类型/委托状态/账号状态/`ORDER_TYPE_SET`），值对齐原生 MiniQMT。
- **文件日志系统**（`logging_setup.py`）：TimedRotatingFileHandler 按天轮转、保留 7 天（`BIGQMT_LOG_RETENTION_DAYS` 可配），双输出（文件 + QMT 面板），线程安全。
- **启动自动诊断**：`init()` 打印服务状态、关键函数绑定、行情链路，方便排错。
- **server_error 字段**：`submit_order` 校验委托是否进系统，静默失败时返回原因给客户端。
- **统一测试入口**：`run_all_tests.py` 分组跑全部测试（signal_trader 274 + backtest 16）。
- **端到端测试**：`test_all_apis.py` 验证真实 QMT 返回（transport 一致性/持仓空/委托空/下单未进系统/server_error）。
- **生产失败场景单元测试**：7 个测试覆盖返回空/全 0/拒绝的 QMT 边界（非 happy-path）。
- **官方交易查询函数**：`get_value_by_order_id` / `get_last_order_id` / `get_ipo_data` / `get_new_purchase_limit` / `get_history_trade_detail_data` / 融资融券 5 个 / 期权持仓 2 个 / 港股通汇率。
- **无 redis 版本**（`bigqmt_no_redis/`）：自包含 ZMQ transport + 无 redis DRYRUN，解决 QMT 沙箱 `import redis` 报错。
- **多账号使用文档**：README 加「多账号使用」章节（多策略实例 + 多 client）。
- **MiniQMT→BigQMT 转换 skill**：docs + scripts + templates（PR #37）。

### 修复（Bug Fixes）

- **QMT 自动退出**：`ZmqQuotePushChannel.stop()` 跨线程关 SUB socket 触发 Windows signaler abort → 进程崩溃。改为订阅线程自己关 socket。
- **QMT 自动退出（系列）**：`_adjust_phase` 无 except（redis 故障崩策略）、`_publish_response` 逃出、deal_callback/forward_order_event/forward_trade_event/sync_positions_app 无防护、pending 队列满（queue.Full）、init() 无防护、socket_timeout=None 永久阻塞主线程、reset_app 不清理 quote-push/whole-quote（重启泄漏）、exec 事件每次回调新建 redis client（连接池泄漏）。
- **download_history_data 下载不了**（Issue #32）：`download_history_data` 是 QMT 全局函数不是 ContextInfo 方法，改走 `qmt_api` 注入。
- **复权数据返回全 0**（front/back）：服务端需先下载原始数据 + 除权因子。下载类（`download_history_data2`）自动预下载；读取类（`get_market_data_ex`/`get_market_data`）自愈（检测全 0 → 服务端下载 → 重试）。
- **卖出方向误判**（exec_events）：QMT 回调 `m_nDirection` 恒为 48，改仲裁链（offset_flag > direction > op_type）。
- **query_orders/query_trades 返回空**：`strategy_name` 过滤不匹配，默认改 `""` 返回全部。
- **get_financial_data 返回 None**：参数顺序错误（stock_list/table_list 反了）。
- **position_events 内存无限增长**（Issue #21）：xadd 无 maxlen，加 maxlen=2000。
- **异步回调签名错误**：`on_order_stock_async_response`/`on_cancel_order_stock_async_response` 原生签名 1 参数（response 带 seq），之前传 2 参数导致 TypeError 被吞。
- **order_stock 返回 -1**：`order_stock_async` 调 `result.get()` 崩，改为触发 `on_order_error`。
- **客户端 transport 不匹配**（Issue #24）：`query_stock_asset` 返回 None 的根因是客户端 redis / 服务端 zmq 不匹配。
- **DRYRUN 硬编码路径**：`_known_qmt_python_dir` 改 sys.path 扫描（paste-run 模式）。
- **ZMQ bind 冲突提示**：加端口占用检测 + 解决步骤提示。

### 变更（Changed）

- 包发布：`pip install xtquant-big-convert`（pyproject.toml 完善元数据 + LICENSE）。
- README 重写：依赖安装分客户端/服务端、API 总览、传输层对比、FormulaServer 直连、异步回调、无 redis 版本、日志排错、多账号、复权陷阱等章节。

---

## [0.1.0] - 2026-07-02

初始版本：Big QMT Redis RPC 桥接 + MiniQMT 兼容层。

### 新增

- Redis RPC 服务（rpush/blpop/brpop）+ 可插拔传输层（redis/zmq/mysql/shm）。
- 客户端兼容层（`xtquant_compat`）：`xt_trader` / `xtdata` 方法名映射。
- 行情/持仓/委托/下单基础 RPC 接口。
- `BIGQMT_REDIS_DRYRUN.py` QMT 编辑器入口。
