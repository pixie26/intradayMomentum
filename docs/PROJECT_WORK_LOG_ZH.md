# Intraday Momentum 项目完整研究与优化记录

**记录日期：2026-07-29**  
**研究对象：SPY 一分钟日内动量策略**  
**目标：区分论文逻辑、作者 sample code、数据和执行假设，最终判断策略在论文公开后是否仍有可交易价值。**

---

## 1. 项目起点与研究目标

项目起初希望复现论文《Beat the Market: An Effective Intraday Momentum Strategy for S&P 500 ETF (SPY)》，并用 2008–2026 年 SPY 一分钟数据回答四个问题：

1. 作者论文和 sample code 能否准确复现；
2. 原始高收益是否由未来函数、数据处理或乐观成交假设造成；
3. 在严格数据、真实成交和完整成本下，策略是否仍有经济价值；
4. 2024-05-01 论文公开后的表现是否仍然有效。

项目初期考虑过 Qlib，但随后决定暂不引入。原因是这是一只标的、分钟级、规则型策略，首要任务不是机器学习框架，而是建立一套透明、逐分钟可审计、可和论文逐项核对的原生引擎。

---

## 2. 论文和作者 sample code 研究

### 2.1 核心策略逻辑

策略使用：

- 当日开盘价；
- 前一交易日经现金分红调整后的收盘价；
- 过去 14 个交易日、同一分钟相对开盘的绝对波动；
- 当日累计 VWAP；
- 每 30 分钟决策一次；
- 2% 日波动目标和最高 4 倍杠杆。

信号结构：

```text
close > upper_band 且 close > VWAP  → long
close < lower_band 且 close < VWAP  → short
否则                                  → flat
```

### 2.2 作者 sample code 中发现的偏差

作者的 Python/Colab sample 有教学价值，但并不是严格 gold standard。确认的问题包括：

1. **日波动率窗口 off-by-one**：使用 `d-15..d-2`，漏掉最近完成的交易日；
2. **`sigma_open` 只要求 13 个历史值**：论文写的是 14 个交易日；
3. **波动率缺失时使用 4 倍杠杆**：缺少风险估计时反而承担最大风险；
4. **sample 未完整扣论文滑点**；
5. **使用存在的数据行 rolling**：缺分钟或缺交易日时会向前多取更老观察；
6. **成交口径乐观**：signal bar close / shifted close-to-close 并非严格可执行 next-open；
7. **时间戳和 bar label 语义不够明确**；
8. **benchmark 最初只计算 price return**；
9. **缺失日、AUM延续、回归和 drawdown 统计不够严谨**。

因此最终没有把“论文”和“sample”合并成同一个模式，而是明确建立三个 profile：

- `official_sample_compatible`
- `paper_spec`
- `corrected_execution`

---

## 3. 为什么先重建数据层

原始 GitHub 代码直接读取 merged CSV/Parquet 回测。这样无法区分：

- 策略本身失效；
- timestamp 错位；
- 重复bar；
- 缺失分钟；
- 半日市；
- 熔断；
- vendor splice；
- 分红或复权口径错误。

因此研究顺序调整为：

```text
raw data
→ data validation
→ component validity
→ features
→ signal
→ order/fill
→ PnL/cost
→ evaluation
```

核心原则是：**不使用 forward fill 制造不存在的价格、成交量、VWAP或可成交机会。**

---

## 4. 数据 pipeline 的迭代

### 4.1 初始版本

完成：

- UTC / America/New_York 时间统一；
- start-labelled bar 语义；
- XNYS 交易日历；
- DST、节假日和半日市；
- 重复 timestamp；
- 冲突 OHLCV；
- OHLC 合法性；
- 缺失 minute/session；
- 分红清理；
- manifest 和文件 hash；
- clean 和 backtest-ready 输出。

### 4.2 第一轮审查发现的问题

- `backtest_ready` 对 `interior_gap` 和 `truncated` 太宽松；
- `band_warm` 仍在 prior=13 时变 True；
- 半日市下午被当成缺失分钟；
- halt 只比较 bar 数量，未比较具体分钟集合；
- numeric-string epoch 无法解析；
- dividend symbol alias 和重复事件处理不够严格；
- vendor format regime 被误当成 vendor identification。

### 4.3 session tier 分层

建立：

- `paper_ready`：完整分钟、适合论文复现；
- `halt_aware`：完整日加经验证的官方 halt 日；
- `exploratory`：允许少量数据缺口，仅用于敏感性分析。

