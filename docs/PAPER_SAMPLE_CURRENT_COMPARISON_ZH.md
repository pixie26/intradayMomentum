# 论文、作者 Sample Code 与当前实现的完整差异说明

**版本日期：2026-07-29**

本文说明三个不同研究对象：

1. **论文**：策略经济逻辑和论文文字口径；
2. **作者 sample code / Colab**：作者公开的简化实现；
3. **当前项目**：数据层 v5 candidate + `im_engine_v4.py` 的三profile研究框架。

不能把“论文”和“sample code”视为同一件事。当前项目专门保留三个profile，就是为了把差异可归因地拆开。

---

## 1. 三个当前 profile 的定位

| Profile | 目的 | 是否代表真实可交易结果 |
|---|---|---|
| `official_sample_compatible` | 复现作者notebook主要conventions | 否，仅用于归因/parity |
| `paper_spec` | 按论文文字实现信号和成本 | 仍偏乐观，主要用于论文复现 |
| `corrected_execution` | 保留论文信号，修正成交、halt和数据有效性 | 当前最接近经济结果，但仍未含完整融资、借券和impact |

---

## 2. 逐项差异矩阵

| 项目 | 论文口径 | 作者 sample code | 当前优化实现 | 为什么优化 |
|---|---|---|---|---|
| 同分钟噪声窗口 | 前14个交易日 | `min_periods=13`，存在行rolling | paper/corrected严格14个eligible session；sample profile保留13 | 避免提前warm-up和缺行后向前多取旧数据 |
| 日波动率窗口 | 最近已完成的14日 | `d-15..d-2`，漏最近一天 | paper/corrected严格窗口；sample profile保留原偏差 | 修复off-by-one和杠杆错配 |
| 波动率缺失 | 原则上不可可靠定仓 | 最大4倍杠杆 | paper/corrected跳过；sample profile保留4x fallback | 缺少风险估计时不应承担最大风险 |
| Previous close | 前一交易日，经除息调整 | 有简化分红调整 | 从完整calendar和`close_valid`生成；自动加载clean dividend | 防止删除坏日后取到两天前收盘 |
| 分红 | 影响band锚点和benchmark total return | 策略有处理，benchmark常遗漏 | paper/corrected缺分红默认拒绝；可显式ignore并记录 | 保证论文复现和真实超额收益口径 |
| 半日市 | 只存在交易所安排分钟 | 容易依赖现有rows | scheduled grid按calendar bars生成 | 半日市下午不是“缺失” |
| 整日缺失 | 应占calendar和rolling槽位 | 通常直接不存在 | daily母表来自`vsess`完整calendar | 防止窗口向前多取更老session |
| Interior gap | gap后累计VWAP不再可信 | 通常静默继续 | `vwap_valid`在gap后失效；`move_open_obs_valid`独立判断 | 不把不同依赖结构压成单一flag |
| Truncated open | 当日开盘锚点失效 | 第一根可得bar可能被当open | `open_valid=False`，整日`move_open_obs_valid=False`，close仍可独立有效 | 避免错误open污染当天及后14日特征 |
| Trailing truncation | 缺真实退出/收盘 | 最后一根可得bar可能当收盘 | `close_valid=False`；持仓暴露到未知尾部则`unknown_exit` | 不制造虚假EOD退出和benchmark close |
| Halt minute | 不可交易，已有仓位承担复牌gap | 无完整状态机 | minute-set验证；halt bar不进VWAP/mark；已有仓位计gap，未成交订单不计 | 区分“持仓风险”和“未成交订单” |
| 决策频率 | 每30分钟 | 依赖分钟编号 | `config_validity()`按当前Cfg重建15/30/60等网格 | 防止参数sweep仍使用默认30分钟mask |
| Signal validity | 取决于当前参数 | 隐含在代码流程 | parameter-free primitives + config-specific mask | 避免把trade_freq/window/VWAP烧进数据层 |
| Signal时点 | bar结束确认 | shifted exposure近似 | 三profile显式区分signal与fill | 防止同bar未来函数和模糊成交语义 |
| 成交价格 | 论文文字存在乐观解释空间 | shifted close / signal-bar-close | paper保留；corrected使用next executable open | 新仓位不应获得signal close到next open的不可成交变化 |
| Pending order | 未明确 | 没有真正状态机 | cancel或queue到第一个executable bar | halt/缺分钟时不能按“下一可得行”自动成交 |
| 最后一根bar | 不应新开后立即平仓 | row shift自然避免部分情况 | 明确禁止final scheduled bar新开仓 | 避免零PnL双成本round trip |
| `exec_lag>1` | 未重点讨论 | 不支持或隐含 | `< due / == due / > due`显式处理 | 防止延迟订单提前成交 |
| 股数舍入 | 论文实现可用floor | sample使用round | profile分别设置，并有Cfg字段使用审计 | 保持归因真实性，防止配置声明但未读取 |
| 反手 | 两倍成交量 | `abs(position change)`概念 | 3 fill events / 4 trade units；支持单2N订单或两张N订单 | 修复成本和turnover少算 |
| Commission | USD 0.0035/share，最低佣金 | 有 | 按实际订单quantity；最低佣金只作用commission | 正确处理反手和小订单 |
| Slippage | 论文包含约USD 0.001/share | sample通常未扣 | paper=0.001；corrected默认0.005；独立于commission | 解释sample→paper→corrected收益下降 |
| Market impact | 未完整建模 | 无 | 尚未实现 | 大规模AUM和2.5x杠杆下固定滑点仍偏乐观 |
| 现金/融资/借券 | 经济上应存在 | 基本无 | 已有rate接口和基础账户，但time-integral仍需完善 | 多头融资与空头borrow不能因signed notional抵消 |
| PnL marking | 简化描述 | close-to-close shift | 归属于成交前持仓；成交后新仓位开始mark | 正确处理复牌gap和fill前价格变化 |
| Unknown exit | 未系统处理 | 最后可得bar | terminate / freeze / impute三policy；默认terminate | 不让猜测PnL进入后续AUM复利 |
| Session filtering | 通常基于可用数据 | 直接在现有数据运行 | 先全calendar计算特征，最后应用tier mask | 防止先过滤后改变previous close和rolling定义 |
| VWAP | 当日累计VWAP | sample固定实现 | `hlc3 / ohlc4 / vendor_bar_vwap`显式选择 | 数据商bar VWAP定义不一定一致 |
| Benchmark | 应比较SPY total return | 常为price return | 同引擎price/total；invalid close为NaN；首日前anchor | 防止高估excess和错位首日收益 |
| Sharpe | 应在统一时间轴年化 | active日筛选可能抬高 | calendar Sharpe为主；active只报未年化conditional moments | 删除纯年化因子制造的高Sharpe |
| CAGR | 应按真实时间 | 用保留收益行数量年化 | 按首末日期/365.2425 | 不因删除坏日压缩时间轴 |
| MDD | 应来自同一权益曲线 | EOD为主 | 每tier独立完整指标；不跨tier拼接 | 避免无定义的混合Calmar |
| Alpha/Beta | 应使用对齐total return和稳健误差 | 简单OLS | 当前对齐benchmark；正式版仍建议HAC和独立daily benchmark | 提升统计可信度 |
| 数据审计 | 论文非重点 | sample依赖输入 | hash、manifest、reports、`_SUCCESS`、latest pointer | 保证可复现和可追溯 |
| 测试 | 无完整公开测试矩阵 | 教学代码 | 项目方最新报告57 engine + 28 data tests | 防止静默patch失败和配置未使用 |

