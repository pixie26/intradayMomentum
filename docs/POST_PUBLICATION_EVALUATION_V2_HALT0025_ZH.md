# Post-publication evaluation v2：halt-aware × $0.0025 headline amendment

本文件记录原始 frozen v2 之后的 **post-result reporting-headline amendment**。
原始 `paper_ready × $0.005/share` 正式结果及其报告保持不变；本 amendment
把已有 72-cell 矩阵中的
`corrected_execution × halt_aware × with_dividends × $0.0025/share`
指定为主要经济展示口径，不把它描述成事前预注册选择。

## 正式运行与 provenance

- run：`20260731T200227Z_formal_spec2_58205b0c130f`
- evaluation commit：`050b0314b6d8454e7ce08cdfd9f9d9e8d32b7cc3`
- Git dirty：`false`
- spec：`config/evaluation_spec_v2_halt_headline.yml`
- spec SHA-256：`58205b0c130f64b8c9eee3ae3278a54137deb726e1631b2e27a894ca4dc8e54f`
- run manifest SHA-256：`624d4a9956fdaccd8f1e276888a96e6d06b33bd6c86e3cdb772694dcd176d05b`
- data release manifest SHA-256：`fc63122dff1d13df95735ba2e1df0a9763db100e0c3efbfa99adf948d602c530`
- benchmark SHA-256：`5dc28af7090d155da99939928170bfc2bf799d853e11c212d40f6564b836bc28`
- financing curve SHA-256：`3f4addb685cbbdf5f887aec7837825b3fbefff13d4fdfd2192673b5458698be8`

72/72 cells 和 216/216 summary rows 均完成。与此前
`20260731T100457Z_formal_spec2_10a9f44dfac6` 按
`profile/tier/dividend/slippage/subperiod` 对齐后，所有数值列最大差异为
**0.0**；文件 hash 的变化来自 tier 输出顺序及新的 provenance/报告文字。

## Headline performance 与 cash-interest 口径修正

正式 portfolio CAGR 与 same-path trading-only CAGR 必须并列。后者逐日使用
`portfolio return − cash-interest return` 后重新复利；它保留实际 AUM、仓位和
融资路径，是分析口径，不是另一条自融资回测。

| 窗口 | Portfolio CAGR（含现金） | Trading-only CAGR | Cash interest 年化 | Cash 占简单加总收益 | SPY TR CAGR | Sharpe vs cash | MDD |
|---|---:|---:|---:|---:|---:|---:|---:|
| Full sample | 16.70% | 15.25% | 1.26% | 7.6% | 11.96% | 1.07 | −28.26% |
| Pre-publication | 18.00% | 16.97% | 0.88% | 5.0% | 10.70% | 1.18 | −28.26% |
| Post-publication | **7.52%** | **3.27%** | **4.06%** | **48.6%** | 21.74% | 0.30 | −17.06% |

`Cash interest 年化 = mean(daily cash-interest return) × 252`；
`Cash 占简单加总收益 = Σ cash-interest return / Σ portfolio return`。
Post 的 portfolio CAGR 有约 4.25pp 来自 cash-carry uplift；2024–2026 的高利率
现金收益不是策略 alpha。因此不得再把 7.52% 单独作为策略 headline 解读。

核心结论相应收紧：post 毛交易边际仍为正，但 trading-only CAGR 只有 3.27%，
Sharpe vs cash 只有 0.30，并大幅跑输同期 SPY total return。

## Post-publication P&L 来源

下表的 return contribution 使用逐日复利链接：每个组件按其后的策略财富增长
链接，所有组件精确加总到 post 累计收益 17.19%。它不是把美元 P&L 简单除以
初始 AUM。

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

主要发现：

- post 毛交易收益主要来自 short，而不是 long；
- 逐日剔除 cash-interest return 后，same-path trading-only 累计收益为 +7.29%，
  CAGR 为 3.27%；linked components 中 cash 以外的贡献为 +8.35pp，两者因
  复利链接定义不同而不应混用；
- execution 合计约拖累 −2.49pp，funding + borrow 约拖累 −0.57pp；
- borrow 25bp 本身不是 post 表现弱的主要原因；
- with-dividends 是信号锚点定义，不是日内持仓收到的 dividend cash P&L。

## 资本使用、交易切片与利润集中度

| 指标 | Pre | Post |
|---|---:|---:|
| 全交易时段平均 gross exposure / AUM | 65.2% | 68.7% |
| 有持仓分钟 / scheduled minutes | 25.2% | 25.4% |
| 有持仓时平均 gross leverage | 2.57× | 2.71× |
| Active-day rate | 60.6% | 60.2% |
| 平均正现金余额 / AUM | 114.1% | 116.9% |
| 平均借款余额 / AUM | 22.6% | 24.2% |

正现金余额可因 short proceeds 超过 100%，不能直接解释为“闲置资本比例”。
实际资本投入强度使用引擎的 notional-minute ledger 计算。