同时明确：每个 tier 必须分别报告完整的 CAGR、Sharpe、MDD 等指标，不能把一条权益曲线的 CAGR 和另一条的 MDD 拼在一起。

### 4.4 组件级 validity

进一步发现，不能先过滤 `paper_ready` 再计算特征。实测：

- 删除 2009-07-27 后，2009-07-28 的 previous close 从 98.35 错成 98.07；
- “过去 14 个交易日”的自然跨度从 19 天压成 17 天；
- truncated-open 日下午存在的bar会被误当作有效 `move_open` observation；
- MDD可被低估约1.6个百分点。

因此新增无参数 primitives：

```text
bar_present
open_valid
close_valid
prev_close_valid
daily_ret_valid
move_open_obs_valid
vwap_valid
is_halt_minute
is_executable_minute
is_scheduled_decision_minute
```

关键设计：

- truncated-open：`open_valid=False`，但 `close_valid` 仍可能为 True；
- interior gap：gap 后 `vwap_valid=False`，独立的 `move_open_obs_valid` 仍可能为 True；
- trailing truncation：当日 close 和跨该日 daily return 无效；
- halt minute：不可执行，但不应被当成普通缺失，也不应破坏真实累计 VWAP；
  即使 vendor 在 `allow_present` 模式下保留 phantom bar，也不得进入
  `move_open_obs_valid` 或后续同分钟 `sigma_open` 历史。

### 4.5 发布和审计工程

完成：

- staging 临时目录；
- `runs/<run_id>/`；
- `_SUCCESS`；
- 原子更新 `latest.json`；
- 相对路径 manifest；
- pipeline/schema/version；
- 输入、脚本、输出和报告 SHA-256；
- 极端一分钟收益、stale、zero-volume 和 source-format 异常报告；
- 28 项 data self-test。

### 4.6 data-v1.0 冻结前审计补强

2026-07-30 完成数据层 v5 candidate：

- 新增显式 `expected_start` / `expected_end`，manifest 同时记录观察边界
  和首尾缺失交易日；
- OHLCV duplicate conflict 与 `vendor_bar_vwap` / `transactions` metadata
  conflict 分开报告；headline policy 对 OHLCV 冲突硬失败，transactions
  冲突必须使用明确 source precedence；
- 相邻可得行收益拆成真正连续一分钟、普通 gap 和 halt reopen 三类；
- 新增并强制校验 `requirements.lock`；`bar_label=start` 固定在
  `config/data_release_v1.yml`。

真实 raw parquet 的观察范围是 2008-01-22 至 2026-07-09。最初按
`expected_start=2008-01-01` 探测时，程序正确识别出 2008-01-02 至
2008-01-18 共 13 个前置 XNYS session 缺失并拒绝发布。2026-07-30
正式决定把 data-v1.0 研究起点重定界为 **2008-01-22**：这是原始源中
首个观察到且完整的 XNYS session，由数据可得性决定，不依据策略结果；
以后所有报告必须显示精确起点，不得表述为覆盖完整 2008 年。完整理由见
`DATA_V1_START_DATE_DECISION_ZH.md`。

---

## 5. 回测引擎重构

### 5.1 为什么不能继续使用简单 `position.shift(1)`

简单公式：

```python
position.shift(1) * close.diff()
```

无法正确表达：

- pending order；
- intended fill time；
- next executable open；
- halt；
- queued/cancelled order；
- reversal quantity；
- unknown exit；
- fill-level cost。

因此改成真正的状态机：

```text
signal
→ pending order
→ intended fill
→ actual fill
→ filled position
→ mark-to-market
→ cost/accounting
```

### 5.2 三个 profile

#### `official_sample_compatible`

复制作者 notebook 的主要 conventions：

- `min_periods=13`；
- `d-15..d-2` 日波动窗口；
- NaN vol → 4x；
- `round()` 股数；
- 无滑点；
- row-based rolling；
- shifted close / signal-bar-close 习惯。

它是**约定兼容**，不是逐行 bit parity。真正 parity 必须逐日核对 shares、signal、exposure、trade units、commission、gross、net 和 AUM。

#### `paper_spec`

按论文文字：

- 严格 14-session window；
- 现金分红调整 previous close；
- commission USD 0.0035/share；
- slippage USD 0.001/share；
- floor share sizing；
- 论文式 signal-bar-close 执行假设。

#### `corrected_execution`

在 `paper_spec` 信号基础上加入：

- next executable open fill；
- pending-order state machine；
- halt cancel/queue policy；
- 真实 reversal units；
- component validity；
- 独立 commission/slippage；
- calendarized statistics。

### 5.3 参数 sweep 静默失效的修复

