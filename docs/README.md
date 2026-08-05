# 文档索引与证据层级

根 [README](../README.md) 是研究全景和当前结论；本页帮助维护者定位权威契约、冻结结论、事后附录、探索性实验与历史材料。相互矛盾时，优先采用带冻结配置、manifest 和明确日期的较新正式文档；历史文档保留当时语境，不静默改写 provenance。

## 推荐接手顺序

| 顺序 | 目的 | 文档 |
|---:|---|---|
| 1 | 了解研究问题、当前结论和路线图 | [根 README](../README.md) |
| 2 | 理解项目演进和关键决策 | [项目工作记录](PROJECT_WORK_LOG_ZH.md)；需要术语解释时读[详细版](PROJECT_WORK_LOG_ZH_DETAILED.md) |
| 3 | 理解论文、sample 和当前实现差异 | [完整差异说明](PAPER_SAMPLE_CURRENT_COMPARISON_ZH.md) |
| 4 | 理解数据与引擎契约 | [数据层](README_DATA.md)、[引擎层](README_ENGINE.md) |
| 5 | 阅读冻结 SPY 结论及事后约束 | [frozen v2](POST_PUBLICATION_EVALUATION_V2_ZH.md)、[reporting amendment](POST_PUBLICATION_EVALUATION_V2_HALT0025_ZH.md)、[统计不确定性](STATISTICAL_UNCERTAINTY_V1_ZH.md) |
| 6 | 按任务进入专项报告或实验 | 使用下方分类索引 |

## 权威契约与实现语义

- [数据层契约](README_DATA.md)：数据边界、时间戳、duplicate、return gap、component validity 与 data → engine contract。
- [data-v1.0 冻结验收](DATA_V1_FREEZE_AUDIT_20260730_ZH.md) 与[起点决策](DATA_V1_START_DATE_DECISION_ZH.md)：`2008-01-22` 起点、双跑一致性与发布证据。
- [引擎契约](README_ENGINE.md)：三个 profile、halt、成交、会计、benchmark 与融资。
- [交易逻辑与实现详解](PAPER_TRADING_LOGIC_AND_IMPLEMENTATION_ZH.md)：面向读者的逐分钟、公式和事件时间线说明；[HTML 版](PAPER_TRADING_LOGIC.html)适合浏览。
- 冻结机器可读配置位于 [`config/`](../config/)；不得因看到结果而修改原 spec。

## SPY 正式结论与附录

| 证据层 | 文档 / 报告 | 解释 |
|---|---|---|
| 事前冻结主结果 | [v2 结果](POST_PUBLICATION_EVALUATION_V2_ZH.md)、[HTML](POST_PUBLICATION_EVALUATION_V2.html) | `paper_ready × $0.005/share` 原始 headline，必须保留 |
| 事后展示修订 | [halt-aware × $0.0025](POST_PUBLICATION_EVALUATION_V2_HALT0025_ZH.md) | 报告口径修订，不是新的预注册 headline |
| 归因 | [交互式 P&L 报告](POST_PUBLICATION_EVALUATION_V2_HALT0025_ATTRIBUTION.html) | 分离交易、成本、现金 carry、融资与借券 |
| 统计附录 | [HAC 与 block bootstrap](STATISTICAL_UNCERTAINTY_V1_ZH.md) | 事后附录；约束“衰减显著”的表述 |
| 失败机制 | [年度亏损机制](ANNUAL_FAILURE_MECHANISMS_ZH.md) | 解释 false breakout、方向持续性与前视边界 |

## 事后敏感性实验

这些结果不能覆盖 frozen v2，也不能因表现较好而升级为 headline。

- [EOD close / daily close / TWAP](EOD_CLOSE_SOURCE_EXPERIMENT_V1_ZH.md) 与[HTML](EOD_CLOSE_SOURCE_EXPERIMENT_V1_ZH.html)。
- [杠杆与定仓](LEVERAGE_SIZING_SENSITIVITY_ZH.md) 与[HTML](LEVERAGE_SIZING_SENSITIVITY_ZH.html)。
- [IBKR / SEC Section 31](IBKR_SECTION31_COST_SENSITIVITY_ZH.md) 与[HTML](IBKR_SECTION31_COST_SENSITIVITY_ZH.html)。
- [交易成本讨论与官方来源摘要](IBKR_US_Stock_Trading_Costs_Summary_ZH.md)。
- 可执行实验代码和各自 README 位于 [`experiments/`](../experiments/)。

## QQQ 探索性扩展

QQQ 工作验证框架能迁移到 XNAS/QQQ，但目前不是预注册正式发布。

- [QQQ 数据审计](QQQ_DATA_AUDIT_20260804_ZH.md)。
- [无股息首轮实验](../experiments/qqq_first_pass_v1/SUMMARY_ZH.md)。
- [带股息、融资与统计附录的重跑](../experiments/qqq_with_dividends_v1/SUMMARY_ZH.md)。
- [QQQ 交互式归因报告](../experiments/qqq_with_dividends_v1/QQQ_ATTRIBUTION.html)。
- [QQQ 股息 provenance](../data/reference/qqq_dividends_crossvalidated_20260804_PROVENANCE.md)。

升级为正式 QQQ 发布前仍需冻结独立 spec、人工裁决 invalid row、完成 clean deterministic rerun、补足第二分钟源与更强发行方股息证据。

## 历史、讨论与归档材料

- [首次本地全量数据审计](DATA_AUDIT_20260729_ZH.md) 与 [v3 历史 review](HISTORICAL_V3_REVIEW.md) 记录问题如何被发现；它们不是当前 release contract 的替代品。
- `discussion001.md`：pending order、EOD close、融资与执行假设讨论。
- `discussion002.md`：统计功效、bootstrap 与 Sharpe 差异讨论；精确正式数字以[统计附录](STATISTICAL_UNCERTAINTY_V1_ZH.md)为准。
- `discussion003.md`：对研究结论、成本和解释的批判性 review；其中已解决或被后续证据修正的观点应结合当前 README 阅读。
- [`previous_research/`](../previous_research/) 是原始基线脚本与图，只读保留，不再扩展。
- [运行输入与本地产物说明](MISSING_RUNTIME_INPUTS_ZH.md) 的文件名因历史链接保留，正文已更新为当前状态。

## 维护规则

1. 新文档标题和文件名应表达主题；不要继续增加 `discussion004.md` 这类无语义名称。
2. 报告必须标明日期范围、输入、配置、run ID、hash、Git 状态与结论边界。
3. 读者入口保留中文；必要的 profile、字段名和审计原始值可保留英文。
4. HTML 报告可内嵌数据，但对应的 Markdown 摘要必须给出核心结论和 provenance。
5. 相对链接由 `python scripts/check_markdown_links.py` 校验；不要提交 `D:\...` 或 `file://` 本机链接。
