# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 和 [语义化版本](https://semver.org/)。

## [0.3.1] - 2026-08-30

### 修复

- **信用委托类型被塌缩成普通买卖**（Issue #103）：`order_stock(acc, code, 27, ...)` 返回「不支持该类型」。有两层，第二层更危险：

  - RPC 只认 `23` / `24`，其余一律 `action or order_type is required`
  - 即使放行也没用——`submit()` 按 action 映射 opType，`BUY` 恒等于 `23`，**融资买入会被当成普通买入下出去**。一笔真实的、但下错品种的委托，比直接报错危险得多。

  数值本身是陷阱。MiniQMT 的 `order_type`（`xtconstant`）与 `passorder` 的 `opType` 是**两套编号**：

  | 含义 | xtconstant | passorder opType（API 参考 10.1） |
  |---|---|---|
  | 融资买入 … 直接还款 | 27–32 | 27–32（相同） |
  | 担保品买入 / 卖出 | — | 33 / 34 |
  | **专项两融** | **40–45** | **70–75** |

  所以 `40` 原样转发会到达 `passorder` 的**期货组合开多**。翻译不是可选项。

  所有数值**按常量名取自 `xtconstant`**，不写字面量——PR #88 正是把它们写成字面量、测试又编码了同一个错误前提，因而全绿而映射是错的。另有一条测试：`xtconstant` 若新增未覆盖的 `CREDIT_` 常量即变红，新类型是可见缺口而非静默透传。

  `直接还款`（32 / 45）移动现金而非证券，**没有买卖方向**——不猜，报错要求显式传 `action`。

- **长代码列表失败时全丢**（Issue #104）：一次 RPC 只带一个超时，装不下的列表不是降级而是全部丢失。实测 1000 个 0.42s、10000 个 2.87s、26744 个超时；报告人在 1000 附近就撞上——超时的落点取决于机器。

  现在长列表**失败或返回不全**时，改用市场令牌重读并筛选。重读**先窄后宽**：先按 `stock`（或调用方给的 `types=`）读（1.08s），不够才退到 `all`（7.4s）。

  做成兜底而非阈值切换：1000 个代码直接请求 0.42s，整市场 7.4s，无条件切换会把本来正常的请求拖慢 17 倍。短列表失败仍然抛出（小列表失败是桥坏了，不是尺寸问题）、短列表返回不全不重读（停牌/退市/未订阅本来就会缺）、无可识别后缀的代码不重读。

- **版本标记停在 0.2.16**：`pyproject` 与 CHANGELOG 已是 0.3.0，`version.py` 未跟上，于是 0.3.0 的部署把自己**报告成 0.2.16**——#103 的报告人正是照着这个数字确认版本的。`tests/test_version_stamp.py` 本就为此而写。

### 已知限制

- **信用委托未实盘验证**：会下真实的融资融券委托，本机无信用账户。@fengzhizialex 已表示可代为验证。
- **长列表兜底未实盘验证**：触发条件（超时、返回不全）均为单测模拟。
- **PyPI 曾落后于 GitHub**：0.2.10 到 0.2.15 只发了 GitHub Release、未上传 PyPI，那段时间 `pip install --upgrade` 取到的一直是 0.2.9。0.3.1 已上传。
- 其余同 0.3.0。

---

## [0.3.0] - 2026-08-29

### 新增

- **纯 ZMQ 编辑器入口 `BIGQMT_ZMQ_DRYRUN.py`**（PR #108，@amigobot）：无 redis 部署（券商白名单沙箱）专用入口，强制 ZMQ + 后台线程，自动关闭 redis 依赖功能（download_jobs/exec_events/full_tick_cache）；bootstrap 失败写入 `logs/bigqmt-bootstrap-error.log`。能力边界（无执行回报推送）已在 README 注明。
- **精简 QMT Python 兼容 fallback**：部分券商 python36.zip 裁掉 `importlib` / `logging`——REDIS_DRYRUN 注册最小 importlib 替代模块，logging_setup 降级为手写文件+stdout logger。
- **`OrderSnapshot.price_type`**：委托快照透出报价类型（m_nOrderPriceType），并补 `traded_price`；shim 新增 `xtdata.get_stock_type` 转发。

### 修复

- **纯 ZMQ 模式隐式连 Redis**（PR #108）：`publish_event`/`save_quote_subscription` 在无 redis discovery 的纯 ZMQ 下直接跳过，不再隐式建 redis 连接。
- **日线缓存日期窗口全滤光**（PR #108）：缓存为 8 位日期轴而调用方传 14 位 start_time 时字符串比较清空全部数据，现在按缓存轴精度对齐下限。
- **timeout_seconds 被吞**（PR #108）：`get_market_data_ex` 批处理与复权自愈重试路径上超时参数被丢弃，现全程透传。
- **交易日 ContextInfo fallback 用错首参**（PR #108）：SH/SZ 市场码被当证券代码传入，改为映射代表指数（000001.SH/399001.SZ）。

## [0.2.16] - 2026-08-29

纯新增，无破坏性变更。

### 新增

- **部署版本检测**：部署到 QMT 是文件拷贝，而 QMT 跨策略重跑保留 `sys.modules`——所以「忘了拷」和「拷了但没被加载」**从外部看完全一样**，此前只能靠比对文件字节和找行为变化来判断。

  启动日志现在会说明实际加载的是哪个构建：

  ```
  [bigqmt_shell] bigqmt_signal_trader 0.2.16 loaded from D:\...\python\bigqmt_signal_trader
  ```

  同一信息开放为 RPC：

  ```python
  xtdata.get_deployment_info()
  # {'version': '0.2.16', 'package_dir': ..., 'qmt_python_dir': ...,
  #  'strategy_dir': ..., 'python_version': '3.6.8'}
  ```

  `ping` 响应也带上了 `version`，客户端连接时若与自身版本不一致会**告警一次**，并说明拷贝之后仍需重启策略。

- **部署同步 `sync_deployment()`**：把客户端的包推到 QMT 的 python 目录，目标目录取自 `get_deployment_info()`，**不必硬编码路径**。

  ```python
  xt_trader.sync_deployment(dry_run=True)   # 先看会动哪些文件
  xt_trader.sync_deployment()               # 真同步
  ```

  设 `BIGQMT_AUTO_SYNC=1` 后，连接时检测到版本不一致会自动同步。**默认关闭**——往实盘终端写文件不该是「连接」的副作用，源码树里若有半成品会直接进实盘。

  | 行为 | 说明 |
  |---|---|
  | **绝不写入配置文件** | `bigqmt_signal_trader_local_config.py` / `bigqmt_signal_trader_client_config.py` 存账号与凭据；对应 `.example.py` 属文档，会更新 |
  | **不新增顶层文件** | 只刷新部署已有的模块，加上策略入口（全新部署需要） |
  | **覆盖前备份** | 留 `.bak_<时间戳>` |
  | **原子写入** | 先写临时文件再替换，中断不会留下半个模块 |

  **同步逻辑跑在客户端，不在 QMT 内。** 让交易进程盘中改写自己的代码，等于把源码树里的任何东西——包括改到一半的——直接送上实盘。每次结果都带 `restart_required`：拷贝本身不生效，必须重启策略。

### 修复