原数据层聚合字段 `signal_valid` 烧入默认：

- trade frequency；
- sigma window；
- VWAP；
- vol targeting。

导致 `trade_freq=15` 仍只在 30 分钟决策。

修复：

- 字段改名 `signal_valid_default_config`；
- 引擎使用 `config_validity()` 从 primitives 根据当前 `Cfg` 重建 mask；
- 可选 `require_config_match=True` 强制参数一致。

### 5.4 Halt 和成交语义

明确四种情况：

| 情况 | 是否承担复牌gap |
|---|---|
| halt前已成交持仓 | 是 |
| halt前产生信号、下一分钟不可成交并取消 | 否 |
| 订单排队至复牌成交 | 成交前否，成交后开始 |
| halt内信号 | 禁止 |

已有仓位的复牌跳空必须计入；不能因为halt没有bar就抹掉风险。

### 5.5 反手和成本

发现 `0 → +1 → -1 → 0` 曾被计为3个trade units，实际应为4：

- 开多1；
- 多翻空2；
- 平空1。

因此拆分：

```text
fill_events
trade_units
shares_traded
```

成本：

```text
commission = Σ max(minimum commission, commission/share × quantity)
slippage   = Σ slippage/share × quantity
```

最低佣金只作用于commission，不吞并slippage。

### 5.6 严格 eligible-session rolling

修正：

- minute 缺失仍占 fixed rolling slot；
- 整日 absent 仍占 daily rolling slot；
- 半日市下午不属于eligible grid；
- invalid daily return 不进入 volatility window；
- 不向前取更老观察补足14个有效值。

### 5.7 其他修复

完成：

- final scheduled bar 禁止新开仓，避免零PnL双成本round trip；
- `exec_lag_minutes>1` 按真正due minute成交；
- `ignore_dividends=True` 真正忽略已有文件；
- `share_rounding` 从配置读取；
- Cfg字段使用审计，未读取字段测试直接失败；
- unknown exit 不进入headline，也不污染后续AUM；
- calendar CAGR按实际日期跨度；
- 删除错误年化的 `Sharpe_active`；
- 只将 `Sharpe_calendar` 作为主Sharpe；
- benchmark屏蔽invalid close并加入首日anchor；
- 枚举和参数在读取数据前校验；
- 62项 engine test + 28项 data self-test（2026-07-30本工作区实跑）。

在本次交付环境中，三个最新 Python 文件均通过 `py_compile`。完整测试结果沿用项目方提供的最新运行记录。

---

## 6. 当前固定基线

**注意：以下是零分红、零融资/借券成本的 engine-mechanics baseline。**

| Profile | Total Return | CAGR | Vol | Sharpe | MDD | Trade Units | Shares Traded | Cost/Share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| official_sample_compatible | 1714.2% | 17.04% | 14.63% | 1.15 | −24.5% | 8,304 | 40,593,898 | 0.35¢ |
| paper_spec | 1632.9% | 16.76% | 14.29% | 1.16 | −26.8% | 8,236 | 39,581,998 | 0.45¢ |
| corrected_execution | 1053.7% | 14.21% | 14.30% | 1.01 | −30.3% | 8,236 | 30,345,204 | 0.85¢ |

### 主要归因

- sample → paper：窗口、NaN vol 4x、slippage、validity 等；
- paper → corrected：next-open成交、halt pending order、较高执行滑点和真实成交路径。

结论：原始sample并非完全由bug制造收益，但执行假设明显乐观；诚实执行后Sharpe降至约1.0，MDD约-30%。

---

## 7. Benchmark研究

已修复：

- 不使用 trailing-truncated session 的最后可得bar冒充收盘；
- `pct_change(fill_method=None)`；
- evaluation首日前增加一个有效close anchor；
- strategy和benchmark日期对齐；
- 输出valid/missing/aligned session数。

零分红基线：

| Profile | SPY Price CAGR | Strategy CAGR | Excess | Beta | Alpha | IR |
|---|---:|---:|---:|---:|---:|---:|
| official | 9.81% | 17.04% | +7.24pp | −0.072 | 17.65% | 0.22 |
| paper_spec | 9.66% | 16.76% | +7.10pp | −0.064 | 17.36% | 0.21 |
| corrected | 9.66% | 14.21% | +4.55pp | −0.064 | 15.13% | 0.13 |

目前 `dividends_used=False`，所以 total return 等于 price return，excess 和 IR 仍被高估。正式报告应使用真实分红和独立 daily SPY raw-close benchmark。

---

## 8. 预注册的 post-publication 评估

