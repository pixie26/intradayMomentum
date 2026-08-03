# IBKR 美国股票交易成本与 Section 31 回测处理讨论纪要

**整理日期：** 2026-08-02  
**讨论范围：** IBKR Pro Tiered 美国股票佣金、第三方费用、Section 31 历史费率，以及这些费用在 2008 年至今的 SPY 日内策略回测中应如何建模。

**复核与实施状态：** 主结论合理，但原稿的 FY2009 费率示例需要修正：
公布过的 `9.30/million` 最终没有生效。项目已实施 P0 的可审计版本：完整
Section 31 生效日表、卖出端逐 fill 计费、分项 ledger/daily accounting、严格覆盖
校验和独立 post-result 敏感性实验。默认/frozen 路径保持不变。P1/P2 中没有可靠
历史序列或账户账单校准的项目没有被伪装成“精确 all-in 成本”。

---

## 1. 背景

当前项目正在复现和评估一个以 SPY 为主要交易标的的日内动量策略，样本大致覆盖 2008 年至今。

原有回测或研究假设中，可能使用了类似以下交易成本设定：

```text
IBKR commission = USD 0.0035 / share
slippage        = USD 0.0025 / share
```

核心疑问是：

1. IBKR 所列的 `USD 0.0035/share` 是否已经包含 SEC Section 31、FINRA TAF、CAT、清算费用及交易所费用；
2. Section 31 是否从 2008 年至今一直收取；
3. 回测能否将当前费率作为一个固定常数应用于全部历史时期；
4. 如何建立更接近真实执行的 all-in transaction cost model。

---

## 2. 核心问题

### 2.1 `USD 0.0035/share` 是不是 all-in commission？

不是。

`USD 0.0035/share` 对应的是 IBKR Pro **Tiered** 定价下的 IBKR 经纪佣金本体，通常对应月成交量不超过 300,000 股的第一档。

IBKR 官方资料明确表示，Tiered 定价之外还会传递或收取：

- exchange fees 或 exchange rebates；
- regulatory fees；
- clearing fees；
- 其他适用的第三方费用或 pass-through fees。

因此，以下等式通常不成立：

```text
all-in explicit cost = shares × 0.0035
```

更合理的表达是：

```text
all-in explicit cost
= IBKR base commission
+ regulatory fees
+ clearing fees
+ exchange / ECN fees
- exchange / ECN rebates
+ other pass-through fees
```

此外，还需要与市场冲击和滑点分开建模。

---

## 3. 主要发现

### 3.1 IBKR Tiered 的 `0.0035/share` 只是基础佣金

以当前公开费率为例，IBKR Pro Tiered 美国股票第一档为：

```text
USD 0.0035 × shares
```

但还受到以下约束：

- 每单最低佣金；
- 每单最高佣金或成交金额比例上限；
- 月成交量分档；
- 客户所属 IBKR 法律实体、账户类型及路由方式；
- 某些 directed API order 可能不适用 Tiered 结构。

因此，即使暂时不考虑第三方费用，也不能对每笔交易无条件使用：

```python
commission = shares * 0.0035
```

至少应写成类似：

```python
commission = max(minimum_per_order, shares * applicable_volume_tier_rate)
```

并进一步处理最高费用限制及分档累计成交量。

### 3.2 第三方费用需要另外计算

IBKR 当前列示的美国股票相关成本包括但不限于：

- SEC Section 31 transaction fee；
- FINRA Trading Activity Fee（TAF）；
- FINRA Consolidated Audit Trail（CAT）相关费用；
- clearing fee；
- pass-through fee；
- exchange / ECN fee 或 rebate。

这些项目不是统一的固定每股数字：

- Section 31 通常按卖出成交金额计算；
- TAF 通常按卖出股数计算，并可能存在每笔上限；
- CAT 和 clearing 可能按成交数量计算；
- exchange / ECN 成本取决于成交 venue、订单类型以及 maker/taker 状态；
- 开盘和收盘 auction 的费用可能不同于连续交易时段普通 remove-liquidity 费率。

