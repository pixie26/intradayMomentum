# 杠杆与仓位规则敏感性实验汇总

日期：2026-08-02  
状态：**post-result exploratory sensitivity；不修改 frozen v2，不构成新的预注册 headline**

交互摘要：[`LEVERAGE_SIZING_SENSITIVITY_ZH.html`](LEVERAGE_SIZING_SENSITIVITY_ZH.html)

## 1. 为什么做这三组实验

围绕当前主要经济展示口径的杠杆规则，先后产生了三个不同问题：

1. 最初把“取消最大 4 倍杠杆”理解为取消上限，因而运行了**无上限**压力测试；
2. 用户随后明确澄清为“不使用杠杆”，因而运行了**1x 上限**测试；
3. 最后提出更有研究价值的问题：长期**恒定 2x**，是否优于 Paper 的
   `min(2% / lagged 14-session volatility, 4x)` 动态定仓。

第一组不是对澄清后问题的回答，不能伪装成原始意图；但它提供了杠杆尾部和
4x cap 风险控制价值的独立证据，因此保留为明确标注的压力测试。

## 2. 三组实验共同固定的口径

除仓位规则外，全部固定为：

- profile：`corrected_execution`；
- tier：`halt_aware`；
- dividends：`with_dividends`；
- slippage：`$0.0025/share`；
- commission、next-executable-open、halt pending-order、EOD flatten 不变；
- 使用 frozen point-in-time cash / funding / borrow rate curve；
- 连续 AUM 路径从 2008-01-22 跑到 2026-07-09；
- post-publication 从 2024-05-01 开始；
- session tier 在 full-calendar features 之后应用；
- portfolio CAGR 与 same-path trading-only CAGR 并列，后者逐日扣除 cash-interest return。

对照基准始终为当前主要经济展示：

```text
corrected_execution × halt_aware × with-dividends × $0.0025/share
仓位 = min(2% / lagged 14-session daily volatility, 4x)
```

这不是对 `config/evaluation_spec_v2_halt_headline.yml` 的修改。Frozen v2 和原始
`paper_ready × $0.005/share` headline 均保持不变。

## 3. 实验登记表

| 实验 | 仓位规则 | 角色 | 正式使用的 run |
|---|---|---|---|
| A. 取消 4x 上限 | `2% / dvol`，无 cap | 需求理解偏差后保留的机械压力测试 | `20260801T160549Z_no_leverage_cap_v1` |
| B. 不使用杠杆 | `min(2% / dvol, 1x)` | 1x opening-price sizing sensitivity | `20260801T161359Z_no_leverage_v1` |
| C. 恒定 2x | `2 × AUM / session open` | 与 Paper 动态定仓的主要比较；强制相同 dvol eligibility | `20260801T162713Z_constant_2x_vs_vol_target_v1` |

实验 C 另报一个不要求 dvol 的 operational 恒定 2x 版本。它比 Paper 多交易
7 个 2019 年 session；主要结论使用**相同 dvol eligibility**的恒定 2x，持仓日
presence mismatch 为 0。

## 4. 全样本结果

| 仓位规则 | Portfolio CAGR | Trading-only CAGR | 年化波动 | Sharpe | MDD | 最差单日 | 有持仓时加权 gross leverage |
|---|---:|---:|---:|---:|---:|---:|---:|
| Paper 动态、4x cap | 16.70% | 15.25% | 14.27% | 1.07 | −28.26% | −5.19% | 2.62x |
| 1x 上限 | 8.66% | 7.35% | 6.69% | 1.10 | −9.36% | −3.46% | 0.99x |
| 恒定 2x、同 eligibility | 17.22% | 15.77% | 14.98% | 1.06 | −18.94% | −9.60% | 2.00x |
| 无上限 | 16.91% | 15.46% | 15.56% | 1.01 | −29.71% | −7.87% | 2.77x |

只看 full sample：

- 1x 显著降低风险和回报，Sharpe 略高但经济收益约减半；
- 恒定 2x 的 CAGR 比 Paper 高 0.52pp，MDD 更浅，但 Sharpe 没有改善，且最差单日
  明显更坏；
- 无上限只增加 0.22pp CAGR，却降低 Sharpe、扩大 MDD 和单日尾部损失。

因此 full-sample CAGR 本身不足以决定仓位规则。

## 5. Post-publication 结果