已建立 `evaluation_spec_v1.yml`，固定：

- evaluation start：2024-05-01；
- 三个profile；
- `paper_ready / halt_aware / exploratory`；
- with/ignore dividends；
- slippage grid：0.001 / 0.0025 / 0.005 / 0.010；
- long/short、进场时段、波动regime、signal count、持仓时间、turnover、gross edge/share 和 cost/share；
- 必需的spec、engine、data、source、dividend和Git hash。

核心决策规则：

```text
post-publication gross edge/share ≈ 0
→ 信号已消失，不得继续参数优化。

gross edge/share > 0，但被执行、融资和借券成本吞掉
→ 才研究执行、频率和容量。
```

这段只能称为 **post-publication evaluation period**，不能称为 untouched OOS，因为我们已看过早期结果并据此修正过方法。

---

## 9. 当前仍未完成的工作

### 9.1 真实分红

需要完整 SPY ex-dividend 文件，正式双跑：

- with dividends；
- ignore dividends。

正式headline必须使用with-dividends。

### 9.2 可执行 evaluation runner

当前spec仍是文档，尚未形成唯一正式入口。需实现：

```text
spec读取/hash
→ profile × tier × dividend × cost grid
→ subperiod
→ completeness check
→ 原子发布
→ 不覆盖既有同spec报告
```

### 9.3 Signals / fills / round-trip ledger

为完成预注册分解，需要输出：

- signals；
- orders/fills；
- round trips。

否则不能严格归因long/short、entry bucket、cancelled signals和holding time。

### 9.4 融资 time-integral

当前有现金、funding和borrow rate接口，但最新审查指出不能使用 `avg_signed_notional` 让日内多头和空头互相抵消。需分别累计：

```text
positive_cash_time_integral
borrowed_cash_time_integral
long_notional_time_integral
short_notional_time_integral
```

跨halt持仓期间也必须继续累计融资和借券时间。

### 9.5 独立daily benchmark

分钟数据有2个无效收盘日，只能标NaN，无法恢复真实收盘。正式报告应加入独立daily raw-close数据及其hash。

### 9.6 evaluation spec v2

正式出结果前还应冻结：

- evaluation end / frozen data run；
- post-period capital reset还是连续AUM；
- replication headline与economic headline；
- slippage grid具体作用于哪个profile；
- cash/funding/borrow rate来源；
- volatility quintile只用pre-publication边界。

v2统计口径已决定为只报告点估计；bootstrap和HAC置信区间明确延期，
不作为本轮正式发布门槛，报告也不得暗示已提供统计置信区间。

---

## 10. 当前研究结论

1. **作者sample结果偏乐观，但并非完全由bug制造。**
2. **严格论文口径仍有统计收益。**
3. **真实执行显著降低收益和Sharpe。**
4. **全样本CAGR不是最终决策变量。**
5. 真正关键的是：

```text
2024-05-01以后
每股毛边际
− execution cost/share
− financing/share
− borrow/share
```

如果毛边际本身接近零，参数优化应停止；如果毛边际为正但成本吞噬，才值得优化执行与容量。

### 10.1 evaluation v2 正式结果（2026-07-31）

正式 run：
`20260731T043943Z_formal_spec2_b4d7a8f805b9`。
完整结果、hash 和审计见
`POST_PUBLICATION_EVALUATION_V2_ZH.md` 与
`POST_PUBLICATION_EVALUATION_V2.html`。

Headline
`corrected_execution × paper_ready × with_dividends × $0.005/share`
在 post-publication window 的 CAGR 为 7.01%，Sharpe 为 0.27，
MDD 为 −17.48%；同期 SPY total-return CAGR 为 21.74%，
excess CAGR 为 −14.73pp。gross edge 为 2.139¢/share，
扣 execution 0.850¢/share 与 funding/borrow 0.136¢/share 后，
trading edge 仍为 +1.153¢/share。

因此正式结论是：post 的信号毛边际没有归零，也没有被当前固定成本完全吞掉，
但风险调整收益很弱，并且大幅跑输同期 SPY。下一步如继续研究，应集中在
market impact、容量和执行模型，而不是参数优化。

### 10.2 halt-aware × $0.0025 headline amendment 与归因（2026-08-01）

按 2026-07-31 的 post-result reporting amendment，主要经济展示口径改为
`corrected_execution × halt_aware × with_dividends × $0.0025/share`。
原始 frozen v2 及其 `$0.005 paper_ready` headline 保持不变。

