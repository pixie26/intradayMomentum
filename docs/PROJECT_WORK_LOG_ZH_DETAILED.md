# Intraday Momentum 项目完整研究与优化记录（详细解释版）

**记录日期：2026-07-29**  
**研究对象：SPY 一分钟日内动量策略**  
**数据范围：约 2008–2026 年 SPY 一分钟行情**  
**当前状态：数据清洗层和核心回测状态机基本完成；真实分红、完整融资/借券成本及冻结的论文公开后评估尚待执行。**

---

## 0. 这份记录要回答什么

这不是一份只记录“改了哪些代码”的开发日志，而是一份研究审计记录。它要说明：

1. 原论文的策略究竟在做什么；
2. 作者 sample code 与论文有哪些差异；
3. 原始 GitHub 回测为什么不能直接相信；
4. 数据中可能出现哪些问题，例如“冲突 OHLCV”“缺失 minute/session”，这些问题具体是什么意思；
5. 每个问题为什么会改变回测结果；
6. 我们如何发现、验证和修复；
7. 修复后收益为什么下降；
8. 当前结果能够说明什么、不能说明什么；
9. 下一步如何判断策略是否仍有真实可交易价值。

整个项目的原则不是“把收益调高”，而是：

> **把每一个可能导致回测虚高、结果不可复现或风险被低估的环节拆开，逐项验证并留下审计证据。**

---

# 1. 项目起点与研究目标

项目起初希望复现论文：

> **Beat the Market: An Effective Intraday Momentum Strategy for S&P 500 ETF (SPY)**

并使用长期 SPY 一分钟数据回答四个核心问题：

1. 论文结果能否准确复现；
2. 作者 sample code 的高收益是否包含实现偏差；
3. 在更严格的数据和成交假设下，策略是否仍有收益；
4. 2024-05-01 论文公开后，信号是否继续有效。

项目早期曾考虑使用 Qlib。后来决定暂不引入，原因是：

- 当前只有一个标的 SPY；
- 策略是分钟级规则策略，并非首先需要机器学习；
- 最大风险不是模型能力不足，而是数据、时间戳、成交和成本口径不清楚；
- 在底层逻辑尚未固定前，换用大型框架只会把错误包装得更复杂。

因此先建立原生、透明、逐分钟可追踪的研究引擎。

---

# 2. 先解释项目中反复出现的术语

## 2.1 OHLCV 是什么

一分钟行情通常每行代表一分钟，包含：

| 字段 | 含义 |
|---|---|
| Open | 这一分钟第一笔或代表性的开盘价格 |
| High | 这一分钟最高价格 |
| Low | 这一分钟最低价格 |
| Close | 这一分钟最后价格 |
| Volume | 这一分钟成交量 |

例如：

```text
09:30  Open=500.00  High=500.40  Low=499.80  Close=500.25  Volume=1,200,000
```

这些字段是策略计算价格变化、VWAP、信号和 PnL 的基础。

---

## 2.2 “重复 timestamp”是什么意思

timestamp 是时间标记。例如：

```text
2024-01-03 09:30:00
```

理论上同一个标的、同一分钟只能有一行。

如果出现两行相同 timestamp，就叫重复 timestamp。

### 完全一致的重复

```text
09:30  500.00  500.40  499.80  500.25  1,200,000
09:30  500.00  500.40  499.80  500.25  1,200,000
```

通常是文件拼接时同一行被复制两次，可以安全去重，但必须记录。

### 冲突 OHLCV

```text
09:30  500.00  500.40  499.80  500.25  1,200,000
09:30  500.00  501.10  499.70  500.90  2,100,000
```

同一分钟却有不同价格或成交量，这就叫**冲突 OHLCV**。

它可能来自：

- 两个 vendor 对分钟bar定义不同；
- 数据拼接边界重复；
- 后续修订行情覆盖了旧数据；
- 一个文件使用 adjusted price，另一个使用 raw price；
- timestamp 时区或 bar label 不同。

### 为什么冲突 OHLCV 很危险

不能随便：

- 取平均；
- 取最后一行；
- 取成交量大的那行；
- 静默保留第一行。

因为任何选择都会改变：

- 当分钟是否突破 band；
- 累计 VWAP；
- 进出场价格；
- 当日最高回撤；
- 后续同一分钟的历史波动。

