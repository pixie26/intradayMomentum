# data-v1.0 冻结验收（2026-07-30）

## 结论

**APPROVED：在正式记录样本重定界后，data-v1.0 可按冻结流程发布。**

2026-07-30 正式决定把研究起点固定为 `2008-01-22`，即原始源中首个
观察到且完整的 XNYS session。该选择由数据可得性决定，不依据策略结果；
完整依据见 `DATA_V1_START_DATE_DECISION_ZH.md`。因此此前
`expected_start=2008-01-01` 暴露的 13 个前置缺失 session 不再属于
data-v1.0 的声明区间，也不被填充或伪造。

审计同时发现旧分红输入在 `2021-12-17` 与官方金额不一致。该问题已
按“不覆盖旧输入”的原则修正：新增 State Street 六位精度输入、metadata
和可复现提取脚本，并已完成 observed-range 候选重跑。

此外，2016-01-04 vendor 拼接点存在约 194 倍的日成交量单位/来源
跃迁。它不直接改变按单日成交量归一化的累计 VWAP，但会影响跨期成交量、
容量和冲击成本研究，冻结说明中必须明确披露并限制用途。

## 候选运行

为避免把已知缺少的头部数据伪装成完整样本，本次只对实际观察区间
`2008-01-22` 至 `2026-07-09` 做了候选运行：

- `data/candidates/data_v5_observed/runs/20260729T164157Z`
- `data/candidates/data_v5_observed/runs/20260729T164316Z`
- `data/candidates/data_v5_observed/runs/20260729T170513Z`
- `data/candidates/data_v5_observed_official_div/runs/20260729T170943Z`

前两个 run 用于逐字节可复现性比较；第三个 run 验证了零行 audit CSV
也会以带表头文件发布；第四个 run 使用官方六位精度分红输入。所有目录
都是验收候选，不是正式数据发布。

原始分钟文件 SHA-256：

`dedcc3cd460dfdd4fd90495f197868519b513a36e0d0723fc12ee47858714218`

## 冻结门槛

| 门槛 | 状态 | 实际证据 |
|---|---:|---|
| 未解释 OHLCV 冲突为 0 | PASS | 0 个 OHLCV conflict；0 个 optional metadata conflict |
| off-grid row 为 0 | PASS | 0 |
| 未经批准删除的 invalid OHLC row 为 0 | PASS | 0 |
| expected 起止区间完整 | PASS | expected/observed 均为 2008-01-22 至 2026-07-09；边界缺失 0 |
| 每个非 paper-ready session 有明确原因 | PASS | 10 个 session，逐日列于下文 |
| halt 分钟全部可解释 | PASS | 4 个 session、56 分钟；halt 内 bar present 为 0 |
| 分红冲突及金额逐日解决 | PASS | 新官方输入 74 个 ex-date/金额逐笔一致；无同日重复 |
| 全部 data tests 通过 | PASS | `python prepare_spy_data.py --self-test`：28/28 |
| 同输入、同环境核心输出 hash 一致 | PASS | 两次运行的 15 个核心产物全部一致 |
| source 与准备冻结的 Git 版本一致 | PASS | 正式发布器只接受记录同一 clean Git HEAD 的两次 run manifest |

历史边界探测曾识别出的 13 个、现已明确排除在 data-v1.0 声明区间之外的
XNYS session：

`2008-01-02`、`2008-01-03`、`2008-01-04`、`2008-01-07`、
`2008-01-08`、`2008-01-09`、`2008-01-10`、`2008-01-11`、
`2008-01-14`、`2008-01-15`、`2008-01-16`、`2008-01-17`、
`2008-01-18`。

旧 `expected_start=2008-01-01` 契约探测按预期失败且没有发布 run；
该失败审计作为重定界证据保存在：

`data/candidates/data_v1_contract_probe/failed_audits/20260729T171110Z`

## 实际产物审计

输入共 1,813,340 行。清洁 RTH 数据为 1,804,283 行；另有 9,057 行
位于 RTH 外并被明确报告。没有坏 timestamp、重复 timestamp、off-grid
row 或 invalid OHLC row 删除。

4,645 个观察 session 的质量分布：