- **`__version__` 卡在 `0.2.0` 已十五个版本**，因而无法回答上述任何问题。现跟随 `pyproject.toml`，由测试钉住，并要求 `CHANGELOG` 中存在对应条目。

- **版本标记移出 `__init__.py`**：QMT 沙箱的加载器**从不执行根包**——它建一个空模块直接返回，因为根包的 eager exports 会撞 QMT 的导入白名单：

  ```python
  # QMT native allowlist rejects the root package eager exports.
  if name == "bigqmt_signal_trader":
      return module
  ```

  所以 `__init__.py` 里定义的东西**在 QMT 里不存在**——在所有测试环境都正常，唯独在它唯一需要生效的地方是隐形的。第一版正是放在那里，实盘返回 `AttributeError: module 'bigqmt_signal_trader' has no attribute 'deployment_report'`。现位于 `version.py` 子模块，测试钉住放置位置与导入写法。

### 实盘验证

部署 + 重启后：启动版本行出现；`get_deployment_info` 返回的版本与本地包一致；同步真跑一次——更新 3 个文件、跳过 51 个相同文件、**两个配置文件字节未变**、二次运行报告无操作。

### 已知限制

- **同步之后仍需手动重启策略**，无法从外部触发（`qmt_launcher` 的 `restart` 路径从未在真实环境执行过）。
- 其余同 0.2.15：本终端无期货行情权限、推送通道不可达的部署未验证、PR #88（信用委托类型）未合入、`can_close_vol` 哨兵值（#84）等。

---

## [0.2.15] - 2026-08-29

### 破坏性变更

两项，升级前请确认是否影响你的代码。

- **全市场快照默认只取股票**（Issue #104）：`get_full_tick(["SH"])` 此前返回交易所挂牌的**全部标的**，现在只返回股票。

  实测上交所 `"SH"` 共 **26744** 个标的，按名称核对后的构成是——**债券 82%**（`24浙江22`、`23山东57`、`深圳2536` 这类地方政府债，每个代码段近 1000 只）、股票 **8.7%**（2315 只）、基金/ETF 4.9%。深交所同理。

  依赖市场令牌取债券/ETF 的代码需显式传 `types=["all"]`。收窄时打印一次提示，避免只是静默变少：

  ```
  [bigqmt_market] SH narrowed to 2315 stock; pass types=['all'] for every
  instrument the exchange lists
  ```

- **`subscribe_quote` 成为真订阅**（Issue #95）：此前它把回调**调用一次**就结束——一次性取数顶着订阅的名字。这比没实现更容易误导：数据到了，然后永远等不到第二次。

  tick 周期改走已有的全推行情通道（单代码即一元 code_list，不新开端口），K 线周期改为轮询（服务端无 bar 推送机制）。**tick 订阅因此依赖推送通道，而原一次性路径不依赖**——推送通道不可达的部署会从"至少拿到一次快照"变成完全沉默。该场景未验证。

### 性能

- **全市场快照快 6.9 倍**（Issue #104）：7.44s → **1.08s**（SH），两市 5216 只 1.66s。

  瓶颈不在桥：服务端 handler 占总耗时 96%，RPC 编码仅 0.17s（14.1MB），传输+解码约 0.3s。QMT 单价严格线性、约 **0.29ms/只**（1000 只 0.42s、5000 只 1.66s、10000 只 2.87s），所以 7.4s 完全由**标的数量**解释。

  因此**在请求时收窄，而不是拿回来再过滤**——事后过滤仍要付 QMT 对每个多余标的的成本。市场令牌先解析为板块清单（FormulaServer 直连，实测 13ms，按运行缓存），只请求那些代码。

  | `types` | 板块 | 约数 |
  |---|---|---|
  | `stock`（默认） | 上证A股 / 深证A股 / 京市A股 | 2315 / 2901 / 339 |
  | `etf` / `fund` / `index` / `convertible` | 沪深ETF / 沪深基金 / 沪深指数 / 沪深转债 | 1696 / 2249 / 609 / 320 |
  | `all` | 不收窄 | 26744（SH） |

  **收窄失败一律退回全量**：类型不认识、板块查不到、板块查询抛异常、该市场无对应板块（如 HK）——都保留原令牌。丢行情比慢更糟。板块名取自实盘终端而非猜测：北交所是 `京市A股`，`北证A股` 返回 0。

### 修复

- **期货合约符号被大写化，共三处**（Issue #95）：各交易所命名规范不可互换（上期所 `rb2401`、郑商所 `AP401`），大写后是 QMT 不认识的代码——**返回空行情、不报错**，与"没有数据"无法区分。

  `#68` 当初靠**绕开** `normalize_stock_code` 解决了持仓路径，其余路径（下单、行情、全推缓存、风控）仍从这里过。三处依次是 `code_utils.normalize_stock_code`、`full_tick_cache.normalize_full_tick_codes`（在 `normalize_stock_code` **之前**又大写一次）、`market_bigqmt.normalize_market_or_stock_code`（同样在委托前大写，**使前两处的修复到不了 `get_full_tick`**——恰是本 issue 报告的路径）。

  同时泛化了返回键的映射：现在按调用方写法发送，映射改为按大写形式索引，**QMT 回显或自行规范化两种行为下都能还原**，#58 不会以任何方式复发。

- **期货交易所令牌不再抛异常**（Issue #95）：`IF` / `SF` / `DF` / `ZF` / `INE` / `GF` 此前在 `normalize_stock_code` 里以 `invalid stock code` 失败，整个交易所的期货快照无法获取。现在送达 QMT 由其回答，且**从不按股票收窄**——期货交易所只挂期货，令牌自带品种信息，无需 `types`。

- **无数据周期的订阅不再静默**（Issue #95）：本终端 `1m` / `5m` 返回空 DataFrame 而 `1d` / `tick` 有数据，订阅这类周期会得到一个**活着、正确、且永远沉默**的订阅——与坏掉的无法区分。现在说明一次（不刷屏），有数据后自动恢复安静。

### 未修复（附理由）

- **`get_market_data_ex` 的 `field_list=[]` 不走 FormulaServer 直连**（Issue #104）：报告人的观察准确（直连 0.03s vs RPC 0.97s，约 30 倍），但推论会损坏数据。空 `field_list` 意味着"全部字段"，返回 11 列，而直连只供 6 列、其余 4 列返回 `NaN`——RPC 有真实值（三只票实测 `preClose` 9.07 / 7.82 / 11.59，直连全部 `nan`）。默认路由到直连等于用 30 倍加速换真实价格静默变成 `NaN`。

  **要那 30 倍，显式写出 6 个 OHLCV 字段即可**；首次不传 `field_list` 时会在 `bigqmt.log` 记一条说明。

### 已知限制

- **本终端无期货行情权限**：合约详情 0 字段、快照 0 键、日线 0 行，九个合约四个交易所全空。因此期货令牌在有权限环境上的实际返回、以及 QMT 对小写合约回什么大小写，**均未验证**；代码对两种情况都做了处理。
- **推送通道不可达的部署未验证**：`subscribe_quote` 的 tick 路径现在依赖该通道。
- `can_close_vol` 哨兵值（#84）、PR #82 的 `traded_price` 无实盘证据、真实打新未验证、单文件构建需源码检出、`EmptyPositionProvider` 缺 `get_position_statistics`、#77 / #78 —— 同 0.2.14。
- **PR #88（信用委托类型）未合入**：其映射把 `33` / `34` 认成专项融资/融券，实为期权操作（专项信用是 `40` / `41`），另缺 9 个类型含最基本的 `28 CREDIT_SLO_SELL`。已请求修改。