因此默认处理是：

> **发现冲突即停止，并输出冲突明细，先确认数据来源再决定。**

---

## 2.3 “非法 OHLC”是什么意思

OHLC 应满足基本价格关系：

```text
High >= Open
High >= Close
High >= Low
Low <= Open
Low <= Close
价格必须大于 0
Volume 必须 >= 0
```

例如：

```text
Open=500, High=499, Low=498, Close=500.5
```

High 比 Open 和 Close 都低，这是不可能的。

非法 OHLC 可能来自：

- 字段映射错误；
- 小数点或币种错误；
- 数据损坏；
- High/Low 列被交换；
- vendor 异常记录。

策略可能因为一个错误价格点，产生虚假的突破信号和巨大 PnL。因此默认不自动修补。

---

## 2.4 “缺失 minute”是什么意思

正常美股完整交易日从 09:30 到 16:00，共 390 个一分钟区间。若使用 start-labelled bar，时间通常是：

```text
09:30, 09:31, ..., 15:59
```

如果当天缺少：

```text
11:19
```

就叫缺失 minute。

### 缺一分钟为什么会影响策略

即使只缺一分钟，也可能影响：

- 这一分钟是否产生信号；
- 前后价格差形成的 PnL；
- 累计成交量；
- 累计 VWAP；
- 下一决策点的 VWAP 判断；
- 当日最高回撤；
- 是否能在预定时间成交。

因此我们不使用 forward fill 来造出一根假bar。

---

## 2.5 “缺失 session”是什么意思

session 指一个完整交易日。

如果交易所当天正常开市，但数据文件里整天一根bar都没有，就叫缺失 session。

缺失 session 比缺失一分钟更严重，因为它会影响：

- 当日策略收益；
- 前一日/后一日收益；
- 14日波动率窗口；
- 过去14个交易日的 sigma；
- benchmark；
- CAGR 的时间轴。

如果直接在已有日期上 rolling，程序会自动跳过缺失日，再向前多取一个更老日期，悄悄改变“过去14个交易日”的含义。

---

## 2.6 Leading、interior、trailing gap 是什么

### Leading-truncated

当天开盘数据缺失，例如第一根bar不是09:30，而是11:15。

这最危险，因为策略中的：

```text
move_open = |当前价格 / 当日开盘价 - 1|
```

会错误地把11:15价格当成当日开盘价。当天所有后续 `move_open` 都会使用错误锚点，并污染未来14个session的历史 sigma。

### Interior gap

当天中间缺几分钟，例如11:19–11:23缺失，但开盘和收盘都存在。

影响主要从缺口开始：

- 累计 VWAP 不完整；
- 缺口后的信号可信度下降；
- 持仓跨缺口时，成交和 PnL 路径不完整。

### Trailing-truncated

尾盘数据缺失，例如最后一根bar在14:30，而非15:59。

影响包括：

- 无法知道真实收盘价；
- 无法确定是否在收盘前平仓；
- 当日日收益不可信；
- 次日 previous close 不可信；
- 日波动率窗口被污染。

---

## 2.7 半日市为什么不是缺失数据

感恩节后等部分交易日可能在13:00提前收盘。

半日市只有约210根一分钟bar，而不是390根。这是交易所安排，不是数据缺失。

早期逻辑把半日市的下午分钟当成“缺失”，导致：

- 下午 minute 240–390 被错误计为0；
- 半日市之后，下午的14-session历史样本数减少；
- `band_warm` 错误变成 False。

后来改为：

> 每个 minute 只在交易所当天原本安排该分钟的 eligible sessions 上计算历史窗口。

因此半日市下午不是缺失，而是“结构上不适用”。

---

## 2.8 Halt / 熔断是什么

市场剧烈下跌时，交易所可能暂停交易。例如2020年3月出现市场熔断。

halt期间：

- 没有正常连续成交；
- 不能下单或成交；
- 但已经持有的仓位仍承担复牌跳空风险。

数据 vendor 可能：

- 完全不提供 halt 分钟bar；
- 提供零成交量、价格不变的 phantom bar；
- 提供聚合或特殊bar。

因此不能只检查“当天总bar数量”。必须比较：

```text
交易所计划分钟
减去官方 halt minute
与实际观察分钟
```