- `complete`: 4,635
- `halt_adjusted`: 4
- `interior_gap`: 3
- `truncated`: 3

非 `paper_ready` session：

| session | 分类 | 明确原因 |
|---|---|---|
| 2009-07-27 | truncated | 开头缺 105 分钟 |
| 2013-12-23 | truncated | 开头缺 38 分钟 |
| 2016-02-02 | interior_gap | 中间缺 9 分钟 |
| 2019-08-12 | truncated | 中间缺 2 分钟、尾部缺 28 分钟，session close 无效 |
| 2020-03-09 | halt_adjusted | 14 个熔断分钟 |
| 2020-03-12 | halt_adjusted | 14 个熔断分钟 |
| 2020-03-16 | halt_adjusted | 14 个熔断分钟 |
| 2020-03-18 | halt_adjusted | 14 个熔断分钟 |
| 2021-05-05 | interior_gap | 中间缺 5 分钟 |
| 2023-06-05 | interior_gap | 中间缺 4 分钟 |

实际只有 `2019-08-12` 的当日 close 无效；`2019-08-13` 是因为上一
session close 无效而使 `prev_close_valid=False`。这两种状态不能混称为
“两个无效收盘日”。

## 异常收益

异常报告已经按语义拆分：

- 真正连续一分钟极端收益：56 行，分布在 26 个 session；
- 普通跨缺口收益：4 行，没有超过 1%；
- halt reopen 收益：4 行，没有超过 1%；
- 最长 stale bar run：1 分钟。

56 个连续一分钟极端 bar 全部落在 Yahoo 独立日线的当日 high-low
范围内（允许 0.02 美元容差），没有日线范围不可能解释的坏 print。
这些日期集中在金融危机、flash crash、疫情及 2025-04 波动期。该检查
支持“真实行情/事件波动”的解释，但不能替代第二套独立分钟源的逐 bar
核对。

## 独立 daily close

Yahoo 日线共提供 4,657 行。`2019-08-12` 的独立日收盘为
`288.070007`，而分钟源最后可得 bar 是 15:31、close `287.94`。

该 daily close 可以用于独立 benchmark，也可以在 benchmark 数据集中
补齐该日；**不能**把它伪造成分钟源中的 15:59/16:00 bar，也不能让它
进入分钟信号或 rolling feature 历史。

更一般地，完整 session 的 15:59 bar 与 daily official close 并不等价，
因为 closing auction、vendor bar 定义和日线 consolidator 均可能不同。
因此应保持 `bar_label=start` 的正式分钟契约，并把独立 daily benchmark
作为单独数据源。

Yahoo 下载文件 SHA-256：

`7cbda615f7c6d03215945978cc90f141f70e4446ac577d53c733c01ea36bbc9d`

## Vendor 拼接点

以 `2016-01-04` 为显式 source split，比较前后各 20 个 session：

| 指标 | split 前 | split 后 |
|---|---:|---:|
| 每个完整日 RTH bar | 390 | 390 |
| median daily volume | 860,777 | 166,875,012 |
| median daily transactions | 327,722.5 | 660,151.5 |
| sub-penny close 比例 | 0% | 约 21.2% |

日成交量中位数跃迁为 193.87 倍；相邻两日从 2015-12-31 的 787,150
变为 2016-01-04 的 202,530,724。bar 数没有结构断点，但 volume unit/
source 和价格精度明显改变。

### 成交量单位验证（2026-07-30，对 Yahoo 独立日线）

用本章已核对的 Yahoo 独立日线（含 volume，4,657 行覆盖全样本）逐日
计算 `yahoo_volume / 本文件_volume`：

| 区间 | ratio 中位数 | p5–p95 | 结论 |
|---|---:|---:|---|
| 2016-01-04 之后 | 1.174 | 1.095–1.335 | 单位为股（约 17% 差异为 consolidator 口径差） |
| 2016-01-04 之前 | 123.0 | 110–145 | 单位为百股整手（×100） |

排除 ×1000 的依据：×1000 意味着 pre-2016 日成交量中位数约 12.6 亿
股，超过当时全市场总量；且 ×100 后 pre 段平均 2.5 手/笔 = 250 股/笔，
与 post 段 236–318 股/笔无缝衔接（×1000 则为不成立的 2,500 股/笔）。

