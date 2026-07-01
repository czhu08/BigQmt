# xtquant_big_convert

大 QMT 运行环境里的 Redis RPC 桥接包，用于把大 QMT 内置 Python 能力封装成可替换的交易/查询适配层，并兼容一组常用 MiniQMT 方法名。

## 能力

- 在大 QMT 策略进程中启动 Redis Pub/Sub RPC 服务。
- 查询资产、持仓、委托、成交、tick 和合约详情。
- 兼容 `query_stock_asset`、`query_stock_positions`、`query_stock_orders`、`query_stock_trades`、`get_full_tick`、`order_stock` 等 MiniQMT 常用方法名。
- 提供 `bigqmt_signal_trader.xtquant_compat` 客户端兼容层，可把原来的 `xt_trader` / `xtdata` 调用转成 Redis RPC。
- 提供可选 `src/xtquant/` shim，可让旧代码的 `from xtquant import xtdata, xtconstant` 命中本仓库兼容实现。
- 默认只读，`order_stock` / `cancel_order_stock_sysid` 等下单撤单接口默认关闭。
- 提供 dry-run 信号消费、Redis 状态写回和持仓同步骨架。

## 目录

- `src/bigqmt_signal_trader/`：核心包和适配器。
- `src/bigqmt_signal_trader/xtquant_compat.py`：MiniQMT 风格客户端兼容层。
- `src/xtquant/`：可选的 `xtquant` import shim。
- `src/bigqmt_signal_trader_strategy.py`：大 QMT 策略基础入口。
- `src/bigqmt_signal_trader_redis_rpc_runtime.py`：只启用 Redis RPC 的大 QMT 入口。
- `src/bigqmt_signal_trader_redis_dryrun.py`：Redis 信号 dry-run 入口。
- `tests/bigqmt_signal_trader/`：无 QMT 环境也能运行的单元测试。
- `docs/`：运行说明和 RPC 协议。

## 快速用法

### 1. 大 QMT 侧

把 `src/bigqmt_signal_trader/`、`src/bigqmt_signal_trader_strategy.py`、`src/bigqmt_signal_trader_redis_rpc_runtime.py` 同步到大 QMT 的 `python` 目录。

在大 QMT 本地创建私有配置文件：

```python
# <QMT_PYTHON_DIR>\bigqmt_signal_trader_local_config.py
BIGQMT_ACCOUNT_ID = "1234567890"

BIGQMT_REDIS_CONFIG = {
    "host": "192.168.1.100",
    "port": 63790,
    "db": 5,
    "username": "",
    "password": "******",
    "rpc_allow_order_methods": False,
}
```

然后在大 QMT 策略编辑器里运行 `bigqmt_signal_trader_redis_rpc_runtime.py` 对应入口。详细入口脚本见 [docs/BIG_QMT_REDIS_RPC.md](docs/BIG_QMT_REDIS_RPC.md)。

### 2. 原策略侧灰度接入

灰度阶段建议显式导入兼容层，不覆盖机器上的真实 `xtquant`：

```python
from bigqmt_signal_trader.xtquant_compat import StockAccount, configure, xt_trader, xtdata

configure(
    account_id="1234567890",
    redis_config={
        "host": "192.168.1.100",
        "port": 63790,
        "db": 5,
        "username": "",
        "password": "******",
    },
)

acc = StockAccount("1234567890", "STOCK")
asset = xt_trader.query_stock_asset(acc)
positions = xt_trader.query_stock_positions(acc)
ticks = xtdata.get_full_tick(["600000.SH"])
```

### 3. 最终无损替换

最终切换时，把本仓库 `src` 放到 `PYTHONPATH` 最前面：

```powershell
$env:PYTHONPATH = "D:\gjzqqmt\xtquant_big_convert\src;$env:PYTHONPATH"
$env:BIGQMT_ACCOUNT_ID = "1234567890"
$env:BIGQMT_REDIS_HOST = "192.168.1.100"
$env:BIGQMT_REDIS_PORT = "63790"
$env:BIGQMT_REDIS_DB = "5"
$env:BIGQMT_REDIS_PASSWORD = "******"
```

这样旧代码里的下面这些 import 可以保持不变：

```python
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtdata, xtconstant
```

完整替换说明见 [docs/XTQUANT_COMPAT_REPLACEMENT.md](docs/XTQUANT_COMPAT_REPLACEMENT.md)。

## 本地测试

```powershell
python -B -m unittest discover -s tests\bigqmt_signal_trader
```

当前测试覆盖 47 个用例。

## QMT 本地配置

复制配置样例到 QMT 的 `python` 目录，并改成真实配置：

```text
src/bigqmt_signal_trader_local_config.example.py
```

目标文件名：

```text
bigqmt_signal_trader_local_config.py
```

真实配置文件不要提交。里面可能包含资金账号和 Redis 密码。

## 运行入口

在大 QMT 策略编辑器里建议使用 `docs/BIG_QMT_REDIS_RPC.md` 中的 reload 入口脚本。这样更新包文件后，重新运行策略即可刷新 `redis_rpc` 子模块，避免 QMT 进程缓存旧白名单。

## 安全默认值

`rpc_allow_order_methods` 默认为 `False`。此时远程调用 `order_stock` 会被拒绝，适合先上线查询和持仓同步链路。只有确认接入方、账号和风控后，再在本地私有配置里显式打开。