---

## [0.2.14] - 2026-08-28

### 新增

- **新股申购（打新）接口**（PR #96 @ThomasAnderson01，PR #98 跟进）：`query_ipo_data` / `ipo_subscribe` / `ipo_subscribe_all` / `query_new_purchase_limit`。

  ```python
  for row in xt_trader.ipo_subscribe_all(acc, dry_run=True):   # 先看计划，不下单
      print(row)
  results = xt_trader.ipo_subscribe_all(acc)                    # 真申购
  ```

  **这是主动调用的方法，不是桥自动执行的行为。** 提交版本在 `adjust` 定时回调里无条件运行——整个 diff 没有任何开关，任何人升级后第二天 09:40 就会自动下真实委托，而且直接调 `passorder`，**绕过 `rpc_allow_order_methods=False`**：明确关掉远程下单的用户照样会被下单。改为显式接口后走既有 `order_stock` 通道，因而与其他委托一样受该开关管控。

  走既有通道还顺带修正了 `quickTrade`：被删的手写路径传 `1`，而 API 参考 1.4 明确要求**定时器/回调中下单必须传 2**（`1` 的语义是 `is_last_bar()` 为真才产生信号，在定时器回调里可能不成立，**委托会静默不发出**）。网关默认值本就是 `orderType 1101` / `prType 11` / `quickTrade 2`。

  **默认只打沪深**（市值申购、不冻结资金），北交所需冻结资金故排除，可用 `markets=("SH","SZ","BJ")` 显式打开。**申购代码认不出来一律跳过**——原实现结尾是 `return True`（"无法识别默认放行"），在一个专门排除北交所的过滤器上倾向于下单。

### 修复

- **`get_ipo_data` 的响应被清空**（实盘发现）：它返回**以申购代码为键的 dict**，却被送进 `_normalize_detail_rows`。那个函数对 dict 做 `for row in rows`——迭代的是**键**，再拿每个代码字符串去抓属性：

  ```
  QMT 返回:  {'301689.SZ': {'issuePrice': 16.0, 'maxPurchaseNum': 12000, ...}}
  归一化后:  [{}]              <- 申购代码、发行价、数量，全没了
  ```

  PR #96 修对了 `type` 参数（原将 `account_id` 传给了期望 `type` 的位置），但数据死在下一层，**所以这个 RPC 从未返回过可用数据**。

  实盘还证明了数据确实存在而非"今天没有新股"：该函数对空输入 `return []`，而服务端 `type="STOCK"` 返回 `[{}]`、`"BOND"` 返回 `[]`——非空 dict 被清空。修复后同一调用返回 `301689.SZ @ 16.0 × 12000`，与 #96 提交者当日上午实盘申购的完全一致。

  `get_new_purchase_limit` 文档（6.10）同样写明返回 dict，同样的问题，一并修。两者现走 `_call_qmt_mapping`：保留映射形状，只把值转成 JSON 安全。

- **客户端不再静默吞掉错误形状的响应**：跟进过程中一度用 `isinstance(data, dict) else {}` 归一化空值，那会把 `[{}]` 变成 `{}` = 「今天没有新股」——而当天恰好有。现在非空却形状不对会明确告警说服务端太旧。

### 文档

- README 新增「新股申购（打新）」一节，并**明确写明该接口不会自动执行**——否则读者看到"打新"容易以为装上就会自己跑。文中每个方法名、关键字参数、申购代码前缀均已对照实现核实。

### 已知限制

- **真实申购未验证**：只读与 `dry_run` 路径已在大 QMT 实盘验证（2026-08-28，`301689.SZ @ 16.0 × 12000` 计划正确、未下单），但 `dry_run=False` 会下真实委托，本仓库未执行。@ThomasAnderson01 曾用原实现于当日成功申购该股。
- **`query_new_purchase_limit` 实盘返回空 dict**：本账户无申购额度，属正常；形状已修正为 dict（此前为 list）。
- **期货合约符号大小写**（Issue #95）修复见 PR #97，**本版未合入**，待报告人确认其终端的期货数据情况。
- **信用委托类型仍会被塌缩成普通买卖**：PR #88 的映射把 `33`/`34` 认成专项融资/融券（实为期权操作，专项信用是 `40`/`41`），另缺 9 个类型含最基本的 `28 CREDIT_SLO_SELL`。已请求修改，未合入。
- **`subscribe_quote` 不是真订阅**：回调只触发一次，之后无推送。需要实时推送请用 `subscribe_whole_quote`。
- `can_close_vol` 哨兵值（#84）、单文件构建需源码检出、`EmptyPositionProvider` 缺 `get_position_statistics`、#77/#78 —— 同 0.2.13。

---

## [0.2.13] - 2026-08-27

### 修复

- **`account_type` 三个配置位置里两个静默失效**（Issue #92）：信用账户按 STOCK 查询**不会报错**——`get_trade_detail_data` 返回一行全 0 的资产。所以这个设置错了，表现就是「信用账户资产全是 0」，日志里没有任何线索。

  三个看着都合理的位置，此前只有一个生效：

  | 位置 | 修复前 |
  |---|---|
  | local config 里的 `BIGQMT_ACCOUNT_TYPE` | 生效 |
  | `BIGQMT_REDIS_CONFIG["account_type"]` | **无人读取** |
  | 改 `redis_rpc_runtime.py` 里的 `ACCOUNT_TYPE` | **被静默覆盖** |

  第三条尤其阴：解析式是 `BIGQMT_ACCOUNT_TYPE or ACCOUNT_TYPE or "STOCK"`，而随包发的 example 配置里写着 `BIGQMT_ACCOUNT_TYPE = "STOCK"`——它是真值，永远赢，所以改文件里那个常量等于白改。报告人用的正是后两条。

  后两个位置现在都认（按上表优先级），并且**解析结果在启动时打印、说明来源**，冲突会指名：

  ```
  [bigqmt_shell] account_type=CREDIT (from BIGQMT_REDIS_CONFIG['account_type'])
  [bigqmt_shell] ignored conflicting account_type from: BIGQMT_ACCOUNT_TYPE
  ```

  模块常量出厂即 `"STOCK"`，因此只有被改动过才算用户的选择——否则每个信用部署都会报一条与它的假冲突。

  **需要说清楚哪部分本来就没坏**：`account_type` 一旦解析出来，确实能正确到达 `get_trade_detail_data(account, 'CREDIT', 'ACCOUNT')`。新增 10 个测试中有 2 个覆盖该链路，它们在修复前的代码上也通过；另外 8 个会红。

### 已验证

- **PR #82 的 `traded_price` 拿到实盘证据**（0.2.11、0.2.12 两版的已知限制，现已解除）：实盘 18 笔委托、14 笔成交，逐笔核对——

  ```
  traded_price 与 price 不同的:  9 笔    (最大差 10.56)
  两者相同的:                    5 笔    (限价单按报价成交)
  已成交但 traded_price 为 0 的:  0 笔    (修复前应为全部 14 笔)
  ```

  证明 `traded_price` 是真实成交均价，而非 `price` 的副本。