| 仓位规则 | Portfolio CAGR | Trading-only CAGR | 年化波动 | Sharpe vs cash | MDD | 最差单日 | 有持仓时加权 gross leverage |
|---|---:|---:|---:|---:|---:|---:|---:|
| Paper 动态、4x cap | 7.52% | 3.27% | 14.73% | 0.30 | −17.06% | −3.10% | 2.71x |
| 1x 上限 | 4.13% | 0.15% | 5.80% | 0.05 | −5.64% | −1.41% | 0.99x |
| 恒定 2x、同 eligibility | 4.54% | 0.47% | 11.95% | 0.10 | −14.39% | −2.82% | 2.00x |
| 无上限 | 8.24% | 3.95% | 15.99% | 0.33 | −17.95% | −3.73% | 2.83x |

Post 的核心不是 portfolio CAGR，而是 trading-only：

- 1x 的 4.13% portfolio CAGR 中，交易路径 CAGR 只有 0.15%；
- 恒定 2x 的 4.54% 中，交易路径 CAGR 只有 0.47%；
- Paper 为 3.27%；
- 无上限为 3.95%，但风险和杠杆尾部更差。

高利率现金 carry 不是策略 alpha。不能用 4.13%、4.54%、7.52% 或 8.24%
单独代表信号能力。

## 6. Finding A：4x cap 有实际风险控制价值

无上限压力测试中：

- full-sample 目标杠杆 P95 / P99 / 最大值为 5.44x / 7.19x / 13.27x；
- 实际有持仓时 gross leverage P95 / 最大值为 5.48x / 13.20x；
- 16.5% 的合格 session 目标杠杆超过 4x；有持仓日中为 17.6%；
- post 对应比例为 13.3% / 14.2%；
- full Sharpe 从 1.07 降至 1.01，MDD 从 −28.26% 扩到 −29.71%；
- 年度效果不稳定：2018 年相对 4x cap 改善 +5.68pp，2014 年恶化 −6.29pp。

结论：4x cap 不是无关紧要的装饰。取消它只产生很小的平均收益改善，却引入
13x 级机械目标杠杆。该路径还没有 margin、forced liquidation、participation 或
market-impact 模型，现实可交易结果只会更差，不能据此取消 cap。

## 7. Finding B：1x 是有效的风险压缩，但 post 交易边际几乎消失

1x cap 在 full sample 的 93.61% 合格 session 生效；post 为 96.90%。它把：

- full 年化波动从 14.27% 降到 6.69%；
- full MDD 从 −28.26% 降到 −9.36%；
- post MDD 从 −17.06% 降到 −5.64%；
- post trading-only CAGR 从 3.27% 降到 0.15%。

因此 1x 是清晰的资本保护选择，但不是保留 Paper 经济收益的等价实现。

实现边界：当前 1x 是按 session open 计算股数；`corrected_execution` 稍后在
next-open 成交，价格移动会使实际 held gross leverage 短暂达到 1.04x。
Full-sample 全时段加权临时负现金约为 AUM 的 0.08%。若要求每一分钟严格零借款，
需要新增 execution-aware dynamic cash constraint；当前实验没有假装实现这一点。

## 8. Finding C：恒定 2x 的 full-sample 优势由 2008 驱动

恒定 2x 的 full CAGR 为 17.22%，表面上高于 Paper 的 16.70%。但完整的逐年移动
起点审计显示：

- 17 个起点中，恒定 2x 只在 1 个起点胜出；
- 这个唯一胜出的起点包含 2008 危机期；
- 从 2009 开始，Paper / 恒定 2x CAGR 为 14.64% / 12.40%；
- 从 2010 至 2024 的每一个后续起点也都是 Paper 胜出；
- 2008 年恒定 2x 相对 Paper 改善 +83.81pp；2024 年恶化 −20.27pp；
- 恒定 2x 最差日是 2020-03-18 的 −9.60%，Paper 当日只有 −1.52%。

这解释了为什么恒定 2x 可以同时拥有较浅的 full MDD 和更坏的单日尾部：它在
2008 的若干高波动趋势日赚得很多，但在 2020-03-18 这类高波动失败日不会自动降仓。

按 Paper 当日 ex-ante sizing regime 分解 post relative wealth（各项为乘法贡献，
不能直接相加）：

| Paper 当日目标杠杆 | 恒定 2x 相对 Paper 的 post relative wealth |
|---|---:|
| `<2x` | +4.20% |
| `2x–<4x` | −4.56% |
| `4x cap` | −5.44% |