法律口径还要再精确一步：Section 31 的法定义务直接落在 SRO，而不是 SEC 直接
向终端客户收费；本项目建模的是 IBKR 向客户传递的 regulatory fee。IBKR 当前
网页把它列为 `SEC Transaction Fee = rate × aggregate sales value`，但客户账单的
聚合和舍入方式仍应由实际 statement 校准。

### 3.3 Tiered 与 Fixed 不能混为一谈

IBKR 的 Tiered 和 Fixed 是两套不同的定价逻辑。

- **Tiered：** 基础佣金较低，但交易所、清算、监管等外部成本通常另外传递，成交 venue 的 rebate 也可能反映给客户；
- **Fixed：** 固定每股佣金通常包含更多交易所和清算成本，但仍需仔细核对监管费及例外条款。

因此，本项目若使用 `0.0035/share`，实际上已经隐含选择了 **Tiered** 定价假设，不能再套用 Fixed 的“部分费用包含”逻辑。

---

## 4. Section 31 的历史结论

### 4.1 Section 31 在 2008 年已经存在

Section 31 并不是近年新增费用。2008 年样本开始时，该项费用已经存在并适用于 covered sales。

SEC 会根据法定目标收费金额和预计市场成交额调整费率，历史上可能在年度开始或年中变化。因此它是一条**按生效日期变化的时间序列**，而不是固定参数。

2008—2009 年的官方公告即可看到多次费率变化：

- 数据起始的 2008-01-22 至 2008-01-24：`USD 15.30 / million`；
- 2008-01-25 起：`USD 11.00 / million`；
- 2008-04-01 起：`USD 5.60 / million`；
- FY2009 曾公布 `USD 9.30 / million`，但因拨款时点没有实际生效；
- `USD 5.60 / million` 实际延续到 2009-04-09，2009-04-10 起变为
  `USD 25.70 / million`。

完整、连续的 23 段生效日表已实现于
`data/reference/sec_section31_rates.csv`。

### 4.2 2025—2026 年存在一段零费率时期

SEC 官方公告显示：

- 截至 2025-05-13：`USD 27.80 / million`；
- 2025-05-14 至 2026-04-03：`USD 0.00 / million`；
- 2026-04-04 起：`USD 20.60 / million`。

因此，“Section 31 从 2008 年到现在每天都有正成本”并不准确。

更准确的说法是：

> Section 31 制度长期存在，但实际适用费率会变化，并可能在特定期间为零。

### 4.3 不能把当前 `0.0000206` 倒推到 2008 年

当前 `USD 20.60 / million` 换算为成交金额比例是：

```text
20.60 / 1,000,000 = 0.0000206
```

但回测中不能将其应用于 2008 年至今所有卖出交易。

正确方式是：

```python
section31_cost = sell_notional * section31_rate_on_trade_date
```

其中：

```python
sell_notional = sell_price * shares_sold
section31_rate_on_trade_date = rate_per_million / 1_000_000
```

对于普通日内多头策略，通常在平仓卖出时产生；对于日内空头策略，卖空开仓本身就是一次 sale，也应按适用规则处理。

---

## 5. 对当前回测的影响

### 5.1 现有显性交易成本会被低估，但总执行成本需防止重复计算

如果回测只使用：

```text
USD 0.0035/share commission
```

并把它理解成全部显性成本，则会遗漏：

- Section 31；
- FINRA TAF；
- CAT；
- clearing；
- pass-through；
- exchange / ECN fee 或 rebate；
- 每单最低佣金的影响。

对于毛收益较低、换手频率较高的日内策略，这些遗漏可能显著影响净 Sharpe、年化收益和可交易性判断。
不过，如果某个经验性 slippage 参数本来就按真实账单或成交结果校准，并已经
吸收了部分 venue/regulatory 成本，再逐项叠加会重复计费。因此“显性费用遗漏”
是确定的，“all-in 总成本一定低估多少”则必须结合 slippage 定义和 statement
calibration 才能回答。