Post 的 488 个 round trips 中 long 261、short 227。long gross P&L 为
+$27,526，short 为 +$124,629。按冻结 entry bucket：

| Entry bucket | Entries | Gross P&L | 平均 gross / entry |
|---|---:|---:|---:|
| Open，分钟 1–120 | 227 | +$254,317 | +$1,120 |
| Midday，121–270 | 163 | −$102,720 | −$630 |
| Close，271+ | 98 | +$558 | +$6 |

这说明 post 的毛利润集中在开盘 bucket；midday 明显为负，close 几乎为零。
该表未把 commission、slippage、funding 或 cash interest 分摊到单个 round trip。

冻结 volatility regime 使用 lagged 14-session daily volatility，并只用
2024-04-30 以前样本确定五分位边界。Post gross P&L 从 Q1 到 Q5 分别为
+$213,137、+$146,994、−$110,321、−$175,486、+$77,831；对应 entries 为
77、118、159、103、31。中高但非极高波动的 Q3/Q4 是主要亏损区间；Q5 样本较少。

Post 每年 headline signal observations / non-flat observations / entries 为：

| 年份 | Signal observations | Non-flat observations | Entries |
|---|---:|---:|---:|
| 2024-05-01 起 | 2,179 | 586 | 142 |
| 2025 | 3,232 | 862 | 216 |
| 2026 至 07-09 | 1,677 | 520 | 130 |

Signal observations 包含 flat 状态，entries 才是 round trips，不能混称。

Post trading-only 利润明显集中：最佳 1 / 5 / 10 / 20 日分别占所有盈利日
trading P&L 的 7.5% / 22.3% / 33.5% / 47.0%。完整 post trading-only 累计收益
为 +7.29%；仅把最佳 1 日的 trading-only return 置零，累计收益即变为 −2.04%。
这不是可交易反事实，但说明 3.27% trading-only CAGR 对少数极端盈利日较敏感。

## 与 SPY total return 的相关性

Post-publication 点估计：

| 口径 | 相关性 |
|---|---:|
| Daily Pearson | −0.099 |
| Active-day daily | −0.107 |
| Weekly | −0.040 |
| Monthly | −0.418 |
| Quarterly | −0.219 |
| Long contribution vs SPY daily | +0.300 |
| Short contribution vs SPY daily | −0.279 |

全样本 daily / monthly / quarterly correlation 分别约为
−0.081 / −0.248 / −0.343。主 benchmark 使用独立 SPY raw close 加 State Street
现金分红；SPX price index 不含分红，不能替代正式 benchmark。

## SPY 最差的 10 个完整季度

季度先按 SPY total return 机械排序，事件名称随后作为描述性标签添加，不代表
单一因果判断。

| Quarter | Event | SPY total return | Strategy |
|---|---|---:|---:|
| 2008Q4 | Global financial crisis | −21.6% | +15.9% |
| 2020Q1 | COVID-19 shock | −19.4% | +6.0% |
| 2022Q2 | Inflation and rate hikes | −16.1% | +12.2% |
| 2011Q3 | US debt ceiling / euro-area crisis | −13.8% | +4.6% |
| 2018Q4 | Fed tightening / trade tensions | −13.5% | +29.3% |
| 2010Q2 | European sovereign-debt crisis | −11.4% | +0.9% |
| 2009Q1 | Financial-crisis bottom | −11.2% | +8.8% |
| 2008Q3 | Lehman collapse / financial crisis | −8.9% | +19.2% |
| 2015Q3 | China slowdown selloff | −6.4% | +7.9% |
| 2022Q3 | Continued rate hikes | −4.9% | +5.7% |

2008Q1 因策略 warm-up 和数据起点不是完整季度；2026Q3 截至 2026-07-09，
两者都不进入最差/最好季度排名。

## 报告

- `POST_PUBLICATION_EVALUATION_V2_HALT0025_REPORT1.html`：正式摘要及完整 216-row matrix；
- `POST_PUBLICATION_EVALUATION_V2_HALT0025_REPORT2.html`：跨 profile/tier/dividend/slippage 与自选日期的交互比较；
- `POST_PUBLICATION_EVALUATION_V2_HALT0025_ATTRIBUTION.html`：完整交互式 P&L 归因、
  benchmark 相关性、rolling correlation、季度情景和年度归因。

本次扩展归因派生目录：
`evaluation/results/20260731T200227Z_formal_spec2_58205b0c130f_attribution_v2/`。
其中 daily/quarterly/annual、round-trip、decomposition CSV、report、manifest 和
`_SUCCESS` 均已生成；旧 attribution 派生目录保留不覆盖。
该目录与正式 run 分离，不修改正式 run 的 hash 清单。

本轮仍只报告点估计。HAC 和 block bootstrap 按决定继续暂缓。
