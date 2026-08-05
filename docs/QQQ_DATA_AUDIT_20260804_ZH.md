# QQQ 一分钟数据质量审计报告（首次本地全量运行，含股息）

- 审计日期:2026-08-04
- 数据 run:`data/candidates/qqq_v1_observed/runs/20260804T143705Z`(带股息重跑)
- 状态:探索性首跑审计,**不是**冻结 data-v1.0 候选。本报告如实记录每一类问题与处理。

## 运行溯源

| 项 | 值 |
|---|---|
| 原始文件 | `QQQ_1min_20260731.csv` |
| 原始 SHA-256 | `3203ecef8d95244d3edf495529b044f8c7916ca36bc9018764f351e8f7348057` |
| 原始行数 | 3,327,869 |
| 数据层脚本 | `prepare_spy_data.py`,SHA-256 `513721c20e4b44de1c8c28ab1ac09a076c1d1f3b086255e48a48c9d31fef2f4e`(与 HEAD `2214aed` 一致,但工作区 dirty) |
| Run ID | `20260804T143705Z`(53.2s) |
| 自检 | preflight 28/28 通过 |
| 交易所日历 | XNAS(Nasdaq),时区 America/New_York |
| 时间戳列 | `DateTime`(auto 选择,候选列仅 `DateTime`,空值率 1.0 即 0 空值) |
| 输入时区 | America/New_York(原始时间戳为美东,无时区标记) |
| bar 标签 | `start`(09:30 bar 表示 09:30:00–09:30:59) |
| 期望区间 | 2007-04-25 .. 2026-07-31 |
| 观测区间 | 2007-04-25 .. 2026-07-31(完全匹配) |
| 股息输入 | `data/reference/qqq_dividends_crossvalidated_20260804.csv`(81 个 ex-date) |
| 价格调整声明 | `none_raw_ohlc`(见第十一节的证据保留) |
| 缺失 bar 策略 | `diagnose_do_not_impute`(raw 不可变,不补值) |
| 环境锁 | numpy 2.1.3 / pandas 2.2.3 / pyarrow 25.0.0 / exchange-calendars 4.13.2,与 `requirements.lock` 一致 |

完整 CLI:
```
prepare_spy_data.py --input data/raw/QQQ_1min_20260731.csv --symbol QQQ --calendar XNAS
  --input-timezone America/New_York --expected-start 2007-04-25
  --expected-end 2026-07-31 --invalid-row-policy drop
  --output-dir data/candidates/qqq_v1_observed
  --dividends data/reference/qqq_dividends_crossvalidated_20260804.csv
  --preflight-self-test
```

## 一、数据规模与常规时段边界

| 量 | 值 |
|---|---:|
| 输入行 | 3,327,869 |
| 符号过滤剔除 | 0(单符号文件) |
| 不可解析时间戳 | 0 |
| 重复时间戳(去重前) | 0 |
| 越界(off-grid)行 | 0 |
| 常规时段(RTH)clean 行 | 1,881,986 |
| 扩展时段行(剔除) | 1,445,882(占 43.4%) |
| 常规时段交易日 | 4,848 |
| 会话覆盖率 | 0.99979(4,847/4,848 期望会话观测到) |

开盘对齐率 97.65%(4,735/4,848 会话以 minute 1 即 09:30 开盘),高于 95% 门槛。未达 100% 的原因是 2007 年大量会话缺 09:30 首 bar(见第六节)。

**时间戳列选择**:文件仅一列 `DateTime`,无歧义,无空值,无多列候选冲突。

## 二、非交易时段 bar -- 1,445,882 行剔除并留档

1,445,882 行(占输入 43.4%)落在 XNAS 常规时段(09:30–16:00 ET)之外,为盘前盘后成交分钟。管道按 `is_rth` 标记剔除,不进任何层级,但完整留档于 `reports/outside_rth_rows.csv`(取样 20 万行)。

剔除扩展时段是策略正确性的要求:日内动量信号与 VWAP/band 都定义在常规时段上,扩展时段成交会污染开盘锚与累积 VWAP。剔除后常规时段内 bar 1,881,986 行。

