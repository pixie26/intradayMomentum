# SPY 一分钟数据质量审计报告（首次本地全量运行）

**运行日期：2026-07-29**
**run_id：`20260729T091312Z`（`data/processed/runs/20260729T091312Z/`，含 `_SUCCESS`，`latest.json` 已指向）**

本文记录数据 pipeline v4 在本机对真实 SPY 数据的首次全量审计结果：每一类数据
问题的定义、本数据集中的实际证据、不处理的后果、pipeline 的修正方式，以及
留档位置。后续数据更新或换数据源时，应对照本文重跑并比较。

## 运行溯源

| 项 | 值 |
|---|---|
| 命令 | `python prepare_spy_data.py --input SPY_1min_2008_202607_merged.parquet --dividends spy_dividends_full.csv --output-dir data/processed --input-timezone America/New_York --bar-label start --source-split 2016-01-04` |
| 自测 | `python prepare_spy_data.py --self-test` → **18 项通过** |
| 运行时长 | 19.07s（manifest 记录） |
| source_sha256 | `dedcc3cd460dfdd4fd90495f197868519b513a36e0d0723fc12ee47858714218` |
| script_sha256 | `21af7584686b2f5e46c7c5d6f14f5b4dbe4ea28e63f748e98ad256d0d3059e7a` |
| 环境 | Python 3.12.7 / pandas 2.2.2 / numpy 1.26.4 / exchange-calendars 4.13.2 / pyarrow 16.1.0 |
| 行守恒 | 1,813,340 输入行 = 9,057 场外剔除 + 1,804,283 干净 RTH 行（其余各类剔除均为 0，断言通过） |

环境插曲：Windows 上发布阶段 `os.replace(staging, runs/<id>)` 首次报
`WinError 5`（新写文件被 Defender/同步软件短暂锁柄），重试一次即成功。
staging 内容完整无损，属环境问题而非逻辑 bug；若后续频繁出现，应给发布
步骤加重试。

---

## 一、时间戳列选择 —— 发现问题且关键

**证据**：原始文件有两个时间戳列：`caldt` 100% 非空；`timestamp`（字符串）
仅 56.78% 非空——缺失的 43.2% 恰好是 **2008–2015 全部年份**（每年约
9.7–9.9 万行）。这是两段 vendor 数据拼接的痕迹。

**不处理的后果**：若按别名字典顺序取第一个匹配列，会选中 `timestamp`，
2008–2015 全部约 78 万行被静默丢弃，回测从 2016 年才开始且不自知。

**修正**：`pick_timestamp_column` 按**非空率**选列并在 manifest 记录候选
分布；tz-naive 输入必须显式传 `--input-timezone`，拒绝猜测；解析后强制
1980–2100 年份合理性检查（防止 YYYYMMDDHHMMSS 被当 epoch 读出 2611 年）。
本次实测：0 坏时间戳行、0 越界、0 off-grid。

## 二、非交易时段 bar —— 9,057 行，剔除并留档

**证据**：场外 bar 三部分——半日市 13:00 收盘后 vendor 继续发的 bar
（3,162 行，集中在感恩节次日/圣诞前夜）；正常日盘前 08:00–09:29（360 行）；
16:00 收盘及盘后（5,535 行）。

**修正**：用 XNYS 日历把每根 bar 映射到 session 与 `minute_of_session`，
`is_rth=False` 全部剔除，明细留档 `reports/outside_rth_rows.csv`，不是
静默丢弃。

## 三、重复 / 冲突 timestamp、非法 OHLC、off-grid —— 均为 0

四项行级检查全部通过：0 重复 timestamp、0 冲突 OHLCV（同一时间戳不同
价格）、0 非法 OHLC（High/Low 关系、正价格、非负成交量）、0 非整分钟
时间戳。默认策略是冲突即报错并输出明细，本数据未触发。

## 四、缺失 minute（interior gap）—— 3 个 session

| 日期 | 缺分钟数 | 组件影响 |
|---|---:|---|
| 2016-02-02 | 9 | 缺口后 `vwap_valid=False`；`move_open_obs_valid` 仍 True |
| 2021-05-05 | 5 | 同上 |
| 2023-06-05 | 4 | 同上 |

**解释**：VWAP 依赖从开盘到当前的**每一根**必需分钟，缺一根则其后全部
不可信；`move_open` 只依赖当日真实开盘 + 当前 bar，缺口后仍可用。两种
依赖结构不能压成一个 flag。

**修正**：不 forward-fill 造假 bar。三日剔出 `paper_ready`（进
`exploratory`）；`feature_validity_minute.parquet` 逐分钟标记；rolling
窗口内缺分钟保留 NaN 占槽位，不向前多取旧日。

## 五、开盘截断（leading truncation）—— 2 个 session，最危险类

**证据**：`2009-07-27` 首根 bar 在 **11:15**（minute 106，缺 105 根）；
`2013-12-23` 缺前 38 根。

