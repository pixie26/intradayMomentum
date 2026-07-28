# Intraday Momentum Research Snapshot — 2026-07-29

本压缩包是当前项目的可交付研究快照，包含最新数据pipeline、回测引擎、测试、README、预注册评估spec，以及两份新增中文研究文档。

## 推荐阅读顺序

1. `docs/PROJECT_WORK_LOG_ZH.md`：从项目开始到现在的完整工作记录；
2. `docs/PAPER_SAMPLE_CURRENT_COMPARISON_ZH.md`：论文、sample code与当前实现逐项比较；
3. `docs/README_DATA.md`：数据层接口和运行方式；
4. `docs/README_ENGINE.md`：三profile和引擎语义；
5. `config/evaluation_spec_v1.yml`：冻结的post-publication评估spec；
6. 项目根目录的 `prepare_spy_data.py`、`im_engine_v4.py`；
7. `test_engine.py`。`src/` 和 `tests/` 同时保留规范化副本。

## 文件说明

- `src/`：规范化文件名的最新代码；
- `tests/`：最新engine tests；
- `docs/`：最新README、完整研究记录和历史review；
- `config/`：原始预注册spec v1；
- `original_uploads/`：保留用户上传时的原始文件名和内容；
- `manifest/FILE_MANIFEST.sha256`：压缩包内文件hash。

## 未包含

本包不含以下运行数据，因为当前会话中没有对应实际文件或数据体积较大：

- `SPY_1min_2008_202607_merged.parquet`
- `spy_dividends_full.csv`
- 数据pipeline生成的 `data/processed/runs/<run_id>/`
- 独立daily SPY benchmark文件

因此本包用于代码、审计和研究交付，不是一个无需外部数据即可完整重跑真实结果的数据归档。

## 验证状态

- 最新三个Python文件在本次交付环境中通过 `python -m py_compile`；
- 项目方最新报告：57项engine test + 18项data test通过；
- 本包保留原始上传和规范化副本，二者SHA可核对。


## 快速验证

```bash
python -m pip install -r requirements.txt
python prepare_spy_data.py --self-test
python test_engine.py
```

本次容器缺少 `pyarrow`，因此只完成了 `py_compile`；直接运行集成测试会在Parquet写入阶段提示安装 `pyarrow`。项目方提供的最新本地记录为57项engine test和18项data test通过。