并识别：

- 官方 halt 内出现的 phantom bars；
- halt 之外真正缺失的分钟；
- 重叠 halt 窗口是否被重复计算。

---

## 2.9 VWAP 是什么，为什么缺数据会影响它

VWAP 是成交量加权平均价格：

```text
VWAP = 累计(价格 × 成交量) / 累计成交量
```

论文用 VWAP 区分趋势是否足够强。

如果中间缺5分钟，误差不是简单的 `5/390`，因为每分钟成交量不同。若缺失发生在：

- 开盘；
- FOMC；
- 宏观数据发布；
- 熔断前后；
- 大幅跳价时段；

这几分钟可能占很大成交量，并足以改变价格是否高于或低于 VWAP。

因此我们为每个minute维护 `vwap_valid`：

- 缺口前仍可有效；
- 缺口后失效；
- 官方 halt 不视为普通缺失。

---

## 2.10 Raw price、adjusted price 和分红

### Raw price

实际市场当时交易的价格。除息日开盘会机械下移。

### Adjusted price

为历史连续性调整过的价格，可能已经把分红或拆股反映进去。

论文信号需要知道：

```text
前一日收盘价 - 当日现金分红
```

如果输入价格已调整，再减一次分红会重复调整；如果价格未调整却不减分红，会把除息gap误判为市场动量。

因此我们：

- 不直接改写原始分钟OHLC；
- 单独清理 ex-dividend 文件；
- 只在策略特征层计算 `prev_close_adjusted`；
- 对“数据是否raw”只输出证据，不做过强断言。

---

## 2.11 Manifest 和 SHA-256 是什么

Manifest 是每次数据处理运行的说明书，记录：

- 输入文件；
- 日期范围；
- 参数；
- timezone；
- bar label；
-输出文件；
- 异常数量；
- 脚本版本；
- Git信息。

SHA-256 是文件指纹。只要文件有一个字节变化，hash通常都会变化。

记录hash的目的：

> 保证未来能准确知道某次回测究竟用了哪一份数据、哪一版脚本和哪一套配置。

---

# 3. 论文和作者 sample code 的研究

## 3.1 论文策略概念

策略使用：

- 当日开盘价；
- 前一交易日经现金分红调整后的收盘价；
- 过去14个交易日同一分钟相对开盘波动；
- 当日累计VWAP；
- 每30分钟决策；
- 2%日波动目标；
- 最大4倍杠杆。

信号大致为：

```text
close > upper_band 且 close > VWAP  → 做多
close < lower_band 且 close < VWAP  → 做空
否则                                  → 空仓
```

---

## 3.2 作者 sample code 做对了什么

作者sample有几个重要优点：

1. 给出了清晰的band和VWAP信号；
2. 将信号后移一分钟，避免最直接的同bar未来函数；
3. 反手时用仓位变化绝对值计算交易单位；
4. 在除息日调整previous close；
5. 展示了波动率目标和最大杠杆的思路。

因此sample是理解作者意图的重要参考。

---

## 3.3 作者 sample code 的主要偏差

### 日波动率 off-by-one

sample使用的切片漏掉最近一个已完成交易日。

**为什么重要：**  
当市场波动突然上升或下降时，最近一天信息最重要。漏掉它会改变杠杆。

**如何修：**

```text
daily_return.shift(1).rolling(14)
```

严格使用昨天及此前共14个完整交易日。

---

### `sigma_open` 只要求13个历史样本

sample允许 `min_periods=13`，论文定义是14日。

**为什么重要：**  
策略可能提前一天开始交易，也会在数据缺口后错误地认为历史样本充足。

**如何修：**  
严格要求14个 eligible exchange sessions。

---

### 波动率缺失时使用4倍杠杆

sample在无法计算风险时反而使用最大杠杆。

**为什么重要：**  
风险估计缺失通常应该不交易，而不是承担最高风险。

**如何处理：**

- sample兼容模式保留这一行为，便于归因；
- paper/corrected模式在风险无效时跳过或按明确规则处理。

---

### 未完整包含论文滑点

sample主要扣每股佣金，论文还讨论了slippage。

**为什么重要：**  
分钟策略换手高，极小的每股成本长期累计后会显著压低收益。

---