### 5.2 Section 31 会造成随价格变化的成本结构

Section 31 按卖出名义金额收费，而不是简单按股数收费。

同样卖出 1 股：

- SPY 价格较低时，Section 31 每股成本较低；
- SPY 价格较高时，Section 31 每股成本较高。

如果使用固定的“每股总成本”，可能产生明显的时间偏差，尤其是在 SPY 名义价格长期上涨的样本中。

### 5.3 用当前费率覆盖历史会制造前视或重估偏差

把 2026 年费率用于 2008—2024 年，会错误重估历史成本；把非零费率用于 2025-05-14 至 2026-04-03，则会直接虚构不存在的 Section 31 成本。

因此，交易成本模型必须与交易日期关联。

### 5.4 开盘和收盘成交不能简单按普通 taker 费率估算

如果策略假设在官方开盘价或收盘价成交，其真实订单更接近 opening/closing auction，而不是连续交易时段中直接 remove displayed liquidity。

因此：

- 不应无条件给每边都加普通 taker fee；
- 需要核对可能成交的交易所、auction order 类型和当期费率；
- 若无法重建 venue，应设置低、中、高三种 venue-cost 情景，而不是伪造一个精确值。

---

## 6. 建议的改进方法

## 6.1 将成本拆为独立组件

建议每笔成交至少记录以下字段：

```text
trade_date
side
quantity
execution_price
notional
ibkr_commission
section31_fee
finra_taf
cat_fee
clearing_fee
pass_through_fee
exchange_fee
exchange_rebate
slippage
market_impact
total_explicit_cost
total_execution_cost
```

其中：

```text
total_explicit_cost
= ibkr_commission
+ section31_fee
+ finra_taf
+ cat_fee
+ clearing_fee
+ pass_through_fee
+ exchange_fee
- exchange_rebate
```

```text
total_execution_cost
= total_explicit_cost
+ slippage
+ market_impact
```

### 6.2 建立 Section 31 历史费率表

建议建立单独的数据文件，例如：

```text
data/reference/sec_section31_rates.csv
```

字段建议：

```csv
effective_from,effective_to,rate_per_million,rate_decimal,source_url,notes
2008-01-25,2008-03-31,11.00,0.00001100,...,...
2008-04-01,...,5.60,0.00000560,...,...
2025-05-14,2026-04-03,0.00,0.00000000,...,SEC rate set to zero
2026-04-04,,20.60,0.00002060,...,...
```

回测时通过交易日期做 as-of join：

```python
rate = section31_schedule.lookup(trade_date)
section31_fee = sell_notional * rate
```

当前实现比示意代码更严格：费率表必须覆盖连续 calendar-date 区间、
`rate_decimal == rate_per_million / 1,000,000`、每段必须有 SEC HTTPS 来源；
任何回测 session 缺覆盖都会硬失败。周末生效日仍保留在表中，因此
2026-04-04 的生效规则不会被错误改写成公告日或下一个任意数据行。

### 6.3 对其他费用也尽量历史化

Section 31 不是唯一发生变化的费用。

后续还应调查：

- FINRA TAF 历史费率及单笔上限；
- CAT fee 的开始日期和历史变化；
- clearing fee 的历史变化；
- IBKR Tiered 分档、最低佣金和最高佣金的历史变化；
- 主要交易所 opening/closing auction 历史费率；
- IBKR 是否在不同时期完整传递全部 rebate。

如果无法取得完整历史资料，应明确披露：

```text
哪些费用使用真实历史费率；
哪些费用使用当前费率代理；
哪些费用被设为情景假设；
哪些成本未被纳入。
```

### 6.4 将费用假设与滑点假设分开

不建议使用一个总的 `cost_per_share` 同时代表佣金、监管费和滑点。

原因是：

- 佣金与数量相关；
- Section 31 与卖出金额相关；
- 最低佣金与订单笔数相关；
- venue fee 与路由和流动性状态相关；
- 滑点与价差、成交量、波动率和参与率相关。