**不处理的后果**：`move_open = |close/open_day − 1|` 会把 11:15 的价格当
"当日开盘价"，当天全部观察锚点错误，并污染**后续 14 天**的 sigma 历史。

**修正**：`open_valid=False` → 当日**所有**分钟
`move_open_obs_valid=False`；但 `close_valid=True`——当天真实尾盘仍是
次日合法 prev_close。这是"组件级有效性"而非"整日一刀切"的核心案例。

## 六、尾盘截断（trailing truncation）—— 1 个 session

**证据**：`2019-08-12` 缺最后 28 根 + 中间 2 根，`close_valid=False`。

**不处理的后果**：用"最后可得价"冒充收盘价 → 当日日收益错、次日
prev_close 错、14 日波动率窗口错、benchmark 错；若策略持仓到数据断点，
退出价未知。

**修正**：跨该日的两条日收益都标 `daily_ret_valid=False` 且**占槽位不进
窗口**；引擎侧若持仓暴露到未知尾部则 `unknown_exit`，默认 terminate，
猜测的 PnL 不进入后续 AUM 复利。

## 七、半日市 —— 39 天，全部正常

**证据**：39 个提前收盘日（感恩节次日、圣诞前夜等），**全部恰好 210 根
bar**（13:00 收盘），一根不缺。

**不处理的后果**：把下午 180 个"交易所根本没安排的分钟"当成缺失，半日市
被误判低质，且后续 14 天下午桶的历史计数被压低（v2 真实犯过）。

**修正**：网格按每日**计划**分钟数生成，半日市下午不属于 eligible grid。
实测 39 天全部 grade=complete。

## 八、Halt（熔断）—— 4 天，全部干净

**证据**：2020-03-09 / 03-12 / 03-16 / 03-18 各 14 个官方 halt 分钟（内置
MWCB 表，位置与历史吻合，如 03-16 为开盘第 2–15 分钟）。**0 phantom bar、
0 计划外分钟**——分钟集合验证（而非 bar 计数）通过。

**修正**：halt 分钟 `is_executable=False`，不下单不成交；因无真实成交，
**不视为缺失成交量**，VWAP 定义不被破坏；已持仓仓位完整承担复牌跳空，
未成交订单不吃 gap。

## 九、整日缺失 session —— 0

4,645 个日历 session 全部有数据，coverage=100%，最长连续缺失 0。防护栏
仍生效：>20 天缺失或连续 2 天缺失会直接报错（vendor 停更情形）。

## 十、极端一分钟收益 —— 56 行，全是真实市场事件

**证据**：|1 分钟收益| > 1% 共 56 行，按年聚集：2008 年 25 行（09-29
TARP 否决、10-10）、2010-05-06 闪崩（14:44 −1.97%、14:47 +2.28%）、
2020-03 新冠 12 行、2025-04 关税波动 9 行。最大 2.57%（2025-04-09
13:19）。时间、方向与历史事件完全吻合——**不是坏数据，是尾部风险本身**。

**修正**：默认不删（删掉等于抹掉真实 PnL 与回撤），输出
`extreme_return_rows.csv` 供人工审计。

## 十一、stale / zero-volume bar —— 极少

stale bar（O=H=L=C）仅 6 根、各自孤立最长 1；zero-volume run 为 0（报告
文件因此未生成）。run 检测按 session 变化 + 分钟不连续断段，不会把前日
最后一分钟与次日第一分钟连成一段。

## 十二、Vendor 拼接（format regime）—— 2016-01-04 两段

**证据**：按 `--source-split 2016-01-04` 切两段。regime 0（2008–2015）：
收盘价 0% 亚便士精度，**中日成交量约 126 万股**；regime 1（2016–2026）：
26.3% 亚便士报价，**中日成交量约 6,132 万股**（约 49 倍）。接缝日
2015-12-31 收 203.90 → 2016-01-04 开 200.49（−1.67%，当日为真实大跌）。

**含义**：两段大概率来自不同 vendor，成交量口径不同。VWAP 是 pv/vol，
日内口径一致则商不受影响；但任何跨段比较成交量的分析都会错。
`candidate_format_regime` 只是启发式提示，不冒充 vendor 识别——本次用
显式 `--source-split` 覆盖。

## 十三、分红 —— 74 个事件干净；raw 证据成立，但分段后有重要保留

74 个季度分红（2008-03-20 → 2026-06-18，0.48–1.993 美元），0 冲突、
0 重复、全部落在交易日。清理后文件：`spy_dividends_clean.csv`（sha256
`c64c46b1b60863d24600ff33afbc79b0c00ed60c1063826c23e029633ee2b8c9`）。

manifest 的 pooled 残差检验（74 个除息日合并）支持 raw 价格
（`evidence_supports_raw_prices=true`）。但 pooled 结果被分红金额更大的
regime 1 主导；**按 regime 分段重算后出现重要差异**：

