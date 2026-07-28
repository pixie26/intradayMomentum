# Intraday Momentum 数据清洗 v3 Review

## 结论

v3 可以作为数据清洗层的正式基线。

本轮确认的主要修复均成立，整体架构已经从普通清洗脚本升级为可审计、可重复、可发布的数据处理流程。以下部分可以保留：

- 时间戳显式化及合理性检查
- XNYS 交易日历、半日市和 halt minute-set 验证
- session tier 分层
- 严格的 14 个历史 eligible session 统计
- numeric-string epoch 处理
- 分红字段识别和重复事件控制
- staging → run directory → `_SUCCESS` → `latest.json` 的原子发布流程
- 完整 manifest、文件 hash 和异常报告
- `vendor_bar_vwap` 命名及数据源格式变化提示

不过，进入策略优化之前，仍有两个策略级问题和若干工程问题需要修正。

---

## 一、最重要的剩余问题：不能在 `paper_ready.parquet` 上重新计算特征

不建议采用以下流程：

```text
latest.json
→ paper_ready.parquet
→ im_engine 计算全部特征
→ 回测
```

原因是：先删除不完整 session，再在过滤后的数据上计算历史特征，会改变论文策略定义。

### 1. Previous close 可能取错

假设某个交易日因为开盘截断而被排除，但当日收盘数据仍然有效。

次日策略本应使用：

```text
前一个交易日的有效收盘价
```

如果引擎直接在 `paper_ready` 上执行：

```python
daily_close.shift(1)
```

则次日可能取到两个交易日前的收盘价。

这会直接改变：

- upper band
- lower band
- 除息日 previous close adjustment
- 次日开盘相对位置

**整个 session 不可交易，不代表该 session 的所有数据成分都不可用于后续特征。**

### 2. 日波动率窗口会被压缩

论文要求的是：

```text
此前 14 个交易日
```

如果提前删除坏 session，则 rolling window 实际变成：

```text
此前 14 个被保留的完整交易日
```

这两者可能跨越不同的自然时间范围，并产生不同的 leverage。

### 3. `sigma_open` 会变成“此前 14 个有效观察”

严格定义应是：

```text
此前 14 个该分钟原本应存在的 eligible exchange sessions
```

提前过滤 session 后，逻辑容易静默变成：

```text
向前寻找 14 个仍然存在的有效观察
```

这会改变论文中的固定交易日窗口。

### 4. Truncated open 会污染整天的 `move_open`

如果某个 session 第一根 bar 在 11:15：

```text
open_day = 11:15 价格
```

那么当天所有：

```python
abs(close / open_day - 1)
```

都以错误的开盘锚点计算。

因此，不仅缺失的早盘 minute 无效，**当天所有 minute 的 `move_open` observation 都无效**。

当前 minute coverage 仅根据某个 minute bar 是否存在判断其可用性，无法表达这一点。

---

## 二、建议的数据—引擎接口

策略引擎应读取：

```text
spy_1min_clean.parquet
session_quality.csv
halt_minutes.csv
spy_dividends_clean.csv
```

先在完整交易日历上计算各组件的有效性，再决定哪些 minute 可产生信号、哪些 session 可交易。

建议新增：

```text
feature_validity.parquet
```

至少包含以下字段：

```text
session_date
minute_of_session
open_valid
close_valid
move_open_observation_valid
vwap_valid
sigma_history_valid
daily_vol_history_valid
signal_valid
execution_bar_valid
is_halt_minute
is_scheduled_decision_minute
is_executable_minute
```

### `open_valid`

当天首个计划交易 minute 存在且可信。

### `close_valid`

当天最后一个计划交易 minute 存在且可信。

一个 leading-truncated session 可能是：

```text
open_valid  = False
close_valid = True
```

该 session 不能贡献 `move_open`，但其收盘价仍可能用于次日 previous close。

### `move_open_observation_valid`

建议定义为：

```python
move_open_observation_valid = (
    open_valid
    & current_minute_bar_present
    & current_price_valid
)
```

如果 session 开盘无效，则该 session 所有 minute 的 `move_open` observation 都应无效。

### `vwap_valid`

应要求：

```text
从开盘到当前 minute 的全部应交易分钟有效
```

官方 halt minute 除外，因为交易所本来没有正常连续交易。

如果出现 interior gap：

```text
缺口之前：vwap_valid = True
缺口之后：vwap_valid = False
```

### `sigma_history_valid`

主结果建议采用：

```text
此前 14 个 eligible exchange sessions
在该 minute 的 move_open observation 均有效
```

可另外研究敏感性版本：

```text
向前寻找此前 14 个有效 observations
```

但必须显式命名，不能通过提前过滤数据静默采用。

---