## 三、重复 / 冲突 / 非法 OHLC / off-grid

| 检查 | 结果 |
|---|---|
| 重复时间戳 | 0 |
| 冲突重复时间戳(同时间戳不同 OHLCV) | 0 |
| OHLCV 冲突 | 0 |
| 可选元数据冲突 | 0 |
| 厂商 VWAP 列冲突 | 0(无该列) |
| 越界(off-grid)行 | 0 |
| 无效 OHLC 行 | 1(删除) |

**无效 OHLC 唯一行**:`2008-03-11 14:21 ET`(minute 292):`open=42.14, high=42.12, low=42.10, close=42.12, volume=638,895`。`high(42.12) < open(42.14)` 2 美分,物理不可能(最高价不低于开盘价),volume 高达 64 万,确属厂商 bar 聚合错误(很可能 open 错记)。

**处理**:按 `--invalid-row-policy drop` 删除(占比 5e-7)。删除后该 session 缺 minute 292 -> `interior_missing=1` -> 评级 `interior_gap` -> 不进 paper_ready/halt_aware(见第七节)。这是验收门 "Unapproved invalid-row deletion = FAIL(1)" 的来源。升级为正式发布时应回到 `--invalid-row-policy error` 并人工裁决。

另有 2 行无效在扩展时段,随扩展时段一并排除。

## 四、会话覆盖总览(6 类评级)

管道按**分钟集合**(非计数)评级,4,848 个交易日分为:

| 评级 | 数量 | 进 paper_ready | 进 halt_aware | 进 exploratory |
|---|---:|:---:|:---:|:---:|
| complete | 4,700 | ✓ | ✓ | ✓ |
| truncated | 109 | ✗ | ✗ | ✓ |
| interior_gap | 33 | ✗ | ✗ | ✓ |
| halt_anomaly | 4 | ✗ | ✗ | ✗ |
| sparse | 1 | ✗ | ✗ | ✗ |
| absent | 1 | ✗ | ✗ | ✗ |

- **paper_ready = 4,700**:complete 且无无效行。
- **halt_aware = 4,700**:complete ∪ halt_adjusted 且无熔断内成交。QQQ 无 halt_adjusted 日(4 个熔断日全因零星成交变 halt_anomaly),故 halt_aware 与 paper_ready **完全同集**。
- **exploratory = 4,842**:排除 absent/sparse/halt_anomaly。
- **不进任何层级 = 6**:4 halt_anomaly + 1 sparse + 1 absent。

评级规则:
```
observed==0                          -> absent
unexpected_minutes>0 或熔断窗内有成交  -> halt_anomaly
缺分钟==0 且无无效行                  -> complete(或 halt_adjusted)
observed < 0.5*required              -> sparse
leading+trailing >= interior         -> truncated
否则                                  -> interior_gap
```

下文逐类展开。

## 五、整日缺失 session(absent)-- 1 个

`2007-06-01`:`observed_minutes=0 / missing_required=390`,评级 `absent`。XNAS 日历有该会话(QQQ 当日正常交易),但厂商文件里一整日 0 行,是**厂商数据缺口**。三个层级都不进(连 exploratory 都不进)。

验收门 "Expected boundaries complete = PASS" 是因为该日属"已知边界内、已分类"的缺席,不算边界外缺失(`boundary_sessions_missing: leading=0, trailing=0`)。`sessions_absent=1`,低于 `max_absent_sessions=20` 且 `max_consecutive_absent_sessions=1`,不触发发布阻断。

**处理**:不补、不插值,如实记缺失。建议找厂商或第二数据源核实补齐(单日缺口,影响有限)。

## 六、开盘截断(truncated)-- 109 个

缺口集中在会话**开头**(leading),尾盘(trailing)在 QQQ 上全为 0。典型形态:缺 09:30 开盘第一根 bar,从 09:31 才开始有成交。

**例:`2007-04-26`,leading_missing=1**(观测 389/390):

| minute | 时间 | open | close | volume |
|---|---|---|---|---|
| 1 | 09:30 | 缺失 | | |
| 2 | 09:31 | 46.42 | 46.44 | 1,192,967 |
| 3 | 09:32 | 46.44 | 46.41 | 514,711 |