### 已知限制

- **信用委托类型仍会被塌缩成普通买卖**：PR #88 试图修此问题，但其映射把 `33` / `34` 认成了专项融资买入/专项融券卖出——那两个实际是 `OPT_OPTION_SELL_CLOSE` / `OPT_OPTION_SELL_OPEN`（期权操作），专项信用是 `40` / `41`；另缺 9 个信用类型，含最基本的 `28 CREDIT_SLO_SELL`。已请求修改，本版未合入。
- **#92 的信用账户表现未实盘确认**：本机为股票账户，**无信用账户可验证「资产不再全 0」**。本版修的是配置发现与可见性，该部分完全由测试钉住，并已部署 QMT 重启验证（日志首次打印 `account_type=STOCK (from default)`，与股票账户未设置的预期一致，回归 6/6 PASS）。请 @jerry87n 在信用账户上复测。
- **`can_close_vol` 在股票账户上返回 LLONG_MAX 哨兵值**（Issue #84）。
- **单文件构建需要源码检出**：`tools/` 不随 wheel/sdist 分发；目标沙箱环境未在本机复现（本机 QMT 不拒绝 `import redis`），真实加载由 @heimo88 实测。
- **PR #81 的接口尚未补全**：`EmptyPositionProvider` 与 `PositionProvider` 协议均缺 `get_position_statistics`。
- **#77**（同终端双账户）属当前设计。**#78** 待报告人补充环境信息。

---

## [0.2.12] - 2026-08-27

### 新增

- **`bigqmt-init` 配置向导**：部署此前意味着抄两份 `.example.py`、搞清楚三十来个键里哪些真的要改、还要手工保证服务端和客户端两边一致。向导只问会变的那几项——账号、账号类型、传输方式、地址端口、Redis 凭据、是否允许远程下单、部署方式——然后**从同一组答案**生成两份配置，所以它们不可能在连接参数上对不上。选单文件部署时顺带跑对应生成器并把配置烘焙进产物，替换掉占位符。

  三项不问、直接定死：`rpc_background_threads` 恒为 `False`（`get_trade_detail_data` 离开主策略线程返回空，这不是可选项）；`rpc_allow_order_methods` 默认关，打开前明确说明含义；选无 redis 单文件会强制 `transport=zmq`，不会留下一份在无法 import redis 的文件里声称用 redis 的配置。

  密码分两类：Redis 密码是服务凭据，写进配置文件（`.example.py` 本来就这么记的），输入不回显；**QMT 登录密码完全不落盘**——`qmt_launcher` 从 `BIGQMT_LOGIN_PASSWORD` 读，这样它不会出现在 `argv` 或磁盘文件里，向导沿用该约定并在结束时说明。

- **单文件 QMT 构建生成器**（Issue #56，感谢 @heimo88）：部分券商的 QMT 是白名单 + 不能加载文件、不能 import 外部模块，只有把所有代码放进一个策略文件才能跑。`tools/build_single_file.py`（base64 内嵌）和 `tools/build_no_redis_single_file_flat.py`（明文真实代码，强制 zmq）把整个包打成一个自包含文件，运行时用自定义 import 钩子从内存解析。脚本由报告人在其券商环境实测通过。

  合入时换掉了模板里夹带的提交者个人实盘配置（真实账号、`rpc_allow_order_methods=True`、`rpc_background_threads=True`、`full_tick_cache_enabled=True`），并补上两个模板都缺的 `BIGQMT_ACCOUNT_TYPE`（#68 加的）。产物已 `.gitignore`，用时重新生成。

  flat 版把每个模块缩进进 `def _mod_N():` 再 exec——**正是让 `from X import *` 报 `SyntaxError: import * only allowed at module level` 的那个形状**。所以这个构建既依赖 0.2.11 对 #76 的修复，现在也成了它的回归守卫：整个测试套件里没有别的地方会把模块编译进函数体。

### 修复

- **负债合约查询少传一个参数**（PR #87，@ljjtim）：`get_unclosed_compacts` / `get_closed_compacts` 只传了 `accountID`，而 `docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md` 6.16 记载的签名是两参数、`accountType` 填 `'CREDIT'`。旁边三个单参数接口（`get_debt_contract` / `get_assure_contract` / `get_enable_short_contract`）未受影响，与文档一致。

- **`account_id is required` 说不清问题在哪**（Issue #90）：原来整条消息就一句 `Big QMT account_id is required`，**不说自己找过哪些模块**——所以「配置文件建了但放在当前解释器 import 不到的位置」和「压根没建配置文件」产生的报错一模一样。报告人其实已经建了那个文件。

  现在区分两种成因（没有可导入的模块 / 模块导入了但没定义 `BIGQMT_ACCOUNT_ID`——后者去查 `sys.path` 是南辕北辙），列出三条已逐一实测的解法，并点出时序陷阱：`configure()` 在模块导入时就跑了一次，之后才放好的配置不会自动生效。构造这条消息本身不会抛异常——它跑在错误路径上。

### 文档

- **QMT Python 组件前置说明**（Issue #85）：全新安装的终端 `bin.x64\` 下没有 `Lib\` 目录，也没有 `python.exe`——那是 Python 组件带来的，不是终端自带的，不要手动创建。此前文档直接假设这些路径存在，还让往 `bin.x64\Lib\site-packages` 里拷包。
- README 新增「配置向导」「单文件构建」两节；修正过期的常量计数（91 → 539，#73 之后）。

### 已知限制

- **信用委托类型仍会被塌缩成普通买卖**：PR #88 试图修这个，但其操作类型映射把 `33` / `34` 认成了专项融资买入/专项融券卖出——那两个实际是 `OPT_OPTION_SELL_CLOSE` / `OPT_OPTION_SELL_OPEN`（期权操作），专项信用是 `40` / `41`；另缺 9 个信用类型，含最基本的 `28 CREDIT_SLO_SELL`。已请求修改，本版未合入。
- **`can_close_vol` 在股票账户上返回 LLONG_MAX 哨兵值**（Issue #84），沿自 0.2.11 的 #81。
- **PR #82 的 `traded_price` 仍无实盘证据**（验证当日 0 笔委托），契约由单测钉住。
- **单文件构建未在目标沙箱环境复现**：本机 QMT 不拒绝 `import redis`。生成、编译、配置正确性由测试钉住；受限券商环境里的真实加载由 @heimo88 实测（#76 已据此关闭）。
- **PR #81 的接口尚未补全**：`EmptyPositionProvider` 与 `PositionProvider` 协议均缺 `get_position_statistics`。
- **#77**（同终端双账户）属当前设计——每账户状态存放在模块级全局，两个实例共享 `sys.modules` 即互相覆盖。**#78** 待报告人补充环境信息。

---

## [0.2.11] - 2026-08-27

### 新增