### Row-based rolling

sample只在实际存在的数据行上rolling。

**为什么重要：**  
某个minute缺失时，它会被跳过，并向前多找一天，变成“最近14个存在的观察”，不再是“最近14个交易日”。

**修复：**  
使用完整交易所 session-minute grid，让缺失也占据窗口槽位并保留NaN。

---

### 成交价格偏乐观

sample大致使用signal后shift的close-to-close收益。

**为什么重要：**  
只有在bar结束后才能看到该bar的close、VWAP和band。现实中通常最快只能在下一根可成交bar下单，不能保证按上一根close成交。

**修复：**

- sample profile保留原惯例；
- paper profile按论文文字；
- corrected profile使用next executable open。

---

# 4. 为什么必须先重建数据层

最初GitHub回测直接读取merged数据。如果结果异常，无法判断是：

- 信号无效；
- 时间戳错位；
- 重复数据；
- 缺分钟；
- 半日市；
- halt；
- 分红；
- vendor拼接；
- 成交逻辑。

因此项目顺序改为：

```text
原始数据
→ 行级校验
→ 交易日/分钟完整性
→ 组件有效性
→ 特征
→ 信号
→ 订单
→ 成交
→ PnL与成本
→ 统计与评估
```

---

# 5. 数据 pipeline 具体做了什么

## 5.1 统一时区和bar语义

处理：

- UTC；
- America/New_York；
- 夏令时切换；
- start-labelled bar；
- timestamp是否落在整分钟；
- epoch单位识别；
- 1980–2100日期合理性。

### 为什么重要

如果09:30 UTC被错误当成纽约09:30，整天会偏移4–5小时。

如果09:59代表bar开始还是bar结束没有明确，所谓“10:00决策”可能差一分钟。分钟策略中一分钟足以改变成交价格和收益。

---

## 5.2 使用真实XNYS交易日历

识别：

- 正常交易日；
- 节假日；
- 周末；
- 半日市；
- 每日计划开收盘时间。

### 为什么重要

不能简单假设每天都有390根bar。半日市只有约210根，是正常交易安排。

---

## 5.3 重复与冲突检查

处理流程：

1. 相同timestamp、OHLCV完全一致：记录并安全去重；
2. 相同timestamp、OHLCV不同：输出冲突报告并默认失败。

### 为什么不自动选择一行

因为没有证据说明哪一行更真实。自动取最后一条会让结果不可审计。

---

## 5.4 OHLCV合法性

检查：

- 正价格；
- High/Low关系；
- 非负成交量；
- finite值；
- 时间单调；
- timestamp唯一。

异常数据默认报错。只有用户显式选择drop时才删除，并记录受影响session。

---

## 5.5 Session质量分类

每个交易日统计：

- expected bars；
- actual bars；
- missing bars；
- leading missing；
- interior missing；
- trailing missing；
- 是否半日市；
- 是否halt；
- 是否整日缺失；
- 第一和最后实际bar。

形成三层输出：

### `paper_ready`

分钟完整、适合严格论文复现。

### `halt_aware`

包括完整日和经过官方halt minute集合验证的熔断日。

### `exploratory`

允许少量缺口，仅用于敏感性，不能默认解释为可实现经济收益。

---

## 5.6 Component-level validity

这是数据层最重要的升级。

最初尝试用一个session级 `is_tradable` flag判断整天是否能用。后来发现不同特征依赖不同数据，不能用一个flag概括。

例如一个中间缺口日：

- 开盘存在；
- 当分钟价格存在；
- 所以12:00的 `move_open` 仍可计算；
- 但从缺口后累计VWAP已不完整；
- 收盘可能仍然有效；
- 次日previous close仍可用。

因此拆分为：

| 字段 | 表示什么 |
|---|---|
| `bar_present` | 该计划minute是否真的有bar |
| `open_valid` | 当天09:30开盘锚点是否可信 |
| `close_valid` | 当天最后计划minute是否可信 |
| `prev_close_valid` | 次日使用的前收是否可信 |
| `daily_ret_valid` | 当日日收益是否可信 |
| `move_open_obs_valid` | 当前minute相对真实开盘的波动是否可信 |
| `vwap_valid` | 从开盘至当前minute的VWAP路径是否完整 |
| `is_halt_minute` | 是否官方暂停交易minute |
| `is_executable_minute` | 是否可以真实成交 |
| `is_scheduled_decision_minute` | 是否默认决策minute |

