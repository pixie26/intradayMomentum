# 贡献与研究变更指南

这个仓库同时承载可复现研究、冻结评估与探索性实验。贡献的首要目标不是让回测数字变好，而是让每个结论都能回答：输入是什么、当时可见的信息是什么、改变了哪个概念组件、产物如何重建。

## 开始之前

1. 先读 [根 README](README.md) 了解当前结论与证据分层。
2. 用 [文档索引](docs/README.md) 找到本次变更对应的数据、引擎、评估或实验文档。
3. 安装锁定依赖：

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. 确认 `git status --short`，不要覆盖他人的未提交变更。

本仓库不要求把可重建的大型 run 目录提交到 Git；正式结果通过配置、manifest、hash、`_SUCCESS` 与可读报告留痕。运行输入与本地 bundle 的边界见 [运行输入说明](docs/MISSING_RUNTIME_INPUTS_ZH.md)。

## 先分类，再修改

| 类型 | 示例 | 最低要求 |
|---|---|---|
| 文档 / 仓库维护 | 链接、索引、CI、措辞 | 不改变研究输出；运行链接与相关测试 |
| 数据层 | 时间戳、日历、缺失、分红、有效性 | 原始数据只读；完整 self-test；新路径发布 |
| 引擎层 | 信号、成交、halt、成本、融资 | 一次只改一个概念组件；运行引擎测试 |
| 正式评估 | spec、headline、benchmark | 不改冻结 spec；必须 clean worktree 与完整 provenance |
| 探索性实验 | EOD、杠杆、费用、跨资产 | 与正式 run 隔离；明确 post-result / exploratory 标签 |

任何使用已观察结果作出的选择，都不能写成预注册决定。SPY frozen v2、事后 reporting amendment 与 QQQ 探索性扩展必须保持清晰分层。

## 数据与产物规则

- 不在原始 OHLCV 上就地清洗、填充或复权。
- 特征先在完整交易日历上计算，session tier 最后才作为交易 mask。
- 不把当日 high/low/close、当日 VIX 变化等收盘后变量用于声称无前视的规则。
- 冻结 artifact 不覆盖；新运行写入新的 run ID。
- 提交源码、配置、简洁表格、manifest 与读者可打开的 HTML 报告。
- 默认忽略大体积、可重建的 Parquet、逐 cell 明细和临时目录。若确有必要提交二进制或大型结果，PR 中说明不可替代性、许可、体积和更新策略。

## 验证命令

基础、无需正式市场 bundle 的检查：

```powershell
python scripts/check_markdown_links.py
python prepare_spy_data.py --self-test
python test_engine.py
python test_attribution_report.py
node test_attribution_report_dom.js
```

依赖本地 ignored release/run 的检查：

```powershell
python test_evaluation_runner.py
python experiments/statistical_uncertainty_v1/test_uncertainty.py
```

只运行实际相关的检查是可以的，但交付或 PR 必须同时写明已运行、未运行及原因。GitHub Actions 只执行不依赖 ignored bundle 的子集。

## PR / 交付清单

- 说明变更文件和动机。
- 标明 engineering-only、strategy-affecting 或 post-result experiment。
- 列出实际命令与结果，不把未运行的检查写成通过。
- 若输出变化，列出必须重建的冻结或实验产物。
- 说明日期范围、输入来源、hash、Git 状态和剩余风险。
- 确认 README、文档索引和相对链接仍然有效。