- **`query_position_statistics` 持仓统计**（PR #81，@ReCodeLife）：对齐 MiniQMT 同名接口，服务端经 `get_trade_detail_data(..., "POSITION_STATISTICS")` 提供，43 个字段同时给出 snake_case 与 `m_` 两套名字。正确加入主线程方法名单——`get_trade_detail_data` 离开主线程返回空。**实盘验证推翻了 PR 描述的一个前提**：该接口在**股票账户**上也返回数据，不限期货。6 个持仓逐行核对：`position` 与 `query_stock_positions` 的持仓量 **6/6 完全一致**，8 组 `m_` 别名与 snake_case 全部吻合，17/43 字段有值（其余 26 个是期货专属的保证金/权利金字段，股票账户为空属正常）。
- **委托回调带成交均价**（PR #82，@yuchiwang）：`traded_price`（成交均价）本就是原生 `XtOrder` 字段，但桥从未填充，导致 `on_stock_order` 在已成时拿不到成交价。服务端查询路径、回调 normalize、客户端 `_order_from_dict` 四层补齐并各带测试。提交者已实盘验证。

### 修复

- **单文件 QMT 沙箱构建无法加载：`from xtquant.xtconstant import *` 是语法错误**（Issue #76）：单文件构建把每个模块塞进函数体 exec，而 `import *` 只允许在模块级，报 `SyntaxError: import * only allowed at module level`。这是 0.2.10 里 #73 引入的——它删掉 `xtquant_compat` 中 110 个硬编码常量、改用 `import *` 兜住。

  **只导入实际用到的 3 个名字会修好语法、同时弄坏别的东西**：`import *` 拉进 534 个名字，模块自身只用 `ORDER_UNKNOWN` / `STOCK_BUY` / `STOCK_SELL`，但 `docs/XTQUANT_COMPAT_REPLACEMENT.md` 记载的「接入方式一」是 `from bigqmt_signal_trader import xtquant_compat as xtconstant`，即调用方从本模块读常量。改为显式循环回填，**539/539 全部保留**并逐个与来源比对。没有使用模块级 `__getattr__`：PEP 562 是 Python 3.7+，而 QMT 自带 3.6（`bin.x64/python36.dll`）；4 个混合大小写常量（含原生 SDK 拼写的 `OFFSET_FLAG_ClOSEYESTERDAY`）也排除了按 `.isupper()` 过滤的写法。

- **回调线程上首次导入模块失败，回调推送全丢**（Issue #76）：`exec_events` 之前是在 order/deal 回调**内部**导入的。QMT 的这些回调跑在经 `PyGILState_Ensure` 进入的 C++ 线程上，在该线程上首次 exec 一个尚未导入的模块会在 C 层失败**且不设置 Python 异常**，表现为 `SystemError: error return without exception set`。普通包部署不触发（init 期的 reload 已把它预热进 `sys.modules`，惰性导入直接命中缓存）；单文件沙箱构建里那次 `import_module` 会失败并被 `except` 吞掉，于是真的在回调线程上首载。改为模块加载期导入，走已在服务适配器模块的同一个本地 loader。

  同时修掉**让这个 bug 藏了一天的原因**：handler 只记 `str(exc)`，日志读出来就是 `error return without exception set` 然后没了。现在带异常类与完整堆栈。

- **adjust 主线程上一个未受控的调试 `print`**（#81 跟进，4c7d1cb）：PR #81 夹带了一段与其功能无关的调试输出。该文件其余 `print` 均受 `debug_log_limit`（默认 0）控制，这一处没有，因而每个响应都执行；且对**完整** payload 做 `json.dumps` 后才截断到 2000 字符。实测一个典型 `get_market_data_ex` 响应（100 支 × 240 根）序列化 2.6MB 耗时约 **30ms**、丢弃 99.92%——而它运行在 adjust 主线程上，实测 `tick_app` 11500 次调用的最大值才 29–42ms、p99.98 在 5ms 以内。

### 已知限制

- **`can_close_vol` 在股票账户上返回 LLONG_MAX 哨兵值**（Issue #84）：实盘 6 个持仓全部返回 `2^63-1`，即 QMT 对股票账户「未设置」的哨兵，被原样透传成一个真实数字；而本仓库 API 参考把 `m_nCanCloseVol` 记为 int「可平」。映射代码本身忠实转换了 QMT 给的值，问题在于哨兵未被识别。**这只有实盘数据能发现**，单测与代码审查都看不出来。
- **PR #82 的 `traded_price` 尚无实盘证据**：验证当日 0 笔委托，该字段需**有成交的委托**才能证明。契约与四层往返由单测钉住。
- **Issue #76 两项未完成验证**：报告人的单文件构建脚本依赖两个从未附带的模块（同 #56），**该构建无法在此复现**——`import *` 一项是通过「将模块源码放入函数体编译」钉住的，已确认该检查在修复前的代码上复现了报告人所报的 SyntaxError，但这不等同于跑过其真实构建；真实回调投递需实际下单才触发。已请报告人复测。
- **PR #81 的接口尚未补全**：`EmptyPositionProvider` 与 `PositionProvider` 协议均缺 `get_position_statistics`（实测 `AttributeError`，会降级为 RPC 错误响应，不会中断线程）。
- **#77**（同终端双账户）：现为一策略实例对应一账户——`_account_id` / `_rpc_service` / `_quote_subscription_service` 等每账户状态存放在模块级全局，RPC 通道亦按账户模板化，两个实例共享 `sys.modules` 即互相覆盖。属当前设计，非缺陷。**#78** 待报告人补充环境信息。

---

## [0.2.10] - 2026-08-26

### 修复

- **zmq 部署收不到任何回调推送**（Issue #76）：order/trade 事件**两端都硬绑 Redis**——服务端 `_publish_exec_event` 建不出 Redis 客户端就直接 `return`，客户端 `_event_loop` 只订阅 Redis 频道。纯 zmq 部署因此完全收不到 `on_stock_order` / `on_stock_trade` / `on_order_error`，而且是**静默的**：客户端只是连不上然后无限重试，服务端把「没有 Redis」当正常跳过。现在 exec 事件复用已有的全推行情 PUB 通道（不新开端口），Redis 仍优先（其频道带 stream 可做短重放）。报告人读代码就把这个推了出来。
- **adjust 每个 tick 都在新建 Redis 客户端**（PR #79）：`_pump_download_jobs` 每次运行都建一个新客户端，而它每个 tick 都跑——按 100ms 间隔就是**每秒 10 个**，每个带一套连接池。症状是 QMT 面板里的 `AttributeError: 'Redis' object has no attribute 'connection'`（redis-py 的 `__del__` 跑在构造未完成的对象上），一天 31 次。Python 把它吞成 `Exception ignored in`，所以**从没进过 `bigqmt.log`**。`_exec_event_redis` 早已为同样理由加过缓存，此处被漏掉；修复是复用同一个缓存 helper。
- **一行无法解析的数据搞垮整个查询**（PR #70）：#73 让 `_full_code` 遇到柜台式交易所 ID 时抛异常——信号本身对，但三个调用方的行循环都无逐行保护，异常一路抛出 `get_positions` / `query_orders` / `query_trades`。一行异常 = 整个持仓查不到；`query_orders` 外层 `except` 返回 `[]`，丢的是全部委托。现在跳过该行、其余照常返回。
- **xtquant / xtquant_compat 循环导入**（PR #74）：#73 反转常量依赖后，`xtquant/__init__` 急切加载的 `xtdata`/`xttrader` 又反向引用 `xtquant_compat`，环闭合——`import bigqmt_signal_trader` 直接失败、**27 个测试模块无法收集**。且**依赖导入顺序**（先 import xtquant 能过），这类 bug 平时测不出来。两个 shim 改为通过模块级 `__getattr__`（PEP 562）惰性解析；调用方三种写法（属性访问 / from-import / 子模块导入）全部逐项验证不变。