### 具体例子

#### 2009-07-27开盘截断

- `open_valid=False`
- `close_valid=True`

含义：

- 当天不能贡献任何 `move_open`；
- 但当天真实尾盘仍可能用于次日previous close。

#### 2016-02-02中间缺口

- 缺口前 `vwap_valid=True`
- 缺口后 `vwap_valid=False`
- 但有bar且真实开盘存在时，`move_open_obs_valid=True`

#### 2019-08-12尾盘截断

- `close_valid=False`
- 当日和次日相关daily return无效。

#### 2020-03-09 halt

- halt分钟 `is_executable=False`
- 不允许下单；
- 但官方停牌不应被视为遗漏成交量，因此不应破坏VWAP连续定义；
- 复牌跳空仍必须计入既有持仓PnL。

---

## 5.7 为什么不能先过滤 `paper_ready` 再算特征

曾经的接口是：

```text
paper_ready.parquet
→ 算previous close和rolling features
```

实测发现：

- 删除2009-07-27后，2009-07-28前收从98.35错成98.07；
- 14日窗口自然跨度从19天压成17天；
- 不完整日被删除后，rolling变成14个“保留日”；
- 最大回撤被低估约1.6个百分点。

正确流程变为：

```text
完整交易日历 + clean bars
→ 组件有效性
→ 固定eligible-session窗口
→ 最后应用交易tier mask
```

---

## 5.8 Halt minute集合验证

早期只做：

```text
expected bars = calendar bars - halt bars
```

这只比较数量。

问题是：

- halt期间可能有phantom bars；
- halt外也可能缺少同样数量的正常bars；
- 总数刚好对上，但分钟位置完全错误。

后来改为集合比较：

```text
required_minutes = scheduled_minutes - official_halt_minutes
missing_required = required_minutes - observed_minutes
present_during_halt = observed_minutes ∩ official_halt_minutes
```

并对重叠halt窗口先做集合并集，防止重复计数。

---

## 5.9 分红处理

处理：

- ex-date；
- cash amount；
- symbol/ticker/sym/s别名；
- dividend type；
- 完全重复事件去重；
- 同日不同金额默认报错；
- regular + special需要明确确认后才可求和。

分红文件不会直接改写分钟OHLC，只在策略和total-return benchmark中使用。

---

## 5.10 数据发布与可重复性

每次运行：

```text
staging临时目录
→ 生成全部文件和报告
→ 写_SUCCESS
→ 移动到runs/<run_id>
→ 原子更新latest.json
```

如果中途失败，`latest.json`不会指向不完整run。

同时记录：

- source SHA-256；
- script SHA-256；
- output/report SHA-256；
-完整CLI；
-配置；
-版本；
-日期范围；
-异常统计。

数据层最终通过18项测试。

---

# 6. 回测引擎为什么需要重构

早期公式：

```python
position.shift(1) * close.diff()
```

只能表达“上一行仓位乘本行价格变化”。

它无法回答：

- 信号何时产生；
- 订单何时提交；
- 原计划何时成交；
- halt前订单是否取消；
- 复牌时新订单是否吃到gap；
- 反手是1笔还是2笔；
- 最后一分钟能否开仓；
- 尾盘数据缺失时如何退出；
- commission和slippage按多少股收取。

因此建立状态机：

```text
signal
→ pending order
→ intended fill
→ actual fill
→ filled position
→ mark-to-market
→ EOD close/exit
→ costs
```

---

# 7. 三个回测 profile 的作用

## 7.1 `official_sample_compatible`

目的：复现作者notebook的主要约定，解释作者结果从何而来。

包含：

- 13个样本即可warm；
- 原日波动率窗口；
- vol缺失时4倍杠杆；
- `round()`股数；
- 无论文滑点；
- row-based rolling；
- shifted close / sample式成交。

它不是最真实版本，也不宣称逐行bit parity。

---

## 7.2 `paper_spec`

目的：按论文文字而不是sample bug实现。

包含：

- 严格14个eligible sessions；
- 论文band和VWAP规则；
- 分红调整；
- commission；
- 论文slippage；
- floor sizing；
- 论文式成交口径。

