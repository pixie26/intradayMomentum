# 日内动量研究框架：SPY 冻结评估与 QQQ 探索性扩展

本项目复现并审计《Beat the Market: An Effective Intraday Momentum Strategy for S&P 500 ETF (SPY)》中的一分钟日内动量策略。研究重点不是生成一条更漂亮的历史净值曲线，而是回答一个更严格的问题：

> 在修正数据、特征窗口、成交时点、交易成本、融资、借券和 benchmark 后，论文公开日 `2024-05-01` 之后的信号，是否仍有可交易的每股毛边际？

当前结论是：**SPY post-publication 毛交易边际没有归零，扣除冻结的执行、融资和借券假设后仍为正；但风险调整收益很弱，并大幅跑输同期 SPY total return。** `pre Sharpe 1.18 → post 0.30` 是方向性下降，但后续 HAC 与 block bootstrap 显示估计很不精确，不能称为“统计显著衰减”。这还不是可直接实盘的策略。下一阶段应先验证 market impact、容量、收盘执行和部分成交，而不是根据已经看过的 post 结果调参。

SPY 冻结样本为 `2008-01-22` 至 `2026-07-09`；2008 年和 2026 年都是部分年度。本文所称 post-publication 是**论文公开后的评估期**，不是 untouched out-of-sample。仓库还包含 `2007-04-25` 至 `2026-07-31` 的 QQQ 探索性扩展；它验证跨资产迁移，但尚未达到正式发布标准。

> 本仓库是研究与审计项目，不构成投资建议。

## 目录

