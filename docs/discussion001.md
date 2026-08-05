> **文档状态：历史讨论稿。** 后续已完成 EOD close-source、Section 31、杠杆/定仓与统计不确定性实验；当前结论及链接以[文档索引](README.md)为准。本文保留当时问题形成过程，不作为最新结论。

这三项里，当前最可能实质改变结果的是 **EOD close代理**；pending cancel/queue在正式v2样本中实际上没有触发差异；融资假设有影响，但目前量级小于信号衰减和EOD价格口径。

## 1. Pending cancel 与 queue

### 当前模型怎么工作

信号在第 \(m\) 分钟收盘确认，默认：

\[
due\_minute=m+1
\]

随后有两种政策：

- `cancel_if_next_unavailable`：如果预定的下一分钟不能成交，订单取消；
- `queue_until_executable`：订单保留，在下一可执行分钟的open全部成交。

实现见 [`backtest_day()` 的 pending-order 状态机](../im_engine_v4.py#L590)。

无论哪种政策，订单都不会获得signal close到复牌open之间的gap：

- cancel：根本没有仓位；
- queue：在复牌open才建立仓位，从该价格之后开始计PnL。

所以两者真正的区别是：

> 复牌以后是否继续持有原来的目标仓位。

### 可能影响

#### 对成交率和turnover的影响

Cancel会：

- 降低成交次数；
- 降低commission和slippage；
- 降低市场暴露；
- 可能同时避开盈利和亏损交易。

Queue会：

- 增加成交率和turnover；
- 承担复牌后的价格变化；
- 可能以已经过时的signal成交。

因此，不能简单说cancel一定保守：

- 如果复牌后价格继续沿signal方向运行，cancel低估收益；
- 如果signal已经失效或价格反转，cancel反而高估策略质量，因为它避开亏损；
- unavailable minutes往往集中在高波动时期，这不是随机缺失，影响可能有明显方向性。

#### Signal陈旧问题

假设：

```text
10:00 close：做多信号
10:01：不可执行
10:08：恢复交易
```

Queue会在10:08 open买入，但原信号已经8分钟以前产生。期间可能发生：

- band大幅变化；
- VWAP关系改变；
- 新信息使方向反转；
- 价格已经完成大部分预期走势。

现实策略通常有第三种规则：

```text
订单失效，但在复牌后用最新信息重新计算信号
```

这既不是现有cancel，也不是现有queue。它可能是最合理的实盘规则。

#### 订单类型决定政策

必须先定义订单是什么：

- IOC/一分种有效期订单：cancel合理；
- 普通DAY market order：更接近queue；
- limit order：可能queue但不一定成交；
- reopening-auction order：可能按复牌竞价成交；
- broker risk control：某些halt订单可能被拒绝或取消。

没有订单类型时，`cancel`和`queue`都只是情景。

### 对当前正式结果的实际影响

正式账本中：

- 代表性`corrected × halt-aware × with-dividends × 0.005`有8,198张成交订单；
- 取消订单为0；
- 所有corrected cells合计账本中也没有cancelled order。

原因是当前默认一分种lag、30分钟决策网格和数据tier组合没有产生“信号刚好遇到下一分钟不可执行”的真实案例。四个2020熔断发生在开盘附近，策略实际信号和成交出现在更晚时段。

因此：

> cancel与queue是重要的规则设计问题，但它目前不影响已发布v2数字。

未来加入更细决策频率、更长lag、个股或更多halt数据后，这项才可能变得显著。

### 我的建议

未来规则最好定义为：

```text
默认：订单只对预定下一分钟有效；
若该分钟不可执行，取消；
恢复交易后重新计算最新信号，不执行旧signal。
```

同时保留`queue_until_executable`作为压力测试。这样比无期限执行陈旧target更符合策略语义。

---

## 2. EOD close代理的限制

这一项比原先想象的严重。

### 当前模型怎么做

策略不在最后一根 bar 新开仓，但已有仓位在最后可执行一分钟的 close 平仓，见 [`backtest_day()` 的 EOD 分支](../im_engine_v4.py#L638)。

正常完整交易日中，最后一根分钟bar是：

```text
15:59:00–15:59:59
```

当前引擎使用这个bar的close作为EOD成交价。

但这不一定是16:00官方closing auction价格。当前原始分钟文件没有独立的16:00 auction bar，因此：

```text
分钟文件15:59 close ≠ 官方daily close
```

### 三种现实执行方式

#### A. 提前提交MOC

策略已知自己必须日终清仓，因此可以提前提交Market-on-Close订单。

理论成交价应接近或等于官方closing auction price，而不是15:59分钟close。这里使用daily raw close是有经济依据的，不属于未来函数，因为清仓决定早已预先固定。

#### B. 15:59附近主动市价单

如果不参加closing auction，而是在15:59主动平仓，则：

- 成交价更接近当时bid/ask；
- 分钟close仍不保证可成交；
- 大订单需要spread和impact；
- 有可能错过最后几十秒或auction中的大幅变动。

#### C. 尾盘TWAP/VWAP

把平仓分散在最后数分钟：

- 降低单点auction风险；
- 改变持仓结束时间；
- 会失去或减少最后几分钟的momentum收益；
- 需要单独定义，不应偷偷替代EOD规则。

### 当前结果有多依赖EOD退出

在`corrected × halt-aware × with-dividends × 0.005`中：

| 指标 | Full sample | Post-publication |
|---|---:|---:|
| EOD平仓fill数 | 1,355 | 159 |
| EOD平仓股数占全部成交股数 | 16.69% | 17.25% |
| EOD退出round trips占比 | 33.03% | 32.58% |

更重要的是，持有到EOD的round trips贡献了主要正gross PnL；非EOD退出交易整体抵消了其中大量利润。因此EOD价格并不是一个边缘细节。

### 分钟close与独立daily close实测差异

我把正式账本中的EOD fill price与冻结的独立daily raw close直接对比：

| 指标 | Full sample | Post-publication |
|---|---:|---:|
| EOD fill数量 | 1,355 | 159 |
| 绝对差异中位数 | 约3¢ | 约4¢ |
| 差异≤1¢ | 290次 | 16次 |
| 最大绝对差异 | `$5.25` | `$5.25` |

最明显的是2025-04-09：

```text
分钟文件15:59 close：543.37
独立daily close：548.62
差异：5.25美元
```

原始分钟文件确实以15:59的543.37结束，没有收录16:00 closing print。

对现有EOD仓位做一次**静态替换核算**：

- 若将post期间EOD fill从分钟close替换为daily close；
- 不重新计算后续AUM和股数；
- PnL约增加 `$47,600`。

这个数字约等于：

- 当前post gross PnL `$121,849`的39%；
- 当前扣执行与融资后的交易边际约 `$65,700`的72%。

这只是一级审计，不是合法的新CAGR，因为没有重新走连续AUM路径。但它证明：

> EOD价格来源的影响可能大于`$0.001`与`$0.005`滑点情景之间的差异。

而且当前样本中替换daily close恰好提高收益，不能据此事后选择。必须作为新实验或新spec预先定义。

### 固定滑点在EOD可能也不合适

如果用MOC并按官方auction price成交：

- auction price本身就是成交清算价；
- 再统一扣`$0.005/share`未必与日内market-order滑点含义一致；
- closing auction impact更应该与订单占auction volume、imbalance及参与率关联。

因此未来最好拆成：

```text
intraday execution slippage
EOD auction execution/impact
```

而不是所有fill使用同一个per-share滑点。

### 我的建议

优先做一个独立、不覆盖v2的EOD实验：

1. 原模型：15:59 minute close + 固定滑点；
2. MOC模型：独立daily raw close + 明确auction成本；
3. 尾盘执行模型：最后5分钟VWAP/TWAP；
4. 报告每日价格差、方向性PnL差和连续AUM结果。

这是目前最值得优先解决的执行问题。

---

## 3. Funding spread与short-proceeds现金利息

### 当前现金账户模型

设：

- \(E\)：当时AUM；
- \(N\)：持仓notional；
- 多头\(N>0\)，空头用绝对值表示；
- benchmark为LIBOR proxy或SOFR。

当前冻结规则是：

```text
正现金收益率 = benchmark − 50bp
借入现金成本 = benchmark + 100bp
SPY borrow fee = 25bp
```

采用 PIT 日费率和 ACT/360，见 [evaluation spec v2](../config/evaluation_spec_v2.yml#L64)。

### 多头时怎么计算

如果持有多头notional \(N\)：

\[
Cash=E-N
\]

例如4倍多头：

\[
N=4E,\quad Cash=-3E
\]

引擎会对\(3E\)借入现金按：

\[
benchmark+100bp
\]

收取持仓分钟对应的融资成本。

影响特征：

- 杠杆越高，成本越大；
- 持有越久，成本越大；
- 只影响超过1倍AUM的多头借款部分；
- 1倍多头没有负现金融资，但会失去该时段的现金利息；
- 低于1倍多头的剩余现金仍可赚取利息。

### 空头时怎么计算

如果持有空头notional \(N\)：

\[
Cash=E+N
\]

当前模型假设：

1. 原本的\(E\)继续是现金；
2. 卖空所得\(N\)也成为正现金；
3. 整个\(E+N\)赚取`benchmark−50bp`；
4. 同时对\(N\)支付25bp borrow fee。

因此卖空所得的净利率近似：

\[
benchmark-50bp-25bp
=benchmark-75bp
\]

这相当于一种较优的机构账户stock-loan rebate安排。

### 为什么short proceeds存在争议

不同账户处理完全不同。

#### 机构/prime broker模式

卖空所得可能形成抵押现金，并得到接近benchmark减spread的rebate。当前模型大致可以解释为这种安排。

#### Retail broker模式

可能出现：

- 卖空所得被限制使用；
- 不向客户支付全部现金利息；
- 利息门槛或分档；
- borrow fee明显高于25bp；
- rebate与borrow fee已合并报价，不能再分开加减。

如果short proceeds不赚利息，当前模型会高估空头经济性。

### 当前正式结果中的量级

`corrected × halt-aware × with-dividends × 0.005`的post期间：

| 项目 | 金额 | 每成交股 |
|---|---:|---:|
| 借入现金funding成本 | `$7,298` | 0.1281¢ |
| SPY borrow成本 | `$435` | 0.0076¢ |
| 合计funding+borrow | `$7,733` | 0.1358¢ |
| 当前模型给予short proceeds的现金利息 | `$6,624` | 0.1163¢ |

所以short-proceeds现金利息与正式borrow/funding成本是同一量级，不能忽略。

### 几个直接敏感性

在不重算仓位路径的一级核算下，post期间：

| 假设变化 | PnL约变化 | 每成交股 |
|---|---:|---:|
| Funding spread再提高100bp | `−$1,374` | −0.0241¢ |
| Borrow rate再提高100bp | `−$1,741` | −0.0306¢ |
| Short proceeds完全不付息 | `−$6,624` | −0.1163¢ |
| 所有现金收益再降低50bp | `−$15,855` | −0.2783¢ |

最后一项大部分来自隔夜基础现金，不完全是交易成本。它会降低绝对CAGR，但若Sharpe hurdle同步使用相同现金利率，对超额Sharpe的影响会小很多。

### 需要区分三类收益

正式报告最好把现金拆成：

1. **基础现金收益**

   策略隔夜空仓时，原始AUM赚取的现金利息。它不是交易alpha。

2. **多头融资成本**

   超过AUM的多头notional需要借款，这是策略成本。

3. **空头rebate净收益**

   卖空所得现金利息减borrow fee，这是空头策略特有的融资贡献。

当前`cash_interest`把基础现金与short-proceeds利息放在一起，而`financing`放多头funding和borrow。因此虽然会计恒等式正确，经济归因还不够直观。

### 我的建议

未来敏感性至少做：

```text
Institutional:
short proceeds earn benchmark − 50bp
borrow = 25bp

Conservative institutional:
short proceeds earn benchmark − 100bp
borrow = 50–100bp

Retail/no-rebate:
short proceeds earn 0
borrow按实际broker费率
```

Funding spread则至少比较：

```text
benchmark + 50bp
benchmark + 100bp
benchmark + 200bp
```

但从当前量级看，融资假设会改变结果，却不太可能单独解释post-publication从高表现降到7% CAGR、0.27 Sharpe。EOD价格代理反而可能是一阶影响。

## 优先级判断

我会这样排序：

1. **EOD close/MOC价格来源**：已有证据显示可能重大，应最先实验；
2. **short-proceeds利息归属**：量级可见，且高度依赖账户类型；
3. **funding/borrow spread敏感性**：需要做，但当前影响较小；
4. **pending cancel/queue**：规则必须定义清楚，但正式v2样本中没有触发，暂不影响现有结论。

本轮仍是只读审查，没有改文件、没有重新运行回测。数值来自正式账本及其冻结的独立daily benchmark；EOD `$47,600`是静态替换审计，不是新策略结果。


改成标准md 格式