它回答：

> 如果按论文文字执行，历史结果如何？

---

## 7.3 `corrected_execution`

目的：判断现实中更诚实的可执行结果。

包含：

- next executable open成交；
- pending order；
- halt cancel/queue policy；
- 未成交订单不吃复牌gap；
- 已有仓位完整承担复牌gap；
- component validity；
- 真实反手股数；
- 独立佣金和滑点；
- calendarized统计。

它回答：

> 保留论文信号，但用更现实执行，策略还剩多少？

---

# 8. 引擎中发现并修复的主要问题

## 8.1 参数 sweep 静默失效

早期数据层预生成 `signal_valid`，其中烧入：

- 30分钟频率；
- 14日窗口；
- 使用VWAP；
- 波动率目标。

如果引擎设 `trade_freq=15`，实际仍只在30分钟决策，却不会报错。

修复后：

```text
config_validity()
```

根据当前Cfg从primitives重新计算。

预生成字段改名：

```text
signal_valid_default_config
```

只用于默认配置诊断。

---

## 8.2 Halt前未成交订单错误吃复牌gap

错误情况：

```text
09:34产生long信号
09:35开始halt
09:49复牌上涨
```

旧逻辑按“下一可得数据行”shift，导致该订单仿佛在halt前成交，并赚到复牌gap。

正确规则：

| 情况 | 是否承担复牌gap |
|---|---|
| halt前已经持仓 | 是 |
| halt前刚发信号、尚未成交 | 否 |
| halt期间信号 | 禁止 |
| 排队至复牌成交 | 成交后才开始PnL |

状态机因此增加订单到期minute和取消/排队policy。

---

## 8.3 普通分钟成交价格过于乐观

信号用到当前bar close和VWAP，只能在bar结束后确认。

现实corrected版本采用：

```text
bar t结束产生信号
→ bar t+1第一个可执行open成交
→ 从成交后开始计PnL
```

而不是假设按bar t close成交。

---

## 8.4 反手成本少算

仓位：

```text
0 → +1 → -1 → 0
```

实际交易单位：

```text
开多1
多翻空2
平空1
合计4
```

旧实现只按3次仓位变化收成本。

后来拆分：

- `fill_events`
- `trade_units`
- `shares_traded`

成本按真实quantity计算，并区分反手是一张2N订单还是两张N订单。

---

## 8.5 最后一分钟虚构交易

如果15:59产生信号，旧paper逻辑可能：

```text
15:59开仓
→ 同一价格立即EOD平仓
→ 收两次成本、没有收益
```

修复为最后计划bar不接受新entry/reversal，只处理已有仓位退出。

---

## 8.6 严格eligible-session rolling

早期某分钟缺失时，rolling会跳过该日并向前补更老日。

修复后使用完整scheduled grid：

- 缺失minute保留NaN；
- 仍占窗口槽位；
- 半日市下午没有eligible行；
- 14日中有一个无效观察，history validity就为False；
- 不向前寻找第15天补足。

---

## 8.7 无效收盘污染日波动率

尾盘缺失日的“最后可得价”不是真实close。

旧逻辑仍用它计算：

- 当日日收益；
- 次日日收益；
- 14日波动率；
- leverage。

后来引入 `daily_ret_valid`，无效收益为NaN并占窗口槽位。

---

## 8.8 `unknown_exit` 污染AUM

如果尾盘缺失且策略仍持仓，真实退出价未知。

旧版本虽然把该日return设为NaN，但临时PnL已经进入AUM，下一日仓位继续基于错误AUM。

后来增加：

- `terminate`
- `exclude_session_and_freeze_aum`
- `impute_last_observed`

默认使用严格policy，未知退出不再静默污染后续权益曲线。

---

## 8.9 `share_rounding` 配置未生效

作者sample使用 `round()`，论文版本使用 `floor()`。

曾经虽然Cfg中声明了 `share_rounding`，实际代码仍硬编码 `np.floor`。修补脚本又因为缩进不匹配静默失败。

结构性修复：

1. 所有补丁模式出现次数必须恰好为1，否则立即报错；
2. 增加Cfg字段使用审计测试；
3. 增加round与floor行为测试。

这次修复使official profile总收益从1713.4%变为1714.2%。

