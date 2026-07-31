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

## Headline performance

| 窗口 | 策略 CAGR | SPY total-return CAGR | Excess CAGR | Sharpe vs cash | MDD |
|---|---:|---:|---:|---:|---:|
| Full sample | 16.70% | 11.96% | +4.74pp | 1.07 | −28.26% |
| Pre-publication | 18.00% | 10.70% | +7.30pp | 1.18 | −28.26% |
| Post-publication | 7.52% | 21.74% | −14.22pp | 0.30 | −17.06% |

因此 headline 变化没有改变核心结论：post-publication 毛交易边际仍为正，
但风险调整收益较弱，并大幅跑输同期 SPY total return。

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
- 剔除 cash-interest 组件后，同一已实现仓位路径的 linked trading contribution
  约为 +8.35pp；这不是重新回测的自融资组合；
- execution 合计约拖累 −2.49pp，funding + borrow 约拖累 −0.57pp；
- borrow 25bp 本身不是 post 表现弱的主要原因；
- with-dividends 是信号锚点定义，不是日内持仓收到的 dividend cash P&L。

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

归因派生目录：
`evaluation/results/20260731T200227Z_formal_spec2_58205b0c130f_attribution/`。
其中 daily/quarterly/annual CSV、report、manifest 和 `_SUCCESS` 均已生成；
该目录与正式 run 分离，不修改正式 run 的 hash 清单。

本轮仍只报告点估计。HAC 和 block bootstrap 按决定继续暂缓。
