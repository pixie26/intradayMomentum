# QQQ 厂商分钟价格 raw-vs-adjusted 第三方交叉验证

日期:2026-08-05。目的:核实数据审计第十一节"厂商价格疑似已分红调整"的发现。

## 背景

数据管道 `raw_price_evidence` 对 QQQ 给出 `evidence_supports_raw_prices = false`,依据是除权日开盘隔夜跳空(raw_ex=0.0066%)接近非除权日基线(0.042%),而把分红加回去后(0.1985%)反而偏离基线更远--提示除息下跌已被抹平(已调整)。该函数注记明示"evidence, not proof"。

## 第三方源

Yahoo Finance QQQ 日线(`query1.finance.yahoo.com/v8/finance/chart/QQQ`),2007-01-03..2026-08-04,4,927 行。Yahoo 同时提供:
- `raw_close`:未调整收盘(分红/拆股前的原始价);
- `adj_close`:向后调整收盘(历史价按累计分红向下调整,近期价=raw)。

## 测试 A:价位漂移(决定性)

逐年计算 `vendor_close − yahoo_raw_close` 与 `vendor_close − yahoo_adj_close` 的均值(美元):

| year | dr_raw | dr_adj |
|---|---:|---:|
| 2007 | -0.001 | 7.025 |
| 2010 | 0.003 | 6.210 |
| 2015 | 0.005 | 8.423 |
| 2020 | 0.013 | 8.682 |
| 2023 | -0.020 | 6.011 |
| 2026 | 0.055 | 0.907 |

- `dr_raw` 全样本年均绝对值 **0.013 美元**(QQQ 价 ~$30-685,即 <0.01%),几乎为零,差异来自收盘时点(厂商=最后一根 1-min RTH bar 即 15:59 收盘,Yahoo=官方 16:00 收盘交叉)。
- `dr_adj` 全样本年均绝对值 **6.82 美元**,随累计分红增大(2007 ~$7、2015 ~$8.4)并向近期收敛(2026 ~$0.9)--正是"向后调整"模式。

**厂商价 = Yahoo raw,不是 Yahoo adjusted。**

## 测试 B:逐除权日开盘跳空

对全部 81 个 ex-date,比较厂商开盘跳空 `open[ex]/close[ex-1]-1` 与 Yahoo raw 开盘跳空:

- 相关 **0.9958**;平均差 **0.0038%**;逐日一致(差异在收盘时点噪声内)。

厂商开盘跳空与 Yahoo raw 逐日同步,二者同为未调整价。

## 测试 C:审计指标对 Yahoo raw 自身也判 False(误报证据)

将管道的 `raw_price_evidence` 同一指标施于 Yahoo raw 与 Yahoo adjusted(2023-2026,15 个 ex-date):

| 序列 | baseline | ex_raw_gap | ex_div_added | supports_raw |
|---|---:|---:|---:|---|
| vendor(81 ex) | 0.042% | 0.0066% | 0.1985% | False |
| yahoo_raw(15 ex) | 0.0685% | 0.2058% | 0.3405% | **False** |
| yahoo_adj(15 ex) | 1.0473% | 1.2533% | 1.3895% | False |

**Yahoo 自己的 raw 未调整价也判 `supports_raw=False`**。原因:QQQ 季度分红率仅 ~0.13%,远小于隔夜开盘跳空波动;用"距基线距离"比较的测试在弱信号下失效,无法区分 raw 与 adjusted。

## 结论

**厂商 QQQ 分钟价格是 raw 未调整价**,由 Yahoo 第三方日线双重确认(价位逐年至美元级吻合 + 81 个除权日开盘跳空 0.996 相关)。管道 `evidence_supports_raw_prices=false` 是**已知弱测试在低分红率 ETF 上的误报**,不构成调整证据。

**对带息 QQQ run 的影响**:无二次调整问题。引擎 `prev_close_adj = prev_close − dividend` 正确调整 raw 前收,基准全收益 `(close + div)/prev_close` 正确计息。第十一节的保留应据此修正。

## 产物

- `experiments/qqq_with_dividends_v1/crosscheck_raw_vs_adjusted/yearly_drift.csv`
- `experiments/qqq_with_dividends_v1/crosscheck_raw_vs_adjusted/exdate_open_gaps.csv`