### 变更（PR #73，@ReCodeLife）

- **常量定义迁回 shim 侧**：`xtquant/xtconstant.py` 补全为完整实现（90 → 539 个），`xtquant_compat` 改为 `from xtquant.xtconstant import *` 并删去 142 行硬编码。对着 QMT 自带原生 SDK 逐个比对：**原生 90 个常量 0 个值被改动、0 个缺失**，新增 443 个券商扩展枚举。`xttype` 同步扩展。
- **期货持仓代码解析**（PR #68，@ReCodeLife）：裸期货合约（交易所字段为迅投简称 DF/SF/ZF）此前被送进股票归一化并抛错。改为按交易所字段分类、不猜代码形状，并**保留符号原始大小写**（`rb2401.SF` 小写 / `AP401.ZF` 大写，不可互换）。新增 `BIGQMT_ACCOUNT_TYPE` 配置（默认 `STOCK`）。

### 已知限制

- **#79 的实盘验证不充分**：修复后线上 0 次，但**重启前也是 0 次**（问题出现在前一日），所以这个 0 不构成证据。行为由单测钉住（20 次调用 → 1 个客户端，还原即变红），实盘待自然复现。
- **#76 的真实回调投递未验证**：已验证通道连通、格式正确、部署版代码端到端可投递（order/trade/order_error 三类），但真实回调需**实际下单**才触发，收盘后订阅收到 0 个事件属正常。
- **#58 的期货小写代码仍缺实盘样本**；**#56 单文件构建**未进主干（报告人的脚本依赖两个未附带模块）；**#77 / #78** 待报告人补充信息。

---

## [0.2.9] - 2026-08-24

### 修复

- **redis-py 3.5.3 兼容（PR #67 回归，Issue #71）**：`build_redis_client` 无条件传 `protocol=` 在 QMT 自带的老 redis-py 上直接 TypeError——且 QMT 策略重跑后执行事件发布器重建客户端时崩掉，事件被静默全丢（发布器构建失败现在会记日志，不再无声）。改为按版本能力（inspect.signature）条件透传；客户端 `_redis()` 同步处理。
- **async_response 没有真实 order_id**（Issue #72）：委托号异步分配、RPC 应答时通常还没有，order_id 只能回落成 remark，按 order_id 管理委托的代码会解析失败。现在 response 触发前等屏障从暂存的委托事件里学到真实委托号（bounded 2s，学不到才回落 remark）。下单仍走 wait_settlement=False 快速应答（#50/#69 的吞吐不回退）。实盘验证：`async_response order_id=xt1090519419`（真实委托号）。
- **#69 发单间隔**：0.5s 检查已在 #44 改为结算停放（不阻塞提交）——见 issue 回复，无需改动。

## [0.2.8] - 2026-08-25

### 修复

- **期货持仓代码解析错误**（PR #68，@ReCodeLife）：裸期货合约（交易所字段为迅投简称 DF/SF/ZF）被错误送进股票归一化并抛 invalid stock code。改为**按交易所字段分类，不再猜代码形状**：迅投简称（IF/SF/DF/ZF/INE/GF）拼接后缀并**保留符号原始大小写**（`rb2401.SF` 小写 / `AP401.ZF` 大写，两者不可互换）；股票/港股通走归一化；`code_utils` 补 `.HGT` / `.SGT` 后缀识别。
- **一行无法解析的数据会搞垮整个查询**（PR #70）：上一条让 `_full_code` 遇到柜台式交易所 ID 时抛异常——信号本身是对的，但三个调用方的行循环都没有逐行保护，异常会一路抛出 `get_positions` / `query_orders` / `query_trades`。**一行异常 = 整个持仓查不到**；而 `query_orders` 外层 `except` 返回 `[]`，丢的是全部委托。对交易系统而言这比它要报告的问题更危险，也与本模块「降级而非崩溃」的既定风格矛盾（POSITION 查询外的 try/except 注释即为 *degrade to empty*）。现在跳过解析不了的那一行、其余照常返回，跳过按 `(kind, exchange)` 只记一次日志。

### 新增

- **`BIGQMT_ACCOUNT_TYPE` 配置**（PR #68）：账号类型独立可配（默认 `STOCK`，另有 `CREDIT` / `FUTURE` / `OPTION`），`redis_rpc_runtime` 向后兼容读取并归一化为大写字符串后传入 `configure()`。旧配置不写此项时行为不变。

### 已知限制

- **#58 的期货小写代码仍缺实盘样本**：`get_full_tick(['rb2708.SF'])` 返回空，无法据此判断大小写还原是否生效（空结果说明该合约无数据，而非映射失败）。本版持仓侧的大小写保留由单测覆盖。
- **单文件构建（#56）未进主干**：报告人提交的 `build_no_redis_single_file_flat.py` 依赖两个未附带的模块和 `bigqmt_no_redis/` 目录，尚不能独立运行；其惰性加载架构优于此前方案（PR #62 已关闭），待依赖补齐后合入。

---

## [Unreleased]

### 新增

- **Redis 5.x 兼容**（PR #67，@sunjian710）：`build_redis_client` 透传 `protocol`（默认 2/RESP2，可用 `protocol: 3` 或 `BIGQMT_REDIS_PROTOCOL` 覆盖）——redis-py 8.x 默认 RESP3 的 HELLO 握手在 Redis 5.0 上直接报错，现在开箱即用。客户端 `BigQmtRpcClient` 的 redis_config 同样透传。
- **持仓/资产对象原生字段别名**（PR #67，@sunjian710）：`query_stock_positions`/`query_stock_asset` 返回对象新增 `m_` 前缀原生字段（`m_strStockCode`/`m_nVolume`/`m_dCash`/`m_dTotalAsset` 等），读原生 xtquant 字段名的客户端代码（如 miniqmt_redis 风格）不再 AttributeError。

### 修复

- PR #67 合入修正：`_position_object` 的 m_ 别名引用了未定义局部变量（`stock_code`/`stock_name` 只内联在 kwargs 里），持仓查询全挂——提取为局部变量并补回归测试钉住全部别名。

## [0.2.6] - 2026-08-24

### 修复