---

## 8.10 Sharpe_active是假象

曾报告calendar Sharpe约1.00、active Sharpe约1.28。

问题是active日只有约60%，却仍乘 `sqrt(252)` 年化，机械抬高数字。

按真实active频率调整后，1.28正好约等于1.00。

因此删除“Sharpe_active”，只保留：

- `Sharpe_calendar`
- active day非年化mean/std

---

## 8.11 Benchmark修复

旧benchmark存在：

- 使用尾盘无效日最后可得bar；
- 首个evaluation day没有前日anchor；
- `pct_change`可能自动填补；
- 没有真实分红时total=price。

修复后：

- invalid close变NaN；
- `pct_change(fill_method=None)`；
- 加首日前一有效close anchor；
- 报告missing close；
- strategy和benchmark日期严格对齐。

---

# 9. 当前固定基线

以下结果是：

> **零分红、零融资和借券成本下的 engine-mechanics baseline。**

| Profile | Total Return | CAGR | Vol | Sharpe | MDD | Trade Units | Shares Traded | Cost/Share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| official_sample_compatible | 1714.2% | 17.04% | 14.63% | 1.15 | -24.5% | 8,304 | 40,593,898 | 0.35¢ |
| paper_spec | 1632.9% | 16.76% | 14.29% | 1.16 | -26.8% | 8,236 | 39,581,998 | 0.45¢ |
| corrected_execution | 1053.7% | 14.21% | 14.30% | 1.01 | -30.3% | 8,236 | 30,345,204 | 0.85¢ |

## 如何理解差异

### Sample → Paper

收益下降主要来自：

- 13日改为严格14日；
- 日波动率窗口修复；
- 风险无效时不再默认4倍；
- 加入论文slippage；
- 更严格的数据validity。

### Paper → Corrected

收益进一步下降主要来自：

- 下一可执行open成交；
- 未成交订单不再吃价格gap；
- halt订单处理；
- 更现实的滑点；
- 实际成交股数和反手成本。

结论：

> 原始sample的高收益并不完全是虚假的，但包含明显的执行和实现乐观性。

---

# 10. 当前benchmark结果和限制

零分红口径：

| Profile | SPY Price CAGR | Strategy CAGR | Excess | Beta | Alpha | IR |
|---|---:|---:|---:|---:|---:|---:|
| official | 9.81% | 17.04% | +7.24pp | -0.072 | 17.65% | 0.22 |
| paper_spec | 9.66% | 16.76% | +7.10pp | -0.064 | 17.36% | 0.21 |
| corrected | 9.66% | 14.21% | +4.55pp | -0.064 | 15.13% | 0.13 |

当前 `dividends_used=False`，因此：

```text
SPY total return = SPY price return
```

真实SPY total return会更高，所以当前excess和IR被高估。正式结论必须等真实分红文件加入后重跑。

---

# 11. 测试和工程保障

当前已完成：

- 62项engine tests（含不可变data release、普通run等价加载及无分红run兼容）；
- 28项data tests。

测试覆盖包括：

- 14日warmup边界；
- 半日市；
- truncated session；
- interior gap；
- halt phantom bars；
- halt前已有仓位；
- halt前未成交订单；
- queued order；
- 反手quantity和成本；
- final-bar信号；
- strict rolling；
- invalid daily return；
- unknown exit；
- lagged fill；
- round/floor；
- benchmark anchor；
- absent full session；
- invalid configuration；
- Cfg字段使用审计。

这些测试的意义是防止未来“修一个问题又悄悄引入另一个问题”。

---

# 12. Evaluation spec 为什么要预注册

我们已经看过2024-05后的结果，因此不能称为完全 untouched OOS。

准确名称是：

> **post-publication evaluation period**

预注册文件冻结：

- 切点：2024-05-01；
- 三个profile；
- 三个tier；
- 有/无分红；
- slippage网格；
- 分解维度；
- 必须记录的hash；
- 决策规则。

核心决策：

```text
如果2024-05-01后gross edge/share约为0：
信号已消失，不再参数优化。

如果gross edge/share仍为正，但低于执行、融资和借券成本：
才研究执行、频率和容量。
```

这样避免看到结果后再移动切点或选择最有利成本。

---

# 13. 当前仍需完成的工作

