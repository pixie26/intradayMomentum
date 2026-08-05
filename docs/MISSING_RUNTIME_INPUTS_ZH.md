# 运行输入与本地发布包

> 文件名为兼容历史链接而保留；“运行输入未包含”已不再准确。

当前 Git 仓库已经包含 SPY 的原始分钟 Parquet、两套保留 provenance 的 SPY 分红输入、PIT financing release、Section 31 费率表、QQQ 股息参考输入，以及源码、冻结配置和可读报告。路径统一位于 `data/raw/` 与 `data/reference/`，不再从项目根目录读取散落的数据文件。

## Git 跟踪的输入

| 输入 | 路径 | 用途 |
|---|---|---|
| SPY 原始分钟数据 | `data/raw/SPY_1min_2008_202607_merged.parquet` | data-v1.0 的只读 source |
| SPY 精确分红 | `data/reference/spy_dividends_state_street_20260730.csv` | data-v1.0 与 total-return benchmark |
| 历史 rounded 分红 | `data/raw/spy_dividends_full.csv` | 旧版 provenance，不是当前主输入 |
| Financing v1 / v2 | `data/reference/financing_rates_v1/`、`financing_rates_v2/` | 分别覆盖 SPY frozen v2 与 QQQ 扩展样本 |
| QQQ 股息 | `data/reference/qqq_dividends_crossvalidated_20260804.csv` | 探索性 QQQ 重跑；非单一发行方全段真值 |

`data/raw/SPY_1min.csv` 是 2007 年的一小段历史 vendor 文件，保留用于来源核对，不是 data-v1.0 主输入。

## 本地存在但 Git 忽略的内容

- `data_release_v1/`：由冻结发布流程生成的不可变 data bundle。
- `benchmark_release_v1/`：独立 SPY daily raw-close benchmark bundle。
- `data/processed/`、`data/candidates/`：可重建的管道 run。
- `evaluation/results/` 与各实验 `results/`：正式/实验运行明细。
- `data/raw/QQQ_1min_20260731.csv`：约 180 MB 的 vendor export；fresh clone 不包含，因此 QQQ 全量重跑需要单独取得该文件。

这些目录不应被误解为“项目尚未完成”。关键正式结论、配置、manifest 摘要与读者可用 HTML 已跟踪；大型逐分钟和逐 cell 中间产物留在本地，通过 hash 与 run ID 对账。

## Fresh clone 的复现边界

fresh clone 可以运行数据 self-test、引擎合成测试、链接检查和已跟踪报告检查。重建完整 SPY evaluation 还需要先发布本地 `data_release_v1/` 与 `benchmark_release_v1/`；重建 QQQ 还需要未随 Git 分发的 vendor 分钟文件。容量与 market-impact 研究仍缺分钟级 quote/spread、auction volume 或同等级别的成交证据。

具体命令见[根 README](../README.md#复现与验证)与[贡献指南](../CONTRIBUTING.md)。