**例:`2007-05-11`,leading_missing=12**(观测 378/390):09:30–09:41 全缺,09:42 才出现稀薄成交(vol 1.3 万),09:45 流动性才上来(vol 47 万)。

按年分布:

| 年 | 2007 | 2013 | 2014 | 2016 | 2017 | 2019 | 2020 | 2021 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| truncated | 96 | 3 | 1 | 1 | 4 | 2 | 1 | 1 |

**原因**:2007 年 QQQ(当时代号 QQQQ)开盘流动性稀疏,厂商在 09:30 开盘竞价那段经常无成交可聚合或未采集开盘分钟(109 个中 96 个在 2007)。其余零星散布在节假日前后低流动性日,多为缺单根首 bar。

**为什么只进 exploratory**:`move_open` 信号锚定当天第一根 bar 的开盘价,且 `sigma_open`/`dvol` 用 14 天滚动均值——一个缺 09:30 的天不仅当天 move_open 锚无效,还会污染之后 `sigma_window` 个会话在 minute 1 的滚动统计。但收盘价仍是好数据,可作下一日 previous-close,故进 exploratory。

## 七、盘中缺口(interior_gap)-- 33 个

开盘 09:30 与收盘 16:00 都在,但中间有一段或多段缺失。按年分布:2013(13,含宕机日)、2012(7)、2007(5)、2011(3)、2014(3)、2008/2010(各 1)。

### 例 1:`2013-08-22`,interior_missing=174 -- Nasdaq SIP 宕机(真实事件)

缺口集中在 12:21–13:33 ET(174 分钟,分多段),观测仅 216/390。10:45 前正常交易,午后 minute 244(13:33)恢复时首根仅 100 股。这是 2013-08-22 著名的 **Nasdaq SIP 总机故障**(全市场行情冻结约 3 小时),QQQ 挂牌在 Nasdaq,几乎无成交。174 分钟缺失是真实历史事件,非厂商掉数据。

### 例 2:`2008-03-11`,interior_missing=1 -- 删除无效行所致

minute 292(14:21)即第三节那行无效 OHLC 被 drop 后留下的 1 分钟盘中洞(14:20 -> 14:22)。

### 例 3:`2013-01-03`,interior_missing=14 -- 厂商 feed 中断

13:38–13:51 ET 连续 14 分钟**原始文件整段 0 行**(grep 确认)。缺口前 close 67.27、缺口后 close 67.27,跨 21 分钟 `gap_return` 仅 +0.015%——市场本身就安静,厂商 feed 在午后低波动时段掉了一段,无遗漏行情。无交易所事件。

### 例 4:`2012-05-09`,interior_missing=3 -- 厂商零星小洞

14:35–14:37 ET 连续 3 分钟**原始文件 0 行**。价格基本连续(64.5309 -> 64.53),跨缺口收益约 -0.14%(正常波动)。无交易所事件。

**原因分类**:
- 真实市场事件:2013-08-22 Nasdaq 宕机(174 分钟,占 33 个 interior_gap 缺分钟数的大头)。
- 厂商数据洞:零星 1–14 分钟中断(2008-03-11 是删行所致;其余为厂商采集/传输缺口)。

**为什么只进 exploratory**:盘中缺口意味着该会话的累积 VWAP 与 sigma 带在缺口处不连续,跨缺口收益(`gap_return`)跨越未观测分钟,不能等同连续 1 分钟收益。paper_ready 要求 390 分钟全在,任何 interior_gap 都不满足。但开盘/收盘都在,close 可用,故进 exploratory。

## 八、Halt(熔断)-- 4 天,全部 halt_anomaly

4 个 2020-03 L1 全市场熔断日(03-09/12/16/18),每个 14 分钟熔断窗口内**各有 1 笔成交**:

| 日期 | 熔断窗口(ET) | 零星成交分钟 | 成交量 |
|---|---|---|---:|
| 2020-03-09 | 09:35–09:49 | 09:35 | 68,251 |
| 2020-03-12 | 09:36–09:50 | 09:36 | 430,483 |
| 2020-03-16 | 09:31–09:44 | 09:31 | 8,719 |
| 2020-03-18 | 12:57–13:11 | 12:57 | 48,685 |