## 三、Halt-aware PnL 的正确处理

同意以下处理：

- halt 内不生成新信号
- halt 内不下单
- halt 内不调整仓位
- 落在 halt 中的 30 分钟 decision bucket 直接跳过
- vendor 提供的 phantom/stale halt bar 不作为可执行价格

但不同意：

```text
跨 halt 不计 PnL
```

如果这意味着忽略 halt 前最后成交价到复牌后第一成交价之间的价格变化，会系统性低估尾部风险。

正确逻辑应是：

```text
halt 前仓位冻结
halt 期间不交易
复牌时将价格跳变一次性计入现有仓位 PnL
```

例如：

```text
halt 前价格 = 280
持有 100 股多仓
复牌价格 = 275

PnL = 100 × (275 - 280) = -500
```

halt 期间没有逐分钟 mark，但复牌 gap 必须完整反映。

因此更准确的描述是：

> Halt 期间不生成虚构的逐分钟收益；仓位保持冻结，跨 halt 的价格跳变在复牌时完整计入。

---

## 四、不同 tier 必须分别报告完整指标

建议继续保留：

- `paper_ready`
- `halt_aware`
- `exploratory`

但不建议把不同权益曲线的指标拼成一组，例如：

```text
CAGR / Sharpe 使用 paper_ready
MDD / worst day / skew 使用 halt_aware
```

这样会产生没有严格意义的混合指标，例如：

```text
Calmar = CAGR_A / MDD_B
```

正确做法是每个 tier 分别报告完整指标：

| Tier | Total Return / CAGR | Sharpe | MDD | Worst Day | Skew | Turnover |
|---|---:|---:|---:|---:|---:|---:|
| paper_ready | 独立报告 | 独立报告 | 独立报告 | 独立报告 | 独立报告 | 独立报告 |
| halt_aware | 独立报告 | 独立报告 | 独立报告 | 独立报告 | 独立报告 | 独立报告 |
| exploratory | 独立报告 | 独立报告 | 独立报告 | 独立报告 | 独立报告 | 独立报告 |

建议定位：

- `paper_ready`：官方 sample 兼容性和完整数据基准
- `halt_aware`：完成 halt-aware 引擎后，作为主要经济结果
- `exploratory`：数据缺口敏感性分析

---

## 五、Truncated 与 interior gap 的风险排序

同意：

```text
truncated open 通常比少量 interior gap 更严重
```

原因是 truncated open 会令当天整个 `move_open` 参考系失效，并污染后续 rolling history。

不过，不建议把 interior gap 的 VWAP 偏差简单描述为：

```text
5 / 390
```

VWAP 是成交量加权，而不是分钟等权。

误差取决于：

```text
缺失区间成交量占比
×
缺失区间价格与已观察 VWAP 的差异
```

如果缺口发生在：

- 开盘
- 宏观数据发布
- FOMC
- 大幅跳价
- halt 前后
- 高成交量时段

即使只有几分钟，也可能改变 VWAP 阈值和最终信号。

建议风险分级：

### 最高严重度

- leading truncated
- trailing truncated
- 错误开盘锚点
- 错误收盘锚点

### 中高严重度

- interior gap 后的 VWAP
- gap 跨越期间的持仓 PnL
- decision minute 附近缺失

### 局部严重度

- 单个孤立缺口
- 影响大小取决于 minute、volume 和价格变化

当前将上述 session 全部排除在 `paper_ready` 外是合理的。

---

## 六、Manifest 内路径应改为相对路径

当前 staging 目录最终会被移动为：

```text
runs/<run_id>/
```

如果 manifest 在 staging 阶段写入绝对或 staging-relative path，例如：

```text
data/processed/.tmp_<run_id>/spy_1min_paper_ready.parquet
```

目录移动后，该路径会失效。

`latest.json` 可能重新拼接了 final path，但 manifest 内以下字段仍可能指向不存在的位置：

```text
outputs.*.path
reports.dir
dividends.output
```

建议 manifest 只保存相对路径：

```json
{
  "outputs": {
    "paper_ready": {
      "path": "spy_1min_paper_ready.parquet"
    }
  },
  "reports": {
    "dir": "reports"
  },
  "dividends": {
    "output": "spy_dividends_clean.csv"
  }
}
```

消费者统一通过：

```python
run_dir / relative_path
```

读取。

这样也方便：

- 复制整个项目
- 压缩归档
- 更换机器
- CI 运行
- GitHub release
- 云端存储

---

## 七、README 与运行结果应自动生成

当前报告中 session 数量存在一轮相差 1 的情况，例如：

```text
消息：4635 / 4639 / 4645
README：4634 / 4638 / 4644
```

虽然收益指标相同，但说明可能混用了：