混为一个常数会使模型难以审计，也无法判断结果对哪个假设最敏感。

### 6.5 建立三档交易成本情景

在无法准确重建历史 venue 时，可先使用：

#### 情景 A：乐观

- Tiered base commission；
- 实际历史监管费；
- 较低 auction fee 或部分 maker rebate；
- 较低滑点。

#### 情景 B：基准

- Tiered base commission；
- 实际历史监管费；
- clearing、CAT、TAF；
- 中性 auction / venue 成本；
- 基于 bid-ask spread 的滑点。

#### 情景 C：保守

- 最低佣金完整生效；
- 较高 venue/taker 成本；
- 无 rebate；
- 较高滑点和市场冲击。

最终研究结论应报告策略在三种情景下是否仍然成立，而不是只报告单一成本数字。

---

## 7. 推荐的实施优先级

### P0：必须立即修正

1. 不再把 `USD 0.0035/share` 描述为 all-in commission；
2. 将 Section 31 改为按卖出金额计算；
3. Section 31 使用按生效日期匹配的历史费率；
4. 对 2025-05-14 至 2026-04-03 设置零费率；
5. 将显性费用和 slippage 分开输出。

**实施：已完成。** 使用 `explicit_cost_model=legacy_plus_section31` 显式启用；
未提供费率表会失败。默认 `legacy` 完全保留 frozen 结果。fill ledger 记录
`buy_shares`、`sell_shares`、`sell_notional`、`ibkr_commission`、
`section31_rate_decimal`、`section31_fee`、`total_explicit_cost`、`slippage`
和 `total_execution_cost`。日度结果保留相同分解及会计恒等式。

### P1：应在下一轮回测加入

1. 每单最低佣金；
2. FINRA TAF；
3. clearing fee；
4. CAT fee；
5. opening/closing auction venue 成本情景；
6. long 与 short 的卖出端费用逻辑。

**实施边界：** 第 6 项已完成；多头平仓、卖空开仓和 long→short reversal 的
卖出数量均计 Section 31，short cover 买入不计。其余 P1 项尚未正式加入，原因是
当前官网费率不能无披露地倒推到 2008 年；它们必须先建立历史表或冻结为明确的
current-rate proxy 情景。

### P2：用于提高机构级准确度

1. 完整重建 2008 年以来各项第三方费用历史；
2. 根据实际 IBKR Flex Report 或 Activity Statement 校准模型；
3. 根据订单路由记录重建 venue 和 maker/taker 状态；
4. 按月累计成交量动态调整 Tiered 档位；
5. 对大额订单加入 participation-rate market impact model。

---

## 8. 建议的验证方法

最可靠的方法不是只读取费率网页，而是使用真实小额成交进行账单校准。

建议从 IBKR 导出 Flex Query 或 Activity Statement，抽取若干交易，核对：

```text
成交日期
买卖方向
成交股数
成交金额
IB commission
Regulatory fees
Exchange fees/rebates
Clearing fees
总费用
```

然后将账单结果与模型逐项比较。

建议选择以下样本：

- 普通连续交易时段成交；
- opening auction 成交；
- closing auction 成交；
- maker 成交；
- taker 成交；
- 小订单，触发最低佣金；
- 大订单，不受最低佣金主导；
- 买入交易；
- 卖出交易；
- 卖空开仓与买入平仓。

只有经过 statement calibration，才能确认模型反映的是特定账户的真实收费，而不仅仅是官网标准费率。

---

## 9. 尚待确认的问题

以下信息会影响最终成本模型：

1. 实际账户采用 Tiered 还是 Fixed；
2. 账户由哪个 IBKR 法律实体承接；
3. 每月预计成交股数和适用佣金档位；
4. 策略是一次性开平仓，还是拆单执行；
5. 是否使用 SMART routing 或 directed routing；
6. 开盘、收盘信号在实际执行时使用 MOO/MOC、LOO/LOC，还是连续交易订单；
7. 是否能取得历史成交 venue 或 IBKR statement；
8. 当前回测中的 `USD 0.0025/share slippage` 是单边还是 round trip；
9. 现有滑点是否已经隐含 bid-ask spread 或 exchange fee，以避免重复计算。