每笔都出现在熔断窗口的**第一分钟**(熔断触发那一刻)。L1 熔断是触发后即刻暂停,但触发阈值被击穿到正式暂停之间那几秒内已成交的单子,以及延迟/更正上报的成交,会被厂商聚合成一根分钟 bar,落在官方"禁止交易"窗口内。

**处理**:管道无法把"交易所说没交易"与"厂商说有 1 笔成交"调和一致 -> 保守判 `halt_anomaly` -> 三个层级(paper_ready / halt_aware / exploratory)**都不进**。这是验收门 "Halt minutes explained = FAIL(present_in_halt=4)" 的来源。`halt_validation`:total_halt_minutes=56,sessions_with_bars_present_during_halt=4,sessions_with_unexpected_minutes=0。

**与 SPY 的关键差异**:SPY 上这 4 个熔断日是 0 笔成交 -> `halt_adjusted` -> 进 halt_aware 不进 paper_ready,两个层级有差异。QQQ 多了这 1 笔 -> 两个层级都排除 -> **QQQ 上 halt_aware ≡ paper_ready**(均 4,700 天)。这是 QQQ 上"halt_aware"目前只是名义存在、实质等于 paper_ready 的原因。

建议找第二数据源核实这 4 笔的性质(延迟上报 vs 错误聚合)。

## 九、半日与首日(sparse)-- 1 个

`2007-04-25`(QQQ 样本首日):`observed_minutes=19 / missing_required=371`,15:41 才开始有数据,评级 `sparse`(observed < 0.5×required)。不进任何层级。这是数据起点的不完整日,属预热日,影响有限。

XNAS 半日市(7/3 前后、感恩节后、圣诞前夜)在日历中已按缩短会话处理,凡计划分钟齐全即 `complete`,QQQ 上无半日市被单独标记。

## 十、收益异常(只审计、不清洗)

引擎按"**已成交分钟间隔**"分类收益(从不按行相邻):`continuous_1min_return`(相邻可成交分钟)、`gap_return`(跨缺口)、`halt_reopen_return`(熔断后首个可成交分钟)。所有异常**只记录、不删除、不修改**(raw 不可变)。真正的防线是数据层级——任何带完整性问题的会话不进 paper_ready/halt_aware。

| 异常类 | 数量 | 说明 |
|---|---:|---|
| 极端连续 1-min(\|r\|>1%) | 58 | max -6.12%(2015-08-24 09:31 开盘跳水) |
| gap_return(跨缺口) | 49 | max \|r\| 0.61%,无极端(>1%) |
| halt_reopen_return | 0 | 4 熔断日因零星成交变 halt_anomaly,重开收益归入 gap |
| stale bar 序列 | 1,231 段(最长 3) | open=high=low=close,集中 2007–2012 |

### 极端连续 1-min 收益(58 行)年度分布

| 年 | 2008 | 2020 | 2025 | 2015 | 2010 | 2022 | 2007 | 2018 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 行数 | 20 | 10 | 9 | 6 | 5 | 4 | 2 | 2 |

全部落在已知高波动事件,且伴随巨量成交,**是真行情不是厂商错误**:

| 日期 | 分钟 | open | close | 收益 | 事件 |
|---|---|---|---|---:|---|
| 2015-08-24 | 09:31 | 94.22 | 88.46 | -6.12% | 中国贬值开盘跳水 |
| 2010-05-06 | 14:51 | 45.25 | 46.98 | +3.80% | 闪电崩盘 |
| 2015-08-24 | 09:33 | 89.13 | 92.25 | +3.28% | 同上反弹 |
| 2025-04-09 | 13:20 | 422.70 | 434.33 | +2.75% | 关税缓和反弹 |
| 2020-03-03 | 10:01 | 214.14 | 218.83 | +2.19% | COVID 波动 |

### gap_return(49 行)