Paper 的规则不是“偶尔随意上 4x”，而是 inverse-vol sizing：高滞后波动时降到
2x 以下，低滞后波动时才提高到 2–4x。当前证据支持这个动态结构优于恒定 2x，
尤其在 post 和移动起点稳定性上；但它仍然只是 point estimate。

## 9. 综合判断

1. **保留 frozen Paper 动态定仓和 4x cap。** 三组实验没有提供足够证据修改正式口径。
2. **不要取消 4x cap。** 无上限的平均收益改善太小，尾部杠杆和现实执行缺口太大。
3. **1x 适合风险预算，不代表信号更好。** 它显著压缩风险，但 post trading-only
   近乎为零。
4. **不采用恒定 2x 取代动态规则。** Full-sample 的微弱优势不具起点稳定性，并由
   2008 主导；post 明显落后。
5. **不要把任何一组作为新 OOS 选择。** 三组结果都在看过正式结果后产生，只能称为
   post-result sizing sensitivity。
6. **更大的未决问题仍是执行可实现性。** Market impact、capacity、queue position、
   partial fill 和严格 margin/cash constraint 比继续微调杠杆更优先。

## 10. Provenance 与审计

| Run | Git 状态 | Config SHA-256 | Runner SHA-256 | 4x baseline vs formal |
|---|---|---|---|---:|
| `20260801T160549Z_no_leverage_cap_v1` | `c859248`，dirty=false | `1714c0b227f8cca63bd9ffbe0c3c6ba00a1e3d882037f8a7a5faaf8a1b159cbd` | `19ab97a2000aac747cc7abd510fe2ab912f2322eea6283f4fc9ea9855012fbd4` | 0.0 |
| `20260801T161359Z_no_leverage_v1` | `c859248`，dirty=true | `1e91efbf326fe8f4ac918bbfee6ded9914cfe42fa96ddb12e9238a253fbb0fd7` | `ccc42e97cfc234006453f7b5b724aca9ffa5dc4c0767e1e6c8a64b865efcbbef` | 0.0 |
| `20260801T162713Z_constant_2x_vs_vol_target_v1` | `c859248`，dirty=true | `7ef8684e47e6ac5c90bbc09a6b478867287735200726771c5eabf4b334fb5b64` | `c25aaba3336a27d7c13afbaef03279a7eb6efc5950cd005347c470aea1552fc4` | 0.0 |

三次运行的 engine SHA-256 均为
`c2b6062e5da0471fde245d3cae8f62dee434667a6a34eb0a876ebd2bc0875e3f`。
Dirty run 的具体源文件和配置保存在对应实验目录；结果 manifest、`_SUCCESS`、
逐日 parquet 和 CSV 保存在本地 ignored results 目录。三条 4x baseline 均与 clean
72-cell run `20260731T200227Z_formal_spec2_58205b0c130f` 数值完全一致。

会计恒等式均通过：`net = gross − costs + cash interest + financing`，并满足
`AUM_t = AUM_{t-1} + net P&L`。恒定 2x 主要对照与 Paper 的 evaluation mask 差异为
0、持仓 presence mismatch 为 0。

## 11. 文件索引

实验 A：

- `experiments/no_leverage_cap_v1/config.yml`
- `experiments/no_leverage_cap_v1/run.py`
- 本地结果：`experiments/no_leverage_cap_v1/results/20260801T160549Z_no_leverage_cap_v1/`

实验 B：

- `experiments/no_leverage_v1/config.yml`
- `experiments/no_leverage_v1/run.py`
- 本地结果：`experiments/no_leverage_v1/results/20260801T161359Z_no_leverage_v1/`

实验 C：

- `experiments/constant_2x_vs_vol_target_v1/config.yml`
- `experiments/constant_2x_vs_vol_target_v1/run.py`
- 本地结果：`experiments/constant_2x_vs_vol_target_v1/results/20260801T162713Z_constant_2x_vs_vol_target_v1/`

## 12. 尚未运行与下一步

未运行：

- HAC / block bootstrap；
- broker-specific margin、forced liquidation 和每分钟严格零负现金；
- market impact / participation / queue / partial fill；
- 预注册的未来样本验证；
- 风险匹配后的固定杠杆比较或 1.5x / 2.5x 网格。

若继续研究，应先冻结新的 sizing sensitivity spec，再用未来数据或严格的滚动验证
检验；不应根据本文的已观察结果选择新参数。