clean-state 正式 run：
`20260731T200227Z_formal_spec2_58205b0c130f`，commit `050b031`，
spec SHA-256 `58205b0c130f...`。72 cells / 216 rows 全部完成；与此前 headline
variant 矩阵按 cell key 对账，所有数值差异为 0。

新 headline full / pre / post CAGR 分别为 16.70% / 18.00% / 7.52%，
Sharpe 为 1.07 / 1.18 / 0.30。post 的主要毛收益来自 short；execution、
funding 和 borrow 没有吞掉全部交易边际，但现金收益对 post 累计回报贡献明显。

已新增完整交互归因报告，覆盖 long/short、commission、slippage、cash interest、
leveraged funding、SPY borrow、SPY total-return 相关性、rolling correlation、
最差/最好季度和年度情景。详见
`POST_PUBLICATION_EVALUATION_V2_HALT0025_ZH.md` 与
`POST_PUBLICATION_EVALUATION_V2_HALT0025_ATTRIBUTION.html`。

2026-08-01 对报告口径进一步修正：正式 portfolio CAGR 与 same-path
trading-only CAGR 必须并列，cash interest 单独显示。Pre / post 的 cash-interest
年化分别为 0.88% / 4.06%，占窗口简单加总收益 5.0% / 48.6%；post 的
portfolio CAGR 7.52% 中含明显高利率 cash carry，而 trading-only CAGR 仅
3.27%。因此 7.52% 不再单独作为策略表现 headline。

交互报告同时扩展资本投入、持仓时杠杆、active-day、long/short round trips、
冻结 entry-time buckets、lagged-volatility quintiles、年度 signal/entry 数量、
cash diagnostics 和极端日利润集中度。Post 最佳单个 trading day 占盈利日
trading P&L 的 7.5%；将该日 trading-only return 置零后，post trading-only
累计收益由 +7.29% 变为 −2.04%，显示结果对少数极端日较敏感。

报告 v3 进一步把动态生成的卡片、表头、图例、季度事件标签和说明统一为中文；
仅保留 CAGR、SPY、AUM、Beta 等行业缩写及底部可审计的原始字段名。

### 10.3 杠杆与仓位规则敏感性（2026-08-02）

在 frozen v2 之后运行三组明确标为 post-result 的 sizing sensitivity：取消 4x
上限的无上限压力测试、1x 不使用杠杆测试，以及恒定 2x 对 Paper inverse-vol
动态定仓。三组共同固定 `corrected_execution × halt_aware × with-dividends ×
$0.0025/share`、point-in-time financing 和连续 AUM 路径；4x baseline 均与正式
72-cell run 数值完全一致。

主要结论：无上限只小幅提高 CAGR，却把机械目标杠杆推到最高 13.27x；1x 显著
降低回撤，但 post trading-only CAGR 仅 0.15%；恒定 2x 的 full CAGR 17.22%
略高于 Paper 16.70%，但该优势由 2008 驱动，17 个逐年移动起点中只胜 1 个，
从 2009 开始 Paper / 恒定 2x CAGR 为 14.64% / 12.40%。Post Paper / 恒定 2x
trading-only CAGR 为 3.27% / 0.47%。因此不修改当前动态定仓和 4x cap。

完整口径、结果、provenance、起点敏感性和限制见
`LEVERAGE_SIZING_SENSITIVITY_ZH.md` 与
`LEVERAGE_SIZING_SENSITIVITY_ZH.html`。

---

## 11. 项目当前状态

### 基本冻结

- 时间戳解析和 component-validity 语义；
- XNYS calendar / half-day / halt；
- component validity primitives；
- 三profile；
- strict rolling；
- order/fill核心状态机；
- reversal和成本；
- unknown exit；
- calendar statistics；
- benchmark基本对齐；
- data/engine测试框架。

### 尚未冻结

- data-v1.0 不可变发布目录与 Git tag；
- 完整融资time-integral；
- trade/fill ledger；
- executable evaluation runner；
- 独立daily benchmark；
- evaluation spec v2；
- post-publication最终报告。

### 明确暂缓

- 参数优化；
- Qlib；
- 机器学习；
- 实盘部署。

---

## 12. 贯穿项目的原则

本项目所有优化不是以提高回报为目标，而是以消除以下问题为目标：

- 静默错误；
- 不可执行成交；
- 错误数据窗口；
- 缺失数据带来的隐性未来函数；
- 不一致的成本和benchmark；
- 无法复现或无法归因的结果。

最终工作流：

```text
完整交易日历
→ 数据质量审计
→ component validity
→ config-specific features
→ signal
→ order
→ fill
→ marked position
→ costs/accounting
→ calendarized performance
→ frozen post-publication evaluation
```