跨数据缺口的收益,最大 \|r\| 仅 0.61%(2020-03-12 熔断重开),无极端(>1%)。按年:2013(22,含宕机日缺口)、2012(8)、2007(7)。`extreme_gap_return_rows=0`。

### stale bar 序列(1,231 段)

`open=high=low=close` 的平价 bar,最长连续 3 根。按年:2010(120)、2007(120)、2011(81)、2012(79)、2009(49)、2008(26),集中 2007–2012 早期低流动性时段。2013 后骤降至个位数。这些是真实无成交或单笔成交的冷分钟,非错误。

## 十一、分红 -- 81 个 ex-date 干净;raw 价格经第三方验证为未调整(管道 flag 为误报)

### 股息序列(交叉验证三源)

`data/reference/qqq_dividends_crossvalidated_20260804.csv`,81 个 ex-date(2007-06-15 .. 2026-06-22),季度分红。无单一官方源能以全精度覆盖整个样本,故三源合并:

1. **Invesco(发行方)**官方产品页 Distributions 表:5 位精确,5 个最近 ex-date(2025-06-23..2026-06-22),作发行方真值。该页 "View since inception" 控件因页面遮挡层无法在自动化中展开,发行方表无法整段抓取。
2. **Nasdaq(上市交易所)**官方 API:5 位精确,2012-06-15..2026-06-22(52 行;缺 2012-09-21 / 2012-12-21 两季)。
3. **Yahoo Finance**:3 位精度,2007-06-15..2026-06-22 全覆盖(24 行用于 2007-2012 及 Nasdaq 缺的两季)。

交叉验证:重叠期三源 ex-date 完全一致;Nasdaq 与 Invesco 在最近 5 行 5 位精确相等;Yahoo 与另两源在 3 位精度一致(仅精度差)。管道清洗:81 行入,0 冲突,0 ex-date 落在非交易日,0 重复,产出 `spy_dividends_clean.csv`。

### raw 价格调整证据 -- 管道 flag 为 false,但**第三方交叉验证确认为 raw 未调整(管道 flag 是误报)**

管道的 `dividend_adjustment_evidence` 给出 `evidence_supports_raw_prices = false`:

| 量 | 值 | 含义 |
|---|---:|---|
| 非 ex-date 隔夜 gap 均值 | 0.042% | 一般隔夜漂移(QQQ 样本期夜间偏涨) |
| ex-date 隔夜 gap 均值(raw) | 0.0066% | ex-date 开盘相对前收的 gap |
| ex-date gap + 加回分红 | 0.1985% | 若价格已调整、把分红加回去 |
| 残差(raw) | -0.0353% | ex-date raw gap 与非 ex-date 之差 |
| 残差(加回分红) | +0.1566% | 加回分红后 ex-date gap 反而更大 |
| `evidence_supports_raw_prices` | **false** | 管道 flag(见下,为误报) |

管道注记明示"evidence, not proof"。该测试逻辑:若价格 raw 未调整,ex-date 开盘应下跌约分红额,把分红加回去应使 ex-date gap 回到基线;若已调整,raw gap 已近基线、加回分红反而偏离。管道判定 raw gap(0.0066%)距基线(0.042%)比加回分红版(0.1985%)更近 -> flag=false(疑已调整)。

**第三方交叉验证(Yahoo Finance,2026-08-05)推翻此 flag,确认厂商价为 raw 未调整**:

1. **价位漂移(决定性)**:逐年 `vendor_close − yahoo_raw_close` 均值全样本仅 **0.013 美元**(<0.01%,差异来自收盘时点:厂商=15:59 最后一根 1-min bar,Yahoo=16:00 官方收盘);而 `vendor_close − yahoo_adj_close` 均值 **6.82 美元**,随累计分红增大(2007 ~$7、2015 ~$8.4)并向近期收敛(2026 ~$0.9),正是"向后调整"模式。**厂商价 = Yahoo raw,≠ Yahoo adjusted。**

2. **逐除权日开盘跳空**:全部 81 个 ex-date,厂商开盘跳空与 Yahoo raw 开盘跳空相关 **0.9958**、平均差 **0.0038%**,逐日同步。

