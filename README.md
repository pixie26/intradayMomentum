# Intraday Momentum Research Snapshot — 2026-07-30

本压缩包是当前项目的可交付研究快照，包含最新数据pipeline、回测引擎、测试、README、预注册评估spec，以及两份新增中文研究文档。

## 推荐阅读顺序

1. `docs/PROJECT_WORK_LOG_ZH.md`：从项目开始到现在的完整工作记录；
2. `docs/PAPER_SAMPLE_CURRENT_COMPARISON_ZH.md`：论文、sample code与当前实现逐项比较；
3. `docs/README_DATA.md`：数据层接口和运行方式；
4. `docs/README_ENGINE.md`：三profile和引擎语义；
5. `config/evaluation_spec_v1.yml`：冻结的post-publication评估spec；
6. `config/data_release_v1.yml`：data-v1.0候选边界与环境契约；
7. 项目根目录的 `prepare_spy_data.py`、`im_engine_v4.py`；
8. `test_engine.py`。`src/` 和 `tests/` 同时保留规范化副本。

## 文件说明

- `src/`、`tests/`：2026-07-29交付快照的冻结副本；当前修改只在根目录工作副本；
- `docs/`：最新README、完整研究记录和历史review；
- `config/`：原始预注册spec v1；
- `original_uploads/`：保留用户上传时的原始文件名和内容；
- `manifest/FILE_MANIFEST.sha256`：压缩包内文件hash。

## 本地数据状态

当前工作区包含只读输入 `SPY_1min_2008_202607_merged.parquet`（1,813,340
rows）和 `spy_dividends_full.csv`。尚未发布新的
`data/processed/runs/<run_id>/`，也没有独立daily SPY benchmark文件。
data-v1.0候选契约会在生成任何正式run前阻断缺失的样本边界。

## 验证状态

- 最新三个Python文件在本次交付环境中通过 `python -m py_compile`；
- 2026-07-30 本工作区运行 `python prepare_spy_data.py --self-test`，
  28项data-layer checks通过；
- 真实raw边界审计确认观察范围为2008-01-22至2026-07-09；按
  2008-01-01预期起点运行时，13个前置XNYS交易日缺失，data-v1.0暂不冻结；
- 57项engine tests为既有项目记录，本次data-layer修正未重跑；
- 本包保留原始上传和规范化副本，二者SHA可核对。


## 快速验证

```bash
python -m pip install -r requirements.txt
python prepare_spy_data.py --self-test
python test_engine.py
```

本次工作区已按 `requirements.lock` 安装依赖并完成28项data-layer
self-test及真实raw边界失败验证；未发布真实全样本pipeline run，也未回测。