| 检验（均值） | regime 0（32 个除息日） | regime 1（42 个除息日） |
|---|---:|---:|
| 分红率（div/prev_close） | +0.525% | +0.406% |
| 除息日隔夜 gap | −0.110% | −0.403% |
| 加回分红后的隐含市场 gap | **+0.416%**（中位 +0.381%） | +0.002%（中位 −0.014%） |
| 除息日 close-to-close | −0.457% | −0.746% |
| 除息日前/后一日 gap、c2c | 正常 | 正常 |

解读：

- **regime 1（2016–2026）是教科书式 raw**：开盘即跌去全部分红
  （gap −0.403% ≈ 分红率 −0.406%），隐含市场 gap 以 0 为中心对称分布。
  引擎的 `prev_close − dividend` 锚点在这段完全正确。
- **regime 0（2008–2015）的除息日开盘"粘住"**：开盘平均只体现约 20%
  的分红下跌，59% 的除息日隐含市场 gap > +0.25%；全部分红在**当天盘中**
  被吸收（c2c −0.457% ≈ 分红率 + 市场）。效应集中在 2008–2011
  （按年均值 +0.65% ~ +1.48%），2012–2015 明显干净（−0.05% ~ +0.34%）。
- 无法仅凭本数据区分两种成因：vendor 早期段除息日开盘打印异常，或
  危机年代除息日（恰逢季度四巫日）开盘竞价噪声。注意事件日期本身无误
  （闪崩、熔断分钟位置全部吻合），价格水平也无跨段跳变。
- **对策略的影响**：约 32 个 regime-0 早晨（该段 1.6% 的 session），
  开盘锚点比论文假设水平高约 0.4–0.5% → band 中枢上移、`move_open`
  从虚高开盘起步，盘中回落可能制造**系统性偏空的假信号**。规模小但
  非随机。作者 sample 若使用同类 vendor 数据，其历史结果可能内嵌同一
  人工痕迹。
- **处理决定**：原始数据不改动（铁律）。引擎行为不变（按分红文件调整
  prev_close，对文件而言是正确的）。列为敏感性分析项：可做剔除除息日
  的对照运行；若有独立参考行情源，应复核 regime-0 除息日开盘价。

---

## 汇总裁定

| 类别 | 本次发现 | 处理 |
|---|---|---|
| 时间戳列 splice | `timestamp` 列 2008–2015 全空 | 按非空率选 `caldt`，留审计 |
| 场外 bar | 9,057 行（半日市盘后/盘前/16:00 后） | 剔除并留档 |
| 重复/冲突/非法 OHLC/off-grid | 0 | 通过 |
| interior gap | 3 天（9/5/4 分钟） | 出 paper_ready；vwap_valid 缺口后失效 |
| 开盘截断 | 2 天（105/38 分钟） | open_valid=False；当日 move_open 全废，close 仍可用 |
| 尾盘截断 | 1 天（28 分钟） | close_valid=False；两日收益出窗口；unknown_exit |
| 半日市 | 39 天 × 210 根 | 正常，不算缺失 |
| Halt | 4 天 × 14 分钟，无 phantom | halt 分钟不可执行，VWAP 豁免 |
| 整日缺失 | 0 | — |
| 极端收益 | 56 行全是真事件 | 保留 + 审计报告 |
| stale / zero-volume | 6 根 / 0 | 报告 |
| Vendor 拼接 | 2016-01-04，成交量口径约 49× | 显式 split，不静默 |
| 分红 | 74 事件干净；regime 1 教科书 raw；**regime 0 除息日开盘未扣分红（~0.4pp，集中 2008–2011）** | 特征层调整 prev_close；原始数据不改；列敏感性分析项 |

**分层结果**：

| tier | sessions | rows | 定位 |
|---|---:|---:|---|
| `paper_ready` | 4,635 | 1,800,630 | 完整分钟；论文复现与 headline |
| `halt_aware` | 4,639 | 1,802,134 | +4 个熔断验证日；主要经济结果 |
| `exploratory` | 4,645 | 1,804,283 | 全部（含 3 gap + 3 truncated）；仅敏感性 |

引擎在全日历（`spy_1min_clean`）上算特征，最后才应用 tier mask。本 run
已可驱动 `im_engine_v4`。

## 复现与后续

- 重跑：同一命令即可生成新 `runs/<run_id>/`；比对 `source_sha256` 确认输入
  未变。换数据源或扩日期后，应重跑本审计并对照本文各类计数。
- 报告留档（`reports/`）：`session_quality.csv`、`minute_coverage.csv`、
  `halt_minutes.csv`、`extreme_return_rows.csv`、`outside_rth_rows.csv`、
  `longest_stale_bar_runs.csv`、`data_manifest.json`（含全部阈值、CLI、
  输入/输出/报告 SHA-256）。
- 本次未跑：engine 三 profile 回测（下一步）。届时 headline 应使用
  with-dividends 配置。