3. **误报机制**:把管道同一指标施于 Yahoo raw 自身,也得 `supports_raw=False`--因为 QQQ 季度分红率仅 ~0.13%,远小于隔夜开盘跳空波动,"距基线距离"比较在弱信号下失效。详见 `experiments/qqq_with_dividends_v1/crosscheck_raw_vs_adjusted/SUMMARY.md`。

**对策略的影响(修正)**:厂商价为 raw 未调整,故引擎 `prev_close_adj = prev_close − dividend` **正确**调整 raw 前收(无二次调整),基准全收益 `(close + dividend)/prev_close` **正确**计息(无重复计息)。**带息 QQQ run 不受此 flag 影响,结果有效。** 无息首跑(价格基准)亦不受影响。原"疑已调整"保留据此撤销。

## 十二、Vendor 拼接(format regime)-- 单一 regime,无拼接缝

`regime_detection.detected = false`("no year-over-year step in decimal granularity")。整个样本单一 regime:

| 指标 | 值 |
|---|---:|
| 子分位收盘价占比(sub-penny close) | 23.02% |
| 子分位开盘价占比 | 19.31% |
| 零量 bar 占比 | 0.00% |
| stale bar 占比 | 0.066% |
| 极端 1-min 收益(\|r\|>1%) | 58 |
| 最大 \|1-min 收益\| | 6.12% |
| 会话中位成交额 | 32,162,967 股 |

无 regime_seams(无年度精度阶梯),无 vendor 拼接缝。子分位占比 ~20-23% 反映 Nasdaq 成交价的子分位报价特征(正常)。零量 bar 0%(每根常规 bar 都有成交)。

## 十三、验收门汇总

| Gate | 状态 | 证据 |
|---|---|---|
| 未解释 OHLCV 冲突 | PASS | 0 |
| 越界行 | PASS | 0 |
| 未批准无效行删除 | **FAIL** | 1(2008-03-11 14:21,drop) |
| 期望边界完整 | PASS | known=True, leading=0, trailing=0 |
| 每个非 paper-ready 会话已分类 | PASS | 148 个非 paper-ready 会话全分类 |
| Halt 分钟已解释 | **FAIL** | unexpected=0, present_in_halt=4 |
| 股息文件已验证 | PASS | 81 ex-date,0 冲突 |
| 数据自检 | PASS | 28/28 |
| 脚本匹配干净 Git HEAD | **FAIL** | commit=2214aed, dirty=True, matches_head=True |
| 确定性重跑哈希 | PENDING | 需第二次 run 比对 |

3 个 FAIL 均为已知且已披露:1 行无效 OHLC 删除(Q1)、4 个熔断日零星成交(Q6)、工作区 dirty(脚本 SHA 与 HEAD 一致,仅工作区未提交)。这些不阻断探索性发布,但**升级为正式发布前必须解决**(invalid-row-policy 回 error 并人工裁决、核实 4 笔熔断内成交、干净 git HEAD、确定性重跑哈希比对)。

## 十四、与 SPY 数据审计的关键差异

| 维度 | SPY | QQQ |
|---|---|---|
| 样本起点 | 2008-01-22 | 2007-04-25(更早 9 个月) |
| 交易所日历 | XNYS(纽交所) | XNAS(Nasdaq) |
| 常规时段 bar | ~1.8M | 1,881,986 |
| 扩展时段占比 | 较低 | 43.4%(更高) |
| 重复/冲突/off-grid | 0 | 0(同) |
| 无效 OHLC | 0 | 1(2008-03-11) |
| 整日缺失 | 0 | 1(2007-06-01) |
| 熔断日 | 4,均 halt_adjusted(0 成交) | 4,均 halt_anomaly(各 1 笔成交) |
| halt_aware vs paper_ready | 有差异(熔断日进 halt_aware) | **完全同集**(熔断日两层级都排除) |
| 极端 1-min 收益 | 56 | 58(分布相似,均集中在已知事件) |
| stale bar | 极少 | 1,231 段(集中 2007-2012) |
| 股息源 | State Street 发行方单源,精确 | Invesco+Nasdaq+Yahoo 三源交叉验证,2007-2012 仅 3 位精度 |
| raw 价格调整证据 | 支持 raw(evidence=true) | 管道 flag=false,但 Yahoo 第三方验证确认 raw(管道 flag 为低分红率下的误报) |
| 长盘中缺口 | 3 个 interior gap | 33 个(含 2013-08-22 Nasdaq 宕机 174 分钟) |
| Vendor 拼接 | 2016-01-04 两段 | 无(单一 regime) |