---

## 3. 当前实现相对论文的“优化”不都是策略改进

需要区分三类：

### 3.1 纯纠错

这些不改变策略经济逻辑，只修正实现错误：

- 14-session窗口；
- previous close；
- 半日市eligibility；
- invalid daily return；
- reversal quantity；
- commission/slippage拆分；
- share rounding真实读取；
- final-bar round trip；
- exec lag；
- unknown exit AUM污染；
- benchmark invalid close和first-day anchor。

### 3.2 执行现实化

这些会系统性降低回报，但更接近可交易结果：

- next executable open；
- pending order cancel/queue；
- halt成交限制；
- 较高执行滑点；
- 不让未成交订单获得复牌gap。

### 3.3 研究工程优化

这些主要提升可信度和可复现性：

- component validity；
- config-specific mask；
- session tiers；
- manifest/hash；
- atomic publish；
- profile attribution；
- calendar statistics；
- pre-registered evaluation spec；
- Cfg字段使用审计。

---

## 4. 当前结果如何解读

零分红、零融资/借券基线：

| Profile | CAGR | Sharpe | MDD | Cost/Share |
|---|---:|---:|---:|---:|
| official_sample_compatible | 17.04% | 1.15 | −24.5% | 0.35¢ |
| paper_spec | 16.76% | 1.16 | −26.8% | 0.45¢ |
| corrected_execution | 14.21% | 1.01 | −30.3% | 0.85¢ |

含义：

1. sample的乐观实现提高了表现，但策略并非完全由bug产生；
2. 论文文字口径仍显示全样本收益；
3. 现实成交使CAGR下降约2.5个百分点、MDD恶化；
4. corrected Sharpe约1.0，在加入真实SPY total return、融资、borrow和impact后会进一步下降；
5. 全样本结果不能回答策略是否仍活着。

---

## 5. 当前实现仍比“最终实盘模型”乐观的地方

1. 尚无完整market impact和capacity模型；
2. 融资接口已存在，但long/short/cash需按time-integral分别累计；
3. 尚无完整signals/fills/round-trip ledger；
4. 正式分红文件尚未进入当前baseline；
5. benchmark最好换独立daily raw-close数据；
6. evaluation spec尚未由唯一runner强制执行；
7. post-publication区间尚未按冻结spec一次性正式发布；
8. 统计不确定性仍需block bootstrap和HAC。

---

## 6. 为什么现在不应参数优化

已经看过2024-05-01后的早期结果，因此该区间不能再称为完全untouched OOS。此时优化参数很容易变成对已知失效区间拟合。

应先冻结并计算：

```text
post-publication gross edge per traded share
− execution cost per share
− funding cost per share
− borrow cost per share
```

决策：

- **gross edge ≈ 0**：信号已死，停止参数优化；
- **gross edge > 0，但净收益≤0**：研究执行、频率、容量；
- **gross edge和净收益均稳定为正，且置信区间支持**：才进入参数稳健性研究。

---

## 7. 推荐的最终报告结构

### Replication结果

- `official_sample_compatible × paper_ready`
- `paper_spec × paper_ready`

### Economic结果

- `corrected_execution × halt_aware`

### Sensitivity

- `exploratory`
- with/without dividends
- fixed slippage grid
- financing/borrow scenarios

所有结果必须携带：

```text
spec_sha256
engine_script_sha256
data_script_sha256
data_run_id
source_sha256
dividend_sha256
git_commit
git_dirty
```

---

## 8. 最终结论

当前项目相对论文和sample code的最大进步，不是“做出了更高收益”，而是把原本混在一起的五类影响拆开：

```text
论文信号
sample实现偏差
数据质量
成交现实
成本与统计口径
```

当前最可信的全样本经济口径是 `corrected_execution`，但它仍只是完整实盘成本前的上限估计。下一步必须以冻结的 post-publication evaluation 为核心，而不是继续调参数。