事件日抽查 ratio：2008-09-29 → 115.0，2010-05-06 → 116.9，
2011-08-08 → 122.6，2015-08-24 → 143.4。

注意：pre-2016 的 ratio 逐年漂移（2008 年约 113 → 2015 年约 132），
即该列即使 ×100 换算后仍逐年低估全合并成交量约 10–30%（最可能为整手
口径排除碎股、且碎股占比在此期间上升所致）。

处理要求：

- 单日累计 VWAP 可继续使用，因为成交量共同尺度在单日内约掉；
- pre-2016 volume 已验证为百股整手：允许用于单日内相对量（VWAP 分母）
  和 regime 内相对比较；×100 后可作 regime 内近似绝对量，但必须注明
  约 10–30% 且逐年加大的低估；
- 不允许未经 regime 处理，直接做跨拼接点的绝对成交量、容量或冲击成本
  比较；
- 正式 manifest/audit 必须保留 `source_split=2016-01-04` 和该限制。

## 分红独立核对

外部主来源为 State Street 的
[SPDR Historical Distributions](https://www.ssga.com/library-content/products/fund-data/etfs/us/spdr-etf-historical-distributions.xlsx)。
下载的工作簿 SHA-256：

`bf9e8ba5825c62bb48b123070929b9db2b8e5e7249b826b858c3b869ca84ac1a`

官方工作簿 `dividend!A1:J13074` 中共有 135 笔 SPY 历史分配；候选区间
内有 74 笔。核对结果：

- 74 个 ex-date 全部一一对应，没有缺失或多余日期；
- 1 笔金额精确相等；
- 72 笔为官方六位小数金额的正确三位小数舍入；
- `2021-12-17` 本地为 `1.633`，官方为 `1.636431`，差 `0.003431`。

旧输入已保留不动。新增文件：

- `data/reference/spy_dividends_state_street_20260730.csv`
- `data/reference/spy_dividends_state_street_20260730.metadata.json`
- `scripts/extract_state_street_dividends.py`

新 CSV SHA-256 为
`7f11b4419d6497e4c9c6d13878d99c4baff6a817377aaecf67aaeba500be0fa7`；
候选 run 的清洁分红输出 SHA-256 为
`d6f63572845ad46bfac2308fb10af1b60d62065d06f2d6625dcef5d3ce0a3990`。
`config/data_release_v1.yml` 已改用该精确输入。

## 可复现性

两次候选运行中，以下 15 个核心产物逐字节 SHA-256 一致：

- 4 个分钟 parquet；
- 2 个 feature validity parquet；
- `spy_dividends_clean.csv`；
- 8 个核心 audit CSV。

其中主要 hash：

- `spy_1min_clean.parquet`:
  `16d8b23fda49356b9698337050b20bc92c605e8fb0f4a8ddbbf6f0b884603be7`
- `feature_validity_session.parquet`:
  `0244ca370e13c91febfe4976a889c6e311b94f283f3b918e5e5b378c34fbc072`
- `feature_validity_minute.parquet`:
  `9413dc0a866085b2ded7b2d1cc49ca574849d10e2ddd2197e88bcb20e5db5de8`

`data_manifest.json`、`audit_summary.md` 和 `_SUCCESS` 含 run ID、runtime
及 Git provenance，预期不会与不同 run ID 的文件逐字节相同；冻结时应
比较其规范化内容或明确排除这些运行元数据。

## data-v1.0 发布程序

1. 将重定界决策、pipeline、官方分红输入、config、lock 和审计文档提交，
   使工作树 clean。
2. 用冻结配置连续运行两次；两次都必须通过 28 项 preflight tests、
   全部 acceptance gates 和 Git provenance gate。
3. 比较所有数据及 audit CSV 的 SHA-256；仅 run-specific metadata 允许
   不同。
4. 从通过复现的 run 发布不可变 `data_release_v1/`，生成 release
   manifest 和 `_SUCCESS`。
5. Git tag `data-v1.0` 必须指向正式 run manifest 记录的同一 commit。

独立 daily 数据仍只用于 benchmark；不得回填分钟特征。