## 十五、复现与后续

### 复现
```
python prepare_spy_data.py --input data/raw/QQQ_1min_20260731.csv --symbol QQQ \
  --calendar XNAS --input-timezone America/New_York \
  --expected-start 2007-04-25 --expected-end 2026-07-31 \
  --invalid-row-policy drop --output-dir data/candidates/qqq_v1_observed \
  --dividends data/reference/qqq_dividends_crossvalidated_20260804.csv \
  --preflight-self-test
```
审计产物:`<run_dir>/audit_summary.md`、`<run_dir>/reports/*.csv`、`<run_dir>/reports/data_manifest.json`。

### 升级为正式 QQQ 发布前的待办(阻断项)
1. ~~核实厂商分红调整策略~~ **已由 Yahoo 第三方交叉验证确认厂商价为 raw 未调整**(第十一节):价位逐年吻合至美元级、81 个除权日开盘跳空相关 0.996。管道 `evidence_supports_raw_prices=false` 是低分红率下的已知弱测试误报,不构成调整证据。带息 run 无二次调整问题。若仍需 belt-and-suspenders,可向厂商直接确认。
2. `--invalid-row-policy` 回 `error` 并人工裁决 2008-03-11 14:21 那一行(找第二数据源核实 open)。
3. 核实 4 个熔断日的零星成交(第八节):延迟上报 vs 错误聚合。
4. 核实 2007-06-01 整日缺失(第五节):找厂商或第二数据源补齐。
5. 取得 2007-2012 股息的发行方(Invesco)精确值(当前为 Yahoo 3 位精度)。
6. 扩展融资利率释放至 2026-07-31(当前无覆盖,现金收益为 0)。
7. 干净 git HEAD + 确定性重跑哈希比对(PENDING)。
8. **先冻结 QQQ 自己的评估 spec,再谈任何参数调整**——post-2024-05-01 是评价期,不是未触碰 OOS,不得在其上拟合参数。

### 已知风险(非阻断)
- 带息 QQQ 结果:厂商价经 Yahoo 第三方验证为 raw 未调整(第十一节),引擎股息调整与全收益基准均正确,无二次调整/重复计息问题。
- 无融资利率:现金收益为 0,组合 CAGR = 纯交易 CAGR(对比 SPY 时注意 SPY 含现金 carry)。
- 基准为同分钟文件 QQQ 全收益(非独立日频源),与 SPY 的独立日频基准口径不同,横向比较时注意。
- 未做市场冲击/容量、排队成交模型(与 SPY 冻结版相同的待办)。
- 未做统计不确定性的正式冻结(已跑探索性 HAC + bootstrap,见 `experiments/qqq_with_dividends_v1/uncertainty_results/`,但未冻结为 spec 加编)。

### 产出层级(本 run)
| 文件 | 内容 |
|---|---|
| `spy_1min_clean.parquet` | 全常规时段 bar(1,881,986 行,含所有评级会话) |
| `spy_1min_paper_ready.parquet` | 4,700 个 complete 会话 |
| `spy_1min_halt_aware.parquet` | 同 paper_ready(QQQ 上无 halt_adjusted) |
| `spy_1min_exploratory.parquet` | 4,842 会话(排除 absent/sparse/halt_anomaly) |
| `spy_dividends_clean.csv` | 81 个 ex-date 清洗后股息 |
| `feature_validity_minute.parquet` / `feature_validity_session.parquet` | 引擎用特征/会话有效性掩码 |
| `reports/*.csv` | 各类审计明细(无效行、缺口、极端收益、stale、halt 等) |
| `audit_summary.md` | 验收门与摘要 |
| `reports/data_manifest.json` | 完整溯源与统计 |