- [研究问题与策略](#研究问题与策略)
- [方法论](#方法论)
- [目前完成了什么](#目前完成了什么)
- [核心结果](#核心结果)
- [稳健性与跨资产扩展](#稳健性与跨资产扩展)
- [主要 findings](#主要-findings)
- [已知问题与研究边界](#已知问题与研究边界)
- [下一步规划](#下一步规划)
- [复现与验证](#复现与验证)
- [文档与报告导航](#文档与报告导航)
- [仓库结构](#仓库结构)

## 研究问题与策略

策略每 30 分钟比较 SPY 当前价格、动态 noise band 和当日累计 VWAP：

```text
close > upper band 且 close > cumulative VWAP  → long
close < lower band 且 close < cumulative VWAP  → short
否则                                            → flat
```

动态 band 使用当日开盘、前一交易日经现金分红调整的收盘价，以及过去 14 个交易日“同一分钟相对开盘的绝对移动”。仓位使用过去 14 个已完成交易日的日波动率，目标日波动率为 2%，杠杆上限为 4 倍，日终归零。

公式本身并不复杂，真正影响结果的是实现语义：过去 14 日是否允许跳过缺失观察、信号 bar 是否能在同一 close 成交、halt 期间订单是否已持仓、反手是否按两倍数量收费、15:59 minute close 能否代表 closing auction，以及空头所得现金是否计息。本项目把这些问题拆开研究，而不是把它们藏进一条收益曲线。

策略、论文原文和逐分钟时间线的完整说明见 [交易逻辑与实现详解](docs/PAPER_TRADING_LOGIC_AND_IMPLEMENTATION_ZH.md)。

## 方法论

### 研究工作流

```mermaid
flowchart LR
    A["原始分钟数据\n只读、不填充"] --> B["XNYS 日历与数据审计"]
    B --> C["data-v1.0\n不可变数据发布"]
    C --> D["全交易日历上的\ncomponent validity 与特征"]
    D --> E["三种 profile\n论文 / sample / 现实执行"]
    E --> F["信号 → 订单 → 成交 → 持仓\n→ P&L → 成本与融资"]
    F --> G["72-cell 冻结评估矩阵"]
    G --> H["逐日、成交与 round-trip 归因"]
    H --> I["独立且不覆盖冻结结果的\n执行敏感性实验"]
```

贯穿全项目的原则是：**先冻结定义和信息集，再运行；先解释毛收益和交易路径，再解释净收益；不使用事后变量冒充可交易过滤器。**

### 1. 数据层：先证明输入能用于研究

冻结的 `data-v1.0` 使用 XNYS 交易日历、start-labelled minute bars 和 State Street 六位精度现金分红。原始 OHLCV 不复权、不 forward-fill、不在原文件上清洗。所有转换由代码完成，并通过 manifest、SHA-256、`_SUCCESS` 和双跑字节一致性留痕。

| 项目 | 冻结数据事实 |
|---|---:|
| 原始行数 | 1,813,340 |
| 清洁 RTH 行数 | 1,804,283 |
| 明确剔除并留档的 RTH 外行数 | 9,057 |
| 观察到的 XNYS sessions | 4,645 |
| `paper_ready` sessions | 4,635 |
| `halt_aware` sessions | 4,639 |
| 完整日 / 官方 halt 日 / interior-gap 日 / truncated 日 | 4,635 / 4 / 3 / 3 |
| 现金分红事件 | 74 |
| 未解释 OHLCV conflict / off-grid / invalid OHLC | 0 / 0 / 0 |

关键数据决定：

- `2008-01-22` 是原始源中第一个观察到且完整的 XNYS session；此前 13 个 XNYS sessions 不属于 data-v1.0 声明区间，也没有被填充。
- 特征始终在**完整交易日历**上计算，session tier 最后才作为交易 mask 应用。先删“坏日”再滚动，会悄悄改变 previous close 和 14-session window。
- `open_valid`、`close_valid`、`move_open_obs_valid`、`vwap_valid`、`is_halt_minute` 等按依赖关系分别定义；一个单一的 `valid` flag 无法正确表示数据可用性。
- 四个 2020 年熔断日的 halt minute 不可成交；halt 前已成交的持仓承担完整复牌 gap，未成交订单不获得该 gap。
- `2016-01-04` 前后存在数据源/成交量口径切换。pre-2016 volume 是百股整手口径，乘 100 后仍较独立日线低约 10%–30%；未经 regime 处理，不能用于跨期容量或冲击成本比较。

详细契约与实际异常见 [数据层说明](docs/README_DATA.md)、[data-v1.0 冻结验收](docs/DATA_V1_FREEZE_AUDIT_20260730_ZH.md) 和 [首次全量数据审计](docs/DATA_AUDIT_20260729_ZH.md)。

### 2. 三个 profile：不要把论文和作者 sample 混在一起

| Profile | 回答的问题 | 主要语义 | 是否用于经济 headline |
|---|---|---|---:|
| `official_sample_compatible` | 作者 notebook 的主要 conventions 会产生什么结果？ | 13-observation warm-up、`d-15..d-2` 日波动窗口、NaN vol → 4×、`round()`、无 slippage、row-based rolling | 否 |
| `paper_spec` | 严格按论文文字实现会怎样？ | 14-session window、分红调整、论文 commission/slippage、signal-bar-close 成交 | 否，复现用途 |
| `corrected_execution` | 保留论文信号、但采用更诚实的成交和会计后会怎样？ | next executable open、pending-order 状态机、halt-aware marking、真实反手数量、逐项成本、点时融资 | 是 |

`corrected_execution` 不再使用简单的 `position.shift(1) * close.diff()`，而是显式维护：

```text
signal → pending order → intended fill → actual fill
       → filled position → mark-to-market → cost/accounting
```

完整差异矩阵见 [论文、sample 与当前实现逐项比较](docs/PAPER_SAMPLE_CURRENT_COMPARISON_ZH.md)，引擎契约见 [引擎说明](docs/README_ENGINE.md)。

### 3. 三个 session tier：数据完整性与经济真实性分开报告

| Tier | 用途 |
|---|---|
| `paper_ready` | 完整分钟数据；作者 sample / 论文复现与数据完整性基准 |
| `halt_aware` | 在完整日之外保留四个已验证熔断日；当前主要经济解释 |
| `exploratory` | 允许明确记录的 gap/truncation；仅作敏感性分析 |

每个 tier 都生成独立权益曲线和完整指标。不能把一个 tier 的 CAGR 与另一个 tier 的 MDD 拼在一起。

### 4. 冻结的 post-publication 评估设计

正式 v2 不是挑一个最好看的组合，而是完整运行：

```text
3 profiles × 3 tiers × 2 dividend modes × 4 slippage levels
= 72 cells

72 cells × 3 subperiods
= 216 summary rows
```

| 设计项 | 冻结定义 |
|---|---|
| Full sample | `2008-01-22` 至 `2026-07-09`，特征 warm-up 后开始有收益 |
| Pre-publication | 截至 `2024-04-30` |
| Post-publication | `2024-05-01` 至 `2026-07-09` |
| 资本路径 | 从完整样本连续复利，再切片报告；post 不重置资本 |
| Benchmark | 独立 Yahoo raw daily close + State Street distributions 的 SPY total return |
| 正现金 | session open 前可得的 LIBOR proxy / SOFR − 50 bps，ACT/360 |
| 借入现金 | 同一 PIT benchmark + 100 bps |
| SPY borrow | 固定 25 bps p.a. |
| 统计 | frozen v2 保留点估计；事后附录完成 8,000 次 circular block bootstrap 与 Newey-West/HAC 交叉核验 |
| 决策变量 | post gross edge/share 是否高于 execution + funding + borrow cost/share |

每个正式 run 都强制记录 spec、engine、data、dividend、benchmark、financing 和 Git hash；发布前检查 cell/row 完整性及逐日会计恒等式。

## 目前完成了什么

| 工作流 | 状态 | 产物 / 证据 |
|---|---:|---|
| 论文与作者 sample 的语义审计 | 完成 | 35 项逐项比较、三 profile 设计 |
| 分钟数据审计与 component validity | 完成 | 28 项 data self-test、异常分类与审计表 |
| `data-v1.0` 冻结 | 完成 | 两次 clean-HEAD 正式运行、19 个 deterministic files 字节一致、不可变本地 release |
| 独立 SPY daily benchmark | 完成 | raw daily close 独立 release，分红来自 data-v1.0 |
| 回测状态机与账户核算 | 完成 | signals、orders、fills、round trips、commission、slippage、cash、funding、borrow |
| 引擎测试 | 完成 | 当前 86 项 engine checks；另有 evaluation 与归因报告测试 |
| 论文 Q24 月收益复现 | 完成 | 18-cell 独立 replication matrix 与交互报告 |
| evaluation spec v2 与 runner | 完成并冻结 | 72/72 cells、216/216 rows，原子发布与 hash 清单 |
| 原始 v2 正式报告 | 完成 | frozen `paper_ready × $0.005/share` headline |
| halt-aware 报告修订与 P&L 归因 | 完成 | 保留原始 headline，新增 post-result reporting amendment 与交互式归因 |
| EOD close-source 敏感性 | 探索性完成 | daily-close/MOC proxy、0.5/1 bp auction cost、5/10/15/30-minute TWAP |
| Sharpe 统计不确定性 | 事后附录完成 | 8,000 次、20-session circular block bootstrap；HAC 交叉核验；不改 frozen v2 |
| 杠杆与定仓敏感性 | 事后完成 | 1× cap、无上限、恒定 2×；支持保留动态定仓与 4× cap |
| SEC Section 31 | 事后完成 | 按卖出名义金额与历史费率逐笔计费；仍非 all-in IBKR 模型 |
| QQQ 跨资产扩展 | 探索性完成 | 数据审计、股息/融资重跑、归因与统计附录；尚无 frozen QQQ spec |
| Market impact / capacity | 待完成 | 需要 participation、ADV、波动率、时段和真实成交证据 |
| Queue position / partial fill | 待完成 | 当前固定 per-share slippage 无法表达 |

重要的 provenance 规则：**冻结 artifact 不覆盖；看过结果后改变展示口径时，必须标为 post-result reporting amendment；实验写入新路径，不能重写正式 run。**

## 核心结果

### 1. 原始 frozen v2 headline

事前冻结的正式 headline 是：

```text
corrected_execution × paper_ready × with_dividends × $0.005/share slippage
```

| 窗口 | 策略 CAGR | SPY total-return CAGR | Excess CAGR | Sharpe vs cash | MDD |
|---|---:|---:|---:|---:|---:|
| Full sample | 15.35% | 11.96% | +3.39pp | 0.99 | −30.22% |
| Pre-publication | 16.52% | 10.70% | +5.82pp | 1.09 | −30.22% |
| Post-publication | 7.01% | 21.74% | −14.73pp | 0.27 | −17.48% |

Post-publication 每成交股分解：

| 项目 | USD/share |
|---|---:|
| Gross edge | +2.139¢ |
| Commission + slippage | −0.850¢ |
| Funding + borrow | −0.136¢ |
| Trading edge after costs | **+1.153¢** |

因此不能简单说“成本把信号吃完了”。信号毛边际和扣固定成本后的交易边际仍为正，但 post 的 Sharpe 只有 0.27，且 CAGR 比同期 SPY total return 低 14.73 个百分点。正式 provenance、完整矩阵和被替代的早期 run 说明见 [v2 正式结果](docs/POST_PUBLICATION_EVALUATION_V2_ZH.md)；可读 HTML 见 [正式报告](docs/POST_PUBLICATION_EVALUATION_V2.html)。

### 2. Post-result reporting amendment

看过原始 v2 结果后，项目把下面的 cell 指定为主要经济展示口径：

```text
corrected_execution × halt_aware × with_dividends × $0.0025/share slippage
```

这是**报告展示修订，不是新的事前 headline**。原始 frozen headline 保持不变；完整 72-cell 矩阵也没有改变。clean-state 正式重跑的 72/72 cells 与此前矩阵按 cell key 对账，所有数值列最大差异为 `0.0`。

| 窗口 | 策略 CAGR | SPY total-return CAGR | Excess CAGR | Sharpe vs cash | MDD |
|---|---:|---:|---:|---:|---:|
| Full sample | 16.70% | 11.96% | +4.74pp | 1.07 | −28.26% |
| Pre-publication | 18.00% | 10.70% | +7.30pp | 1.18 | −28.26% |
| Post-publication | 7.52% | 21.74% | −14.22pp | 0.30 | −17.06% |

Headline 改变了展示成本和 tier，但没有改变研究判断：post 毛边际仍在，风险调整收益弱，且显著跑输 SPY。

### 3. Post-publication P&L 归因

下表使用逐日复利链接，所有组件精确加总到 post 累计收益 `17.19%`：

| 组件 | 美元 P&L | Linked return contribution |
|---|---:|---:|
| Long gross | +$27,526 | +1.68pp |
| Short gross | +$124,629 | +9.73pp |
| Commission | −$25,130 | −1.45pp |
| Slippage | −$17,950 | −1.04pp |
| Cash interest | +$152,791 | +8.84pp |
| Leveraged funding | −$9,198 | −0.54pp |
| SPY borrow | −$549 | −0.03pp |
| **合计** | **+$252,118** | **+17.19pp** |

Post 毛交易收益主要来自 short。剔除 cash-interest 组件后，同一已实现持仓路径的 linked trading contribution 约为 `+8.35pp`；这不是重新回测的自融资组合。现金收益对最终回报贡献很大，因此不能把 headline CAGR 全部解释成交易 alpha。

完整说明见 [halt-aware amendment](docs/POST_PUBLICATION_EVALUATION_V2_HALT0025_ZH.md)。交互报告可按日期查看 NAV、long/short、成本、融资、与 SPY 的相关性、rolling correlation、季度和年度归因；请下载 [完整 P&L 归因 HTML](docs/POST_PUBLICATION_EVALUATION_V2_HALT0025_ATTRIBUTION.html) 后在浏览器打开。

### 4. EOD close-source 探索性实验

冻结模型使用最后一个计划分钟（完整日通常是 15:59）的 close 日终平仓。由于它不一定等于 official closing auction，项目在**不覆盖 v2**的独立实验中，只改变 EOD 退出价格和明确列示的增量成本：

| EOD 方案 | Full CAGR | Post CAGR | Post Sharpe | Post MDD | vs 15:59 Post CAGR |
|---|---:|---:|---:|---:|---:|
| 15:59 minute close | 16.70% | 7.52% | 0.30 | −17.1% | +0.00pp |
| Independent daily close，无额外 auction cost | 16.91% | 9.11% | 0.40 | −16.4% | +1.59pp |
| Independent daily close + 0.5 bp | 15.82% | 8.00% | 0.33 | −17.3% | +0.48pp |
| Independent daily close + 1 bp | 14.73% | 6.90% | 0.26 | −18.3% | −0.62pp |
| Last 10 scheduled-minute TWAP | 14.87% | 8.11% | 0.35 | −13.7% | +0.59pp |
| Last 30 scheduled-minute TWAP | 13.81% | 9.87% | 0.47 | −11.6% | +2.35pp |

这个实验说明 EOD 价格来源具有一阶经济影响，但不能事后选择 30-minute TWAP 或 daily close：所有 TWAP 在 post 高于基线，却在 full/pre 明显更差；daily-close 优势在约 `0.72 bp` 的局部插值 auction cost 处消失。独立 Yahoo daily close 只是 cross-source proxy，不是真实 MOC fill；2025-04-09 的两个 close 相差 `$5.25`，也说明必须取得 official auction / TAQ 证据。

详见 [EOD 实验说明](docs/EOD_CLOSE_SOURCE_EXPERIMENT_V1_ZH.md) 和 [可读 HTML 报告](docs/EOD_CLOSE_SOURCE_EXPERIMENT_V1_ZH.html)。

## 稳健性与跨资产扩展

以下工作都发生在观察 frozen v2 结果之后，因此用于约束解释或测试可迁移性，不回写原 headline。

### 1. 统计不确定性

对 halt-aware amendment 的精确账本进行 8,000 次、20-session circular moving-block bootstrap，并以 Newey-West/HAC 交叉核验：

| 统计量 | 点估计 | 90% bootstrap 区间 | 90% HAC 区间 |
|---|---:|---:|---:|
| Pre Sharpe | 1.184 | [0.831, 1.528] | [0.833, 1.535] |
| Post Sharpe | 0.300 | [−0.748, 1.281] | [−0.666, 1.266] |
| Pre − post | 0.883 | [−0.163, 1.978] | [−0.145, 1.911] |

差值区间包含 0；当前 548 个 post sessions 同时与“真实下降”和“较差的一次实现”相容。正确措辞是**点估计下降但精度不足**，不是“已证明无衰减”，也不是“显著衰减”。详见[统计不确定性附录](docs/STATISTICAL_UNCERTAINTY_V1_ZH.md)。

### 2. 成本与定仓边界

- 历史 SEC Section 31 按卖出名义金额加入后，SPY post portfolio CAGR 从 `7.52%` 降至 `6.62%`，same-path trading-only CAGR 从 `3.27%` 降至 `2.41%`。这仍不是 all-in IBKR 成本模型；TAF、CAT、venue、auction、impact 与 partial fills 仍未建模。详见 [Section 31 敏感性](docs/IBKR_SECTION31_COST_SENSITIVITY_ZH.md)。
- 取消 4× cap 只带来很小的平均收益改善，却显著增加极端杠杆和尾部风险；1× cap 可压低风险，但 post 交易边际接近消失；恒定 2× 的全样本优势对 2008 起点敏感。因此保留 frozen Paper 动态定仓与 4× cap。详见[杠杆与定仓报告](docs/LEVERAGE_SIZING_SENSITIVITY_ZH.md)。

### 3. QQQ 探索性扩展

同一框架已迁移到 XNAS/QQQ，加入 81 个交叉验证股息事件与覆盖 `2007-04-25` 至 `2026-07-31` 的 financing-rates-v2。与 SPY amendment 类似的 `corrected_execution × halt_aware × $0.0025/share` cell 在 post 期的结果为：

| 口径 | Post CAGR / 年化 | Sharpe | MDD |
|---|---:|---:|---:|
| QQQ portfolio（含现金利息） | 18.35% | 0.99 | −13.8% |
| 同路径 trading-only | 13.76% | — | — |
| 现金利息年化 | 3.98% | — | — |
| QQQ total-return benchmark | 24.66% | — | — |

QQQ 的 pre/post Sharpe 点估计约为 `0.995 / 0.994`，没有观察到 SPY 式点估计下降，但策略仍跑输 QQQ total return，且 18.35% 中包含大量现金 carry。该实验还缺 frozen QQQ spec、clean deterministic rerun、第二分钟源和最终 invalid-row 裁决，不能称为正式 OOS 或直接与 SPY frozen v2 等级等同。详见 [QQQ 数据审计](docs/QQQ_DATA_AUDIT_20260804_ZH.md)、[带股息/融资重跑摘要](experiments/qqq_with_dividends_v1/SUMMARY_ZH.md)与[交互式归因报告](experiments/qqq_with_dividends_v1/QQQ_ATTRIBUTION.html)。

## 主要 findings

1. **作者 sample 偏乐观，但策略不是由单一 bug 制造。** 修正 14-session window、NaN vol → 4×、slippage、row-based rolling 和同 bar 成交后，全样本表现下降，但论文型信号仍有历史收益。

2. **现实执行是主要差异来源之一。** `paper_spec` 到 `corrected_execution` 的变化来自 next-open 成交、pending orders、halt 语义、真实反手数量和成本路径，而不是重新拟合信号。

3. **论文 Q24 可较接近复现，但不是 bit parity。** 18-cell replication 中，`paper_spec × halt_aware × with_dividends` 在 206 个严格可比月份的 MAE 为 `0.3059` 个百分点；忽略分红为 `0.3539`。这支持 halt-aware 与分红锚点语义，但数据源、起始日期和部分月份仍不同。详见 [Q24 replication](experiments/paper_replication_v1/README.md)。

4. **SPY post-publication 不是“信号完全死亡”，但下降幅度估计不精确。** 毛边际仍为正，当前固定执行与融资成本也未完全吞掉它；Sharpe 点估计从 1.18 降至 0.30、excess CAGR 为负且对现金收益依赖较高，但 bootstrap/HAC 的差值区间包含 0。描述性弱化成立，统计显著性不成立。

5. **亏损年份首先是毛 P&L 问题，不是成本故事。** 对 2016、2017 和 2026 YTD 的正式账本归因显示，即使去掉 commission/slippage，策略仍分别约为 `−11.8%`、`−5.9%`、`−3.3%`；2016/2017 的空头假突破尤其严重。2017 年滞后波动率很低，约 76.1% 的交易日触及 4× cap，窄 band 与高杠杆放大了均值回归损失。

6. **策略需要方向持续性，而不是简单的“高 VIX”或“低 VIX”。** 探索性诊断中，前一日 VIX 和滞后 14 日波动率对亏损没有稳定预测力；当天方向效率 `abs(close-open)/(high-low)` 与盈亏关系更强，但它使用收盘后信息，只能解释，不能直接作为无泄漏过滤器。若继续研究，应在新的事前冻结设计中构造“截至每个 30 分钟决策点已知”的路径曲折度或突破持续度。

7. **Halt 不是普通缺失数据。** 已持仓者承担复牌 gap；未成交订单不承担。保留四个官方熔断日几乎不改变本次 post headline，但这个语义对尾部风险和跨资产推广仍然关键。

8. **分红对 benchmark 比对策略更重要。** 分红主要改变策略的 previous-close band anchor，但会长期抬高 SPY total return benchmark；忽略分红会系统性高估策略 excess return。

9. **QQQ 说明资产迁移可能改变点估计，却没有解决 beta 比较。** QQQ post Sharpe 没有观察到衰减，但 portfolio 和 trading-only CAGR 都低于 QQQ total return；这支持继续研究信号机制，不支持把 QQQ 事后结果升级为正式 headline。

10. **全样本 CAGR 不是最终决策变量。** 当前固定的研究判断是：

   ```text
   post gross edge/share ≈ 0
   → 信号消失，停止参数优化

   post gross edge/share > 0，但被可验证成本吞掉
   → 研究执行、容量和交易频率
   ```

## 已知问题与研究边界

| 问题 | 为什么重要 | 当前处理 |
|---|---|---|
| Market impact 与 capacity 未建模 | 固定 per-share slippage 无法随 AUM、ADV、参与率、时段和波动变化 | headline 仍视为 impact 前上限；列为最高优先级 |
| Queue position 与 partial fills 未建模 | next-open 全量成交可能不现实 | 待引入订单类型、参与率和成交概率 |
| EOD/MOC 成交证据不足 | 15:59 minute close、daily close 与 auction print 不等价 | 保留 frozen v2；独立敏感性已完成，等待 official auction/TAQ |
| Short proceeds 利息依赖账户类型 | 当前模型接近可获得 rebate 的机构账户，可能优于 retail | 后续做 institutional / conservative / no-rebate 场景 |
| Post 样本统计精度低 | 548 个 SPY post sessions 且收益高度集中于少数尾部日 | 已完成 Sharpe bootstrap/HAC；差值区间含 0；alpha、edge/share 与事件到达率仍需区间估计 |
| Post 期已被观察 | 任何继续调参都会污染“未见样本”叙事 | 只称 post-publication evaluation；新假设需新冻结区间 |
| 2016 数据源/volume regime 切换 | 跨期 volume、impact 与 capacity 比较会失真 | 单日 VWAP 可用；容量研究必须分 regime |
| Pre-2016 除息日开盘存在来源疑点 | 可能系统性影响少量除息日 band 与空头信号 | 原始数据不改；需要独立分钟源复核 |
| 2008/2026 为部分年度 | 年度表易被误读 | 所有报告显示精确起止日期 |
| 实盘运营未研究 | 券商限制、订单拒绝、监控和风险控制不在当前模型内 | Live deployment 明确延期 |
| QQQ 尚非正式发布 | 事后选资产、dirty run、单一 vendor 分钟源与 invalid-row drop 会弱化证据等级 | 保持 exploratory 标签；冻结独立 spec 后再 clean rerun |

## 下一步规划

优先级按“最可能改变经济结论”排序：

1. **Market impact 与容量**：基于订单规模、minute/auction volume、参与率、spread、波动率和时段建立冲击曲线，并分别处理 pre/post-2016 volume regime。
2. **真实收盘执行证据**：取得 official close/auction print、MOC 券商成交或 TAQ 级数据；把 auction participation、imbalance、partial fill 与增量成本纳入状态机。
3. **账户类型融资敏感性**：拆分 base cash return、long funding、short-proceeds rebate 和 stock borrow；比较机构、保守机构和 no-rebate/retail 场景。
4. **统计扩展**：在已完成 Sharpe bootstrap/HAC 的基础上，对 gross edge/share、相对 benchmark、事件到达率与条件 payoff 建立事前定义的区间估计；不要只比较点估计。
5. **QQQ 正式化门槛**：先冻结独立 spec、人工裁决 invalid row、取得第二分钟源/更完整发行方分红，再做 clean deterministic rerun；不得按已观察 QQQ 结果调参。
6. **前瞻性路径特征研究**：只使用每个决策点当时可见的信息，预先定义方向效率、路径曲折度或突破持续度；不得直接使用当日 high/low/close 或 VIX 收盘变化。
7. **交易模拟扩展**：订单有效期、复牌后重算信号、queue stress、部分成交、订单拒绝和风险限额。

在上述问题解决前，以下方向继续明确暂缓：**参数优化、Qlib、机器学习和 live deployment**。如果未来进入新一轮信号研究，应使用新的冻结训练/验证设计，不能把当前 post 区间重新包装成 OOS。

## 复现与验证

### 安装与快速测试

```powershell
python -m pip install -r requirements.txt
python prepare_spy_data.py --self-test
python test_engine.py
python test_evaluation_runner.py
python test_attribution_report.py
node test_attribution_report_dom.js
python scripts/check_markdown_links.py
```

`requirements.txt` 解析到精确的 `requirements.lock`；正式数据发布会在读取和发布前核对锁定版本。

### 验证 frozen evaluation 计划

先使用只读、无大 parquet 运算的 plan-only 入口：

```powershell
python evaluation/run_evaluation.py `
  --plan-only `
  --spec config/evaluation_spec_v2.yml `
  --data-release data_release_v1 `
  --benchmark-daily benchmark_release_v1/spy_daily_raw_close.csv `
  --financing-rates data/reference/financing_rates_v1
```

正式矩阵运行要求 clean Git worktree、不可变 data release、完整 benchmark 和 financing release。正式产物不会覆盖已有同 spec run。

### 独立实验

```powershell
# 论文 Q24 replication 自测
python experiments/paper_replication_v1/run.py --self-test

# EOD close-source 探索性实验
python experiments/eod_close_source_v1/run.py

# 统计不确定性单元检查与正式账本对账（需要本地 ignored formal run）
python experiments/statistical_uncertainty_v1/test_uncertainty.py
```

大体积 parquet/CSV 研究中间产物可重建，不作为 GitHub 阅读入口；源码、冻结配置、审计文档和可读 HTML 报告应进入 Git。任何报告中的数字都应能追溯到 run manifest 和 hash。

## 文档与报告导航

完整分类、权威层级、历史材料说明和维护规则见 [docs 文档索引](docs/README.md)。

### 推荐阅读顺序

| 读者问题 | 文档 |
|---|---|
| 想先理解策略每一步如何交易 | [SPY 日内动量策略：论文交易逻辑与实现详解](docs/PAPER_TRADING_LOGIC_AND_IMPLEMENTATION_ZH.md) |
| 想知道项目从起点到现在做了什么 | [项目完整研究与优化记录](docs/PROJECT_WORK_LOG_ZH.md)；术语较多时读[详细解释版](docs/PROJECT_WORK_LOG_ZH_DETAILED.md) |
| 想逐项比较论文、sample 与当前代码 | [完整差异说明](docs/PAPER_SAMPLE_CURRENT_COMPARISON_ZH.md) |
| 想审计数据范围、缺失、halt 和发布契约 | [数据层说明](docs/README_DATA.md)、[冻结验收](docs/DATA_V1_FREEZE_AUDIT_20260730_ZH.md) |
| 想审计成交、PnL、融资与统计 | [引擎说明](docs/README_ENGINE.md) |
| 想看原始 frozen v2 结论 | [正式结果说明](docs/POST_PUBLICATION_EVALUATION_V2_ZH.md)、[HTML 报告](docs/POST_PUBLICATION_EVALUATION_V2.html) |
| 想看主要经济展示与完整归因 | [halt-aware amendment](docs/POST_PUBLICATION_EVALUATION_V2_HALT0025_ZH.md)、[交互式归因报告](docs/POST_PUBLICATION_EVALUATION_V2_HALT0025_ATTRIBUTION.html) |
| 想比较 profile/tier/dividend/slippage 或自选日期 | [交互比较报告](docs/POST_PUBLICATION_EVALUATION_V2_HALT0025_REPORT2.html) |
| 想看论文 Q24 月收益复现 | [Replication README](experiments/paper_replication_v1/README.md)、[交互日期窗口报告](experiments/paper_replication_v1/results/data-v1.0_q24_detailed_report_20260730_v2/report2.html) |
| 想看 EOD/MOC/TWAP 敏感性 | [EOD 实验说明](docs/EOD_CLOSE_SOURCE_EXPERIMENT_V1_ZH.md)、[HTML 报告](docs/EOD_CLOSE_SOURCE_EXPERIMENT_V1_ZH.html) |
| 想判断 pre/post Sharpe 差异是否有统计证据 | [统计不确定性附录](docs/STATISTICAL_UNCERTAINTY_V1_ZH.md) |
| 想看杠杆、1× cap、恒定 2× | [杠杆与定仓敏感性](docs/LEVERAGE_SIZING_SENSITIVITY_ZH.md) |
| 想看 Section 31 对净收益的影响 | [Section 31 敏感性](docs/IBKR_SECTION31_COST_SENSITIVITY_ZH.md) |
| 想看 QQQ 探索性扩展 | [QQQ 数据审计](docs/QQQ_DATA_AUDIT_20260804_ZH.md)、[带股息/融资摘要](experiments/qqq_with_dividends_v1/SUMMARY_ZH.md)、[归因 HTML](experiments/qqq_with_dividends_v1/QQQ_ATTRIBUTION.html) |

### 冻结配置

- [data-v1.0 contract](config/data_release_v1.yml)
- [原始 evaluation spec v2](config/evaluation_spec_v2.yml)
- [halt-aware reporting amendment](config/evaluation_spec_v2_halt_headline.yml)
- [论文原文 PDF](docs/Intraday-momentum.pdf)

HTML 报告包含内嵌数据和 JavaScript；GitHub 通常不会直接执行，请下载后在本地浏览器打开。

## 仓库结构

| 路径 | 作用 |
|---|---|
| `prepare_spy_data.py` | 数据审计、交易日历、component validity、原子发布 |
| `im_engine_v4.py` | 三 profile、特征、信号、订单/成交状态机、PnL、成本与融资 |
| `evaluation/` | 冻结矩阵 runner、正式结果生成与归因报告生成器 |
| `config/` | 数据 release 和 evaluation 的冻结契约 |
| `data/reference/` | State Street 分红、PIT financing rate release 等可追溯参考输入 |
| `data_release_v1/` | 本地不可变 data-v1.0 bundle；大体积、可由冻结流程重建 |
| `benchmark_release_v1/` | 本地独立 SPY daily raw-close benchmark release |
| `experiments/paper_replication_v1/` | 与经济评估隔离的论文 Q24 replication |
| `experiments/eod_close_source_v1/` | 不覆盖 v2 的 EOD close-source 执行敏感性 |
| `experiments/statistical_uncertainty_v1/` | SPY amendment 的 post-result bootstrap/HAC 附录 |
| `experiments/qqq_*` | QQQ 数据、策略、归因与统计探索；非 frozen 正式发布 |
| `docs/` | 审计、研究过程、正式结果和可读 HTML 报告 |
| `.github/` | Windows/Python 3.13 基础 CI 与研究型 PR 模板 |
| `CONTRIBUTING.md` | 变更分类、产物政策、验证和交付规范 |
| `test_engine.py` | 引擎合成回归检查 |
| `test_evaluation_runner.py` | 评估矩阵、完整性与发布检查 |
| `test_attribution_report.py` | 归因恒等式和交互报告逻辑检查 |
| `previous_research/` | 只读历史基线；不再扩展 |

项目的核心产出不是某一个 CAGR，而是一套可追溯的研究链条：**原始输入 → 数据契约 → 特征有效性 → 成交状态 → 会计恒等式 → 冻结评估 → 归因 → 独立敏感性。** 只有在这条链条上定义清楚的结果，才进入经济判断。