---

## 10. 最终结论

本次讨论得到的核心结论是：

> IBKR 的 `USD 0.0035/share` 是 Tiered 基础佣金，不是美国股票交易的 all-in cost。

Section 31 从 2008 年样本开始时已经存在，但费率会随时间调整，且 2025-05-14 至 2026-04-03 曾为零。因此：

> 回测不能把当前 Section 31 费率应用于全部历史，也不能使用一个固定每股成本代替所有佣金、监管费、venue 成本和滑点。

对于毛收益较薄的 SPY 日内策略，交易成本建模方式可能直接决定 post-cost 结果是盈利还是亏损。下一步应优先建立日期化的监管费率表、逐笔最低佣金逻辑和 opening/closing auction 成本情景，再重新计算净收益及 Sharpe。

### 10.1 已实施敏感性结果（post-result，不改写 frozen headline）

实验：`corrected_execution × halt_aware × with-dividends × $0.0025/share`
保持信号、成交、融资和 slippage 不变，只在 legacy 基础佣金上增加历史
Section 31：

| 窗口 | Legacy Portfolio CAGR | + Section 31 Portfolio CAGR | Legacy Trading-only CAGR | + Section 31 Trading-only CAGR | Portfolio 变化 | Sharpe 变化 | Section 31 / 总成交股 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Full sample | 16.70% | 15.57% | 15.25% | 14.13% | −1.129pp | 1.075 → 1.006 | 0.2370¢ |
| Pre-publication | 18.00% | 16.84% | 16.97% | 15.82% | −1.163pp | 1.184 → 1.113 | 0.1944¢ |
| Post-publication | 7.52% | 6.62% | 3.27% | 2.41% | −0.896pp | 0.300 → 0.243 | 0.4271¢ |

正式 legacy 对照与既有 formal run 的逐日最大数值差为 `0`；新增模型会计残差
最大约 `1.16e-10`，零费率区间实际收费为 `0`。结果见
`docs/IBKR_SECTION31_COST_SENSITIVITY_ZH.md` / `.html`。这仍只量化
Section 31，不能称作 all-in IBKR 成本。

---

## 11. 官方资料

### Interactive Brokers

- US Stocks / ETFs commissions and third-party fees:  
  https://www.interactivebrokers.com/en/pricing/commissions-stocks.php

- IBKR fact sheet，说明 Tiered 佣金之外另加 exchange、regulatory 和 clearing fees：  
  https://www.interactivebrokers.com/en/general/about/ibkr-fact-sheet.php

### U.S. Securities and Exchange Commission

- Section 31 basic information and rate-adjustment mechanism:  
  https://www.sec.gov/rules-regulations/fee-rate-advisories/section-31-transaction-fees-basic-information-firms

- SEC Fee Rate Advisories archive:  
  https://www.sec.gov/rules-regulations/fee-rate-advisories

- FY2025 advisory，2025-05-14 起调整为零：  
  https://www.sec.gov/rules-regulations/fee-rate-advisories/2025-2

- FY2026 advisory，2026-04-04 起恢复为 USD 20.60/million：  
  https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2

- 2008 年费率从 USD 11.00/million 调整至 USD 5.60/million：  
  https://www.sec.gov/news/press/2008/2008-25.htm

- FY2009 新费率实际从 2009-04-10 起为 USD 25.70/million（也证明
  USD 5.60 延续到此前）：  
  https://www.sec.gov/news/press/2009/2009-56.htm

---

## 12. 说明

本文是对当前讨论的研究纪要，不是完整的 IBKR 历史收费数据库。官网展示的当前收费标准也可能因账户实体、账户类型、执行地点、订单路由及后续费率调整而变化。在正式发布回测结论前，应保存官方历史费率来源，并使用实际 IBKR statement 对模型进行校准。