- **回调对象字段命名不一致**（Issue #65）：`on_order_error`/`on_cancel_error`/async 回报的委托号字段是 `order_sys_id`，而 `on_stock_order`/`on_stock_trade` 用 MiniQMT 规范名 `order_sysid`。全部回调对象现在同时携带两个名字（同值），另补 `order_remark`/`status`/`strategy_name`。
- **on_order_error 缺 order_remark/status**（Issue #64）：服务端事件补上 `m_strRemark`→`order_remark`/`user_order_id` 与 `m_nOrderStatus`→`status`，撤单错误事件同步补齐。柜台的拒单理由此前已在 `error_msg`（m_strCancelInfo，#60）。
- **tick 分笔下载后立读为空**（Issue #66）：两个叠加问题——①QMT 下载全局提交即返回、数据异步落地，下载后立即读读到的是落地前的空结果；②FormulaServer 快速路径不服务 tick/L2 周期却静默返回空，把 RPC 桥的正确答案挡在门外。修复：download_history_data2 分批轮询直到批内每个代码出现真实数据行（`data_wait_seconds` 超时容忍停牌/退市）；FormulaServer 对 tick/l2 周期拒绝路由、回落 RPC。实盘验证：tick 下载 0820 → 立读 1434 行全部属当日。
- **拒单回报乱序**（Issue #51 屏障补全）：服务端在应答 RPC 之前就推送废单事件，order_error/cancel_error 此前不在屏障内，会比 async_response 先到（实盘实测倒挂）。屏障现在同时扣住 order/trade/order_error/cancel_error，实盘验证顺序：async_response → 已报(50) → 废单(57) → order_error。
- **call_async 异步 RPC**（Issue #63）：`client.call_async(method, params, callback=None)` 立即返回 Future，不阻塞调用方；有界在途（64）+ 8 线程池；可选 callback 在单 dispatcher 线程按完成顺序串行派发。注意：下单类 RPC 在服务端仍由 QMT 主线程串行处理，客户端异步叠加的是往返延迟，不会并行化报单到交易所的环节。

---

## [0.2.5] - 2026-08-21

### 修复

- **get_full_tick 返回 key 被全大写**（Issue #58）：代码 upper() 后传给 QMT，返回时未映射回调用方写法。期货交易所的合约代码是小写（`rb2708.SF`、`a2609.DF`、`nr2612`），导致 `code in result` 对每一个都失败、实时五档被误判缺失。现在只还原**大小写**，不动归一化——`600000` 仍补全为 `600000.SH`，补全后缀是调用方依赖的行为。QMT 主动返回而未被请求的 key 原样透传，不丢行情。
- **柜台拒单原因没有传出**（Issue #60）：`status_msg` 与 `error_msg` 在废单时均为空，形如 `[COUNTER] 资金可用余额不足，尚需[4789.630]` 的原因完全丢失。两处缺口：`OrderSnapshot` 没有 `status_msg` 字段；`normalize_order_error_event` 只读 `m_strErrorMsg`。柜台文本实际在 `m_strCancelInfo`（官方字段表标注为「废单原因」，状态 57 明确指向它）。已贯通 `OrderSnapshot` → `order_bigqmt` → 委托事件 → `_order_from_dict`，`order_error` 事件改为优先读该字段。

### 变更（PR #59，@cnwuwil）

- **回调/成交对象对齐 MiniQMT 原生契约**：`XtTrade` 补齐 `traded_id` / `traded_time` / `traded_amount` / `strategy_name`，成交金额优先取 `m_dTradeAmount` 真值（实测限价 9.14 成交 9.06 时估算值偏高约 0.9%）；撤单响应补 `cancel_result` / `error_msg`；业务回调异常落日志不再静默吞掉。
- **回调代码补全交易所后缀**：实盘 order/deal 回调的 `m_strInstrumentID` 只带 6 位裸代码，服务端事件构建时从 `m_strExchangeID` 补全，客户端分发层按 A 股代码段推断作兜底。
- **`order_volume` 语义修正**：由剩余量 `m_nVolumeTotal`（全部成交时为 0）改为原始委托量 `m_nVolumeTotalOriginal`，对齐 `XtOrder.order_volume`。**对依赖「剩余量」的调用方是行为变化**。
- **xtdata shim 签名对齐**：`get_instrument_detail` 接受并忽略第二参数 `is_detail`；`download_history_data(2)` 透传 `dividend_type`。
- 新增官方《内置Python》API 精简参考文档，作为字段映射的依据留存。

### 实盘验证（2026-08-21 收盘后，真实账户）

- **柜台拒单原因**（#60）：29 笔委托中 15 笔废单全部带回柜台原文，`m_strCancelInfo` 字段名经实盘证实。消息不止有原因，连参数也在：
  `[COUNTER][251005][证券可用数量不足][v_stock_code=000506,v_occur_amount=100.00,p_enable_amount=0.00,...]`
- **`order_volume` 语义**（#59）：13 笔已成（状态 56）委托的 `volume` 全部为真实委托量，无一为 0。旧代码读剩余量，这 13 笔会全是 0——这是该项修复的决定性证据。委托代码的交易所后缀也已确认带全。

### 已知限制

- **#58 的期货小写代码缺实盘样本**：`get_full_tick(['rb2708.SF'])` 返回空，无法据此判断大小写还原是否生效（空结果说明该合约无数据，而非映射失败）。股票代码路径确认未被破坏，小写映射逻辑由单测覆盖。
- Issue #56（单文件、无 Redis、仅 ZMQ 的 QMT 内嵌版本）未处理。

---

## [0.2.4] - 2026-08-20

### 修复

- **qmt_launcher mode=login 打不进密码**：SendMessage 把键消息发给顶层窗口，但 Qt 对话框只在窗口持前台焦点时才把按键路由给输入框——后台进程发送等于静默丢弃。改为物理输入（keybd_event/mouse_event）：Alt 键解锁前台保护 → 置顶 + 前置 → 物理点击字段 → 打字。账号预填时先 Ctrl+A 再覆盖（否则变成追加）；密码框点击位置避开右侧虚拟键盘图标；控件坐标按窗口尺寸比例定位。已是大窗（主界面=自动登录完成）时跳过整个输入流程，避免密码打进主窗口控件。
- **登录框就绪判定**：58600 端口在登录前就监听，launcher 的端口就绪不等于"已登录可用"——docstring 已注明，建议以 RPC ping 为真正就绪信号。

### 新增

- **normalize_stock_code 支持 QMT 期权/期货后缀**（PR #57）：`.BJ/.HK/.SHO/.SZO/.SF/.DF/.IF/.ZF/.INE/.GF` 等 ContextInfo 原生后缀直接透传，不再 ValueError——ETF 期权、商品期货/期权代码可正常走 RPC。原 6 位股票码逻辑不变。
- **get_full_tick 超时参数**（PR #57）：新增 `timeout_seconds=None`，调用方传值时优先（批量查 1256+ 期权代码时 120s 默认不够），不传时保留原自动检测（全市场 30s）。

---

## [0.2.3] - 2026-08-19

### 修复