- data sessions
- warm-up 后有效 sessions
- 有收益记录的 sessions
- 实际进入策略的 sessions

建议结果表完全由脚本自动生成，并写入：

```text
data_run_id
source_sha256
script_sha256
backtest_commit
backtest_config_hash
first_data_session
last_data_session
number_of_data_sessions
first_return_session
last_return_session
number_of_return_observations
number_of_trade_sessions
warmup_sessions
```

README 不应手工维护回测数字。

---

## 八、自测仍需补强

### Test：14-session warm boundary

不要只检查第一天为 False。

应显式断言：

```text
第 14 个 session：
prior = 13
band_warm = False

第 15 个 session：
prior = 14
band_warm = True
```

### Test：半日市之后的下午 bucket

除了验证：

```text
半日市 minute > 210 不输出
```

还应验证：

```text
半日市之后的完整交易日
minute390 的 prior count 保持 14
```

### Test：原子发布失败路径

当前如果测试仅在 timezone validation 阶段失败，只能证明早期失败不污染输出。

建议进一步注入失败：

- reports 写完后
- Parquet 写入后
- staging 完成但 move 前
- move 完成但 latest 更新前
- latest 临时文件写完但 replace 前

准确的工程保证应表述为：

> `latest.json` 不会指向未完成 run。

不必强求任何失败都完全不留下 artefact。移动完成后 latest 更新失败，留下一个带 `_SUCCESS` 的孤立 run directory 并不危险，只需允许后续 garbage collection。

---

## 九、Halt 字段应写回输出 Parquet

当 vendor 在 halt 期间保留：

- 零成交量 bar
- carry-forward bar
- phantom bar
- 聚合 bar

引擎必须知道这些 bar 不可用于：

- 信号生成
- 下单
- fill price
- bar VWAP
- decision bucket

建议所有输出都保留：

```text
is_halt_minute
is_scheduled_minute
is_scheduled_decision_minute
is_executable_minute
```

其中：

```text
is_executable_minute = scheduled
                       and not halt
                       and price_valid
                       and bar_present
```

---

## 十、分红 `sum` 仍应增加确认条件

如果使用：

```text
--dividend-duplicate-policy sum
```

当前逻辑不应对任何同日多条记录直接求和。

建议要求：

- 存在 `dividend_type`
- 类型组合符合允许列表，例如 regular + special
- 或额外显式传入：

```text
--confirm-dividend-sum
```

否则同一天不同金额应默认报错。

---

## 十一、Zero-volume / stale run 不应跨 session 连续

若 run detection 仅使用：

```python
(mask != mask.shift()).cumsum()
```

则前一交易日最后一分钟和下一交易日第一分钟都满足 mask 时，可能被识别成同一个连续 run。

应在以下任一条件发生时重置：

```text
session_date 变化
minute_of_session 不连续
mask 状态变化
```

建议 grouping key 类似：

```python
new_run = (
    mask.ne(mask.shift())
    | session_date.ne(session_date.shift())
    | minute_of_session.ne(minute_of_session.shift() + 1)
)
run_id = new_run.cumsum()
```

真实数据当前最长 run 为 1，因此不会改变现有结论，但代码语义应修正。

---

## 十二、建议的下一阶段

数据清洗层主体可以冻结，不建议继续无限扩展。

进入策略优化前，只需完成以下四项：

1. 建立 component-level `feature_validity`
2. 引擎从 `clean + diagnostics` 计算特征，最后才应用交易 mask
3. 实现 halt 期间冻结仓位、不交易，但复牌 gap 完整计入 PnL
4. 修复 manifest 相对路径，并让 README 结果表自动生成

完成后，再重构 `im_engine`，同时维护两个口径：

```text
paper_compatible
corrected_methodology
```

### `paper_compatible`

尽可能对齐作者 sample，便于 parity 检查。

### `corrected_methodology`

修正：

- rolling window
- execution timestamp
- halt handling
- transaction cost
- data validity
- benchmark
- performance statistics

两套结果并行保留，可以区分：

```text
与作者结果的差异
```

究竟来自：

- 数据源
- 数据清洗
- 代码实现
- 论文与 sample 的口径差异
- 合理的方法论修正

---

## 最终判断

v3 已经足够好，可以作为正式数据处理基线。

剩余最关键的原则是：

> 不要通过提前删除 session 来简化特征计算。

正确顺序应是：

```text
完整交易日历
→ 清洗后的分钟数据
→ component-level validity
→ 严格历史特征
→ signal validity
→ execution validity
→ session/tier trade mask
→ 回测
```

数据完整性、特征有效性和可交易性是三个不同层次，不应合并成一个 `paper_ready` 文件后再重新计算全部逻辑。
