# IBKR / SEC Section 31 历史成本敏感性 v1

这是 **post-result transaction-cost sensitivity**，不修改 frozen v2 或
`corrected_execution × halt_aware × with-dividends × $0.0025/share`
reporting amendment。唯一变化是在原有基础佣金之外，按每笔卖出成交的名义
金额乘以交易日有效的 SEC Section 31 费率。

| 时段 | Legacy Portfolio CAGR | + Section 31 Portfolio CAGR | Portfolio 变化 | Legacy Trading-only CAGR | + Section 31 Trading-only CAGR | Trading-only 变化 | Legacy Sharpe | + Section 31 Sharpe | Section 31 总额 | 每成交股 Section 31 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Full sample | 16.70% | 15.57% | −1.129pp | 15.25% | 14.13% | −1.115pp | 1.075 | 1.006 | $78,275.65 | 0.2370¢ |
| Pre-publication | 18.00% | 16.84% | −1.163pp | 16.97% | 15.82% | −1.153pp | 1.184 | 1.113 | $52,459.45 | 0.1944¢ |
| Post-publication | 7.52% | 6.62% | −0.896pp | 3.27% | 2.41% | −0.861pp | 0.300 | 0.243 | $25,816.19 | 0.4271¢ |

Post-publication 加入 Section 31 后，portfolio CAGR 从 7.52% 变为 6.62%，
same-path trading-only CAGR 从 3.27% 变为 2.41%，Sharpe 从 0.300 变为
0.243。SPY 名义价格随时间上升，所以按总成交股平均的
Section 31 成本从 pre 的 0.1944¢ 上升到 post 的 0.4271¢；这正是固定每股
监管费无法表达的时间结构。

## 计费语义

- 多头平仓卖出：计费；
- 卖空开仓：计费；
- long → short：按卖出的 `2 × shares` 计费；
- short cover 买入：不计费；
- 费率使用交易日有效区间，不使用公告日，也不把当前费率倒推历史；
- 2025-05-14 至 2026-04-03 的费率为零。

## 审计与 provenance

- run：`20260801T164756Z_ibkr_section31_v1`
- 分类：`post_result_transaction_cost_sensitivity`
- 数据：2008-01-22 至 2026-07-09
- Section 31 schedule SHA-256：
  `75fa80a576dfeb349e3e49c057981f0cd97018e575900a35b5f5d9ee2b4bcd81`
- data release manifest SHA-256：
  `fc63122dff1d13df95735ba2e1df0a9763db100e0c3efbfa99adf948d602c530`
- financing SHA-256：
  `3f4addb685cbbdf5f887aec7837825b3fbefff13d4fdfd2192673b5458698be8`
- legacy 路径对既有 formal run 最大逐日数值差：`0`
- 最大 accounting residual：`1.16e-10`
- 两条路径均有 8,198 fills，其中 4,098 个含卖出数量；买入端没有被误收费
- 零费率窗口最大 Section 31 fee：`0`
- 运行时 Git dirty：`true`；这是有意的开发验证，不冒充 clean formal run

## 尚未建模

本实验只量化 Section 31，不能称为 all-in IBKR 模型。以下项目保持显式
“未建模”，没有被静默塞进 slippage：

- FINRA TAF、CAT、clearing 和 pass-through 的完整历史；
- venue / maker-taker / auction fee 与 rebate；
- IBKR 客户账单的聚合、舍入和法律实体差异；
- 月度累计成交量的 Tiered 档位；
- market impact、queue position 和 partial fills。

完整研究判断、官方来源和实施边界见
`docs/IBKR_US_Stock_Trading_Costs_Summary_ZH.md`。可交互/浏览版本见
`docs/IBKR_SECTION31_COST_SENSITIVITY_ZH.html`。