## 13.1 真实分红

正式运行：

```text
with_dividends
ignore_dividends
```

headline必须使用with-dividends。

---

## 13.2 Trade/fill/round-trip ledger

目前daily汇总不足以完成全部分解。

需要保存：

### Signals

- 信号时间；
- 信号方向；
- band；
- VWAP；
- 是否被拒绝；
- 拒绝原因。

### Fills

- 原定和实际成交时间；
- 成交价格；
- 数量；
- commission；
- slippage；
- cancel原因。

### Round trips

- entry/exit；
- long/short；
- holding minutes；
- gross/net PnL；
- entry-time bucket；
- volatility regime。

---

## 13.3 Evaluation runner

当前spec还是文件，尚未完全变成代码强制执行。

需要唯一入口自动运行：

```text
profile × tier × dividend × cost × subperiod
```

并验证所有grid cell完整，发布不可覆盖的报告。

---

## 13.4 完整融资与借券账户

当前基础结构已加入rate字段，但最终需要分别累计：

- positive cash；
- borrowed cash；
- long notional；
- short notional；
- 持仓时间；
- halt期间经过的时间。

不能使用平均signed notional，因为同一天先多后空会互相抵消，却实际同时发生融资和借券成本。

---

## 13.5 独立daily benchmark

分钟源有少量无效尾盘。正式benchmark最好使用独立daily SPY raw close，并与同一分红文件结合。

---

## 13.6 Spec v2

正式看结果前还需冻结：

- evaluation end；
- post期是否重置AUM；
- economic headline是否为corrected × halt-aware；
- slippage grid如何应用；
- funding/borrow rate；
- volatility regime边界。

v2统计口径已决定为只报告点估计；bootstrap和HAC置信区间明确延期，
不作为本轮正式发布门槛，报告也不得暗示已提供统计置信区间。

---

# 14. 当前能得出的核心结论

## 14.1 策略不是纯bug

严格paper口径仍有约：

```text
CAGR 16.8%
Sharpe 1.16
```

说明信号并非完全由程序错误制造。

## 14.2 作者sample明显乐观

窗口、杠杆fallback、成本和成交假设都会抬高结果。

## 14.3 现实执行显著削弱收益

corrected结果约：

```text
CAGR 14.2%
Sharpe 1.01
MDD -30.3%
```

并且尚未加入完整：

- 分红benchmark；
- funding；
- borrow；
- market impact；
- 容量约束。

## 14.4 全样本收益不是最终问题

真正需要判断的是：

```text
2024-05-01之后
gross edge per traded share
是否高于
execution + funding + borrow cost per traded share
```

如果毛边际接近0，说明信号本身失效，参数优化只是在拟合已知结果。

---

# 15. 项目当前状态

## 基本冻结

- 时间戳和交易日历；
- 行级OHLCV校验；
- missing minute/session；
- 半日市和halt；
- component validity；
- 三个profile；
- strict rolling；
- order/fill状态机；
- reversal和成本；
- calendar Sharpe；
- unknown exit；
- benchmark基础对齐；
- 测试框架。

## 尚未冻结

- 真实分红；
- 完整融资time-integral；
- ledger；
- executable evaluation runner；
- 独立daily benchmark；
- evaluation spec v2；
- post-publication正式报告。

## 明确暂缓

- 参数优化；
- 因子扩展；
- 机器学习；
- Qlib；
- 实盘部署。

---

# 16. 最终研究方法

项目从最初的一份回测脚本，逐步升级为：

```text
交易所完整日历
→ 原始行情校验
→ 缺失与冲突诊断
→ 组件级有效性
→ 配置相关特征
→ 信号
→ 订单
→ 成交
→ 持仓和逐笔PnL
→ 佣金、滑点、融资和借券
→ 日历化统计
→ 冻结的论文公开后评估
```

每一轮优化都遵循同一标准：

1. 问题是否会改变信号、仓位、PnL或统计；
2. 是否可以构造最小案例复现；
3. 修复后是否有自动测试；
4. 是否记录数据、代码和配置hash；
5. 是否避免根据收益好坏选择实现。

只有在真实分红、完整成本和冻结的post-publication报告完成后，才能回答：

> **这是不是一套现在仍值得投入资金和研发资源的策略。**