- **异步下单的 order/trade 事件先于 async_response 到达**（Issue #51）：两条回调走不同通道——`async_response` 在异步下单工作线程触发，`order`/`trade` 来自 Redis pub/sub 监听线程；服务端 `order_callback` 先推事件、后回 RPC，顺序颠倒是常态。客户端按 `order_remark` 设屏障：命中待响应委托的事件先暂存，response（或 error）触发后按到达顺序放行；10 秒超时兜底，提交失败的事件也不会被永久扣住（丢事件比顺序错乱更糟）。仅 `order_stock_async` 路径受影响，手工/同步/无 remark 委托直通。成交事件无 remark 时按委托事件学到的 `order_sys_id` 关联。已验证：9 个单测（含 4 个反向验证）+ 盘后真实 Redis 注入实测 + 盘中 3 轮真实买卖验证（async_response 均先于 order/trade 到达，买单 50→54 撤单成功，卖单无持仓 50→57 废单正确上报）。
- **重复 order_remark 导致暂存事件丢失**（Issue #51 后续）：`order_remark` 不强制唯一（网格类策略常复用同一 remark），同 remark 的第二笔下单会让 `_arm_order_barrier` 直接覆盖前一笔的屏障，暂存事件被静默丢弃；且前一笔的 response 会误放后一笔的屏障，使后一笔失去保序。改为接管旧屏障时先放行其暂存事件；`_release_order_barrier` 增加 seq 校验，只有 arm 时的那笔委托的 response 才能放行对应屏障。回归测试对任一半修复回退均失败。
- **download_history_data2 只下载当天数据**（Issue #54）：部分 QMT 版本只注入单股下载全局 `down_history_data`，而捕获列表只有 `download_history_data/2` → 下载 RPC 静默返回 False、什么都没下，读取只能看到当天数据。捕获列表补 `down_history_data`；`download_history_data2` 无批量全局时按代码循环调用单股全局（日期透传）；`_handle_download_history_data` 同步兜底。修正 DRYRUN 里恒为 False 的下载绑定诊断打印。
- **get_financial_data 表名不兼容**（Issue #52）：MiniQMT 传整表名（Balance/Income/…），大 QMT 要 `"BIGTABLE.field"` 点分字段列表。服务端适配层新增 MiniQMT→大QMT 表名映射（8 张表）+ 整表名展开为全字段点分列表；点分条目前缀重映射、未知表名透传。实盘验证：`Balance`（展开 54 字段）与 `Capital`（6 字段）均返回真实数据（首调服务端自动下载财务数据较慢，之后毫秒级）。
- **本地缓存丢时间轴导致日期窗口失效**（Issue #54 关联）：客户端规范化把 `stime` 列转成索引（MiniQMT 形态），而 local_cache 只在列里找时间轴 → `get_local_data` 的 start/end 完全不生效、按索引去重也失效。修复：缓存层识别索引形态时间轴（`write`/`read`/`covered`/count 截尾/占位行清理全链路保索引）；parquet 写盘带索引；老版写出的无时间轴缓存文件遇新版写入时自动废弃（行不可切片，留着只会污染窗口过滤）。新增 18 个读路径矩阵用例（形态 × 周期 × 窗口 × count × 复权隔离）。

## [0.2.2] - 2026-08-19

### 修复

- **server_error 污染后续查询**（Issue #43）：`_last_server_error` 是实例状态，但每个成功响应都会读取它，而只有下单路径会重置。一次静默拒绝的委托会把错误盖到之后**所有** ping 和查询上，直到下一次下单。改为在 `handle()` 中每请求清空，且清空发生在方法校验之前，因此被拒绝的方法也不会携带上一次的诊断。
- **order_remark 匹配的模糊兜底**（Issue #41）：那段 `stock_code + action` 的兜底并非用于*识别*委托，而是**告警闸门**——问题比报告描述的更严重。`order_tag` 是我们生成的唯一 id，匹配不上即真未进系统；模糊兜底唯一的作用是**压制真实告警**：账户中若有一笔无关的同股票同方向委托（手动下的或上一笔未成交的），会导致 `order_sys_id` 未回填、`server_error` 为空，客户端看到一次干净的成功，而该委托从未进入系统。已移除。
- **order_stock_async 阻塞 QMT 主线程**（Issue #44）：`_handle_submit_order` 中的 `sleep(0.5)` 在 adjust 主线程执行（下单方法不在 `listener_methods` 中，走 deferred 路径），使其余请求串行等待，吞吐上限约 2 单/秒。改为**推迟响应而非推迟工作**：提交后登记 `OrderSettlement` 并停放响应，由每次 adjust drain 重试查询，委托号就绪即在同一 tick 内回复（零 sleep）。后台线程方案不可行——`get_trade_detail_data` 在非主策略线程返回空，会把每笔都误判为静默拒绝。
- **order_stock_async 未立即返回**（Issue #50）：客户端内部同步调用 `order_stock`，阻塞整个 RPC 往返，加上 #44 后的结算等待，每笔 0.5~1 秒。服务端新增 `wait_settlement` 参数（false 时 passorder 一返回即回复，委托号由 `order_callback` 推送）；客户端提交移至工作线程，`order_stock_async` 不碰网络直接返回 seq。`on_order_error` 现在也携带 `seq`，此前无法判断是哪一笔异步委托失败。
- **未复权下载实为空跑**（Issue #47，亦是 #39 的真正原因）：服务端下载此前只在请求复权时执行，未复权路径仅调用 `get_market_data_ex` 读取已有数据，却照常通过 callback 报告 `{finished: N}`——为一件没发生的事显示进度。而 1d/tick 默认即 `dividend_type="none"`。现在所有 `dividend_type` 都执行下载。实盘验证：601398.SH 本地日线从 0 根变为有数据。
- **query_stock_orders 缺少 order_time**（Issue #48）：大 QMT 的 ORDER 行提供报单日期与时间，但三层均未读取。已贯通 `OrderSnapshot` → `order_bigqmt` → `_order_from_dict`，按 MiniQMT `XtOrder.order_time` 语义输出 Unix 秒。实盘验证：11 笔真实委托全部有值。

### 新增

- **qmt_launcher**（Issue #45）：`open` / `close` / `restart` / `status` 四个命令管理 QMT 终端。按 `bin.x64` 路径隔离（同机多实例并存时不会误关其他账户）、以 FormulaServer 端口可连接为就绪判据而非固定 sleep、窗口标题前缀匹配（不再写死版本号）、先优雅终止 20 秒后才强杀。登录路径用 `SendMessage` 投递窗口句柄，不依赖窗口置于前台。
- **get_market_data_ex 分批**（Issue #47 评论）：宽 `stock_list` 此前共用一个 RPC 超时，要么装得下要么整批丢失。改为按 100 个代码一批，单批失败只损失自身代码，全部失败才抛异常。`chunk_size=0` 恢复原行为。
- **bar driver 观测埋点**：`adjust()` 按触发来源分别计数、`tick_app` 全量耗时直方图、init 报告策略品种/周期/订阅能力。用于定位 RPC 读延迟的来源。

### 变更

- `AssetSnapshot` 补齐 `frozen_cash` / `market_value`，对齐 MiniQMT `XtAsset`；`market_value` 优先取 `m_dInstrumentValue`，仅在服务端未上报时才推导（推导会扣除冻结金额，此前未扣导致市值虚高）。
- ZMQ 传输改为精确绑定配置端口，冲突时报错而非向上扫描——端口静默漂移会让客户端连不上。

### 已知限制

- Issue #44 / #50 的实盘下单验证尚未完成（单测已量化非阻塞行为：drain < 0.2s、20 单 < 1.0s）。
- Issue #47 评论所述的 `get_market_data_ex` 超时未能复现；三组压测（300 只 × count=3、300 只 × 全历史、50 只 × 1m 全天）最慢 718ms，远在默认 6s 超时内。分批目前是防御性改动。
- RPC 读延迟受 QMT 主线程 GIL 制约，延迟 ≈ 基础 + N × `schedule_adjust_interval`。实测该间隔 200ms → 100ms 可使 p50 从 374ms 降至 172ms，代价是 CPU 占用上升。

---

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
