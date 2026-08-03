# Post-publication Sharpe 统计不确定性附录 v1

## 结论

`pre 1.18` 与 `post 0.30` 是有方向性的信息，但精度不足，不能写成
“信号质量**显著**衰减”。在当前正式口径下，数据同时与“真实 Sharpe 下降”及
“未下降、当前 post 样本只是较差的一次实现”相容。

本附录把原先延期的 Sharpe block bootstrap 与 HAC delta-method 补入
可复现流程；不改动 frozen v2 spec、正式点估计、72-cell 矩阵或任何交易路径。
它是查看结果后的统计附录，不是事前预注册检验。

## 输入与点估计对账

- source run：`20260731T200227Z_formal_spec2_58205b0c130f`
- source manifest SHA-256：
  `624d4a9956fdaccd8f1e276888a96e6d06b33bd6c86e3cdb772694dcd176d05b`
- headline cell：
  `corrected_execution__halt_aware__with_dividends__slip_0p0025`
- 收益口径：逐日 `ret`；Sharpe 分子逐日扣除正式 `cash_hurdle_ret`，
  分母保持正式程序的 `ret` 样本标准差，年化为 `sqrt(252)`。

| 窗口 | 首日 | 末日 | 正式有效 sessions | Sharpe |
|---|---|---|---:|---:|
| Pre-publication | 2008-02-11 | 2024-04-30 | 4,055 | 1.1836 |
| Post-publication | 2024-05-01 | 2026-07-09 | 548 | 0.3002 |
| Pre − post | — | — | — | 0.8833 |

上述 sessions 与 Sharpe 均逐项对回正式 `summary.csv`，绝对容差为 `5e-12`。
因此讨论稿中的 pre `4,064` 日、Sharpe `1.20 / 0.28` 不是当前正式
amended headline 的精确口径，不沿用。

## 主检验：20-session circular moving-block bootstrap

主分析使用 8,000 次、固定 seed `20260802`、20-session 区块的 circular
moving-block bootstrap。Pre 与 post 在各自窗口内独立重抽；每个区块保留连续
交易日，循环区块使每个样本日在边际上有相同的被抽中概率。区间为 percentile
interval，不是贝叶斯可信区间。

| 统计量 | 点估计 | bootstrap 中位数 | SD | 90% 区间 | 95% 区间 | `P(draw ≤ 0)` |
|---|---:|---:|---:|---:|---:|---:|
| Pre Sharpe | 1.184 | 1.185 | 0.214 | [0.831, 1.528] | [0.759, 1.596] | 0.0% |
| Post Sharpe | 0.300 | 0.303 | 0.620 | [−0.748, 1.281] | [−0.974, 1.467] | 31.5% |
| Pre − post | 0.883 | 0.883 | 0.655 | [−0.163, 1.978] | [−0.368, 2.204] | 8.3% |

直接差值的 90% 与 95% 区间都包含 0。以零假设为中心的近似 bootstrap
检验给出单边 `p = 0.0889`、双边 `p = 0.1715`；在 5% 水平不能拒绝
“pre 与 post Sharpe 没有下降”。这不是证明两期相同，而是当前样本没有足够
精度把差异与抽样波动分开。

`P(draw ≤ 0)`只是经验分布下的重抽样诊断，不能称为“真实 Sharpe 小于 0 的
概率”，也不能直接冒充 p 值。

## HAC 交叉核验

HAC 使用 Bartlett/Newey-West long-run covariance，对
`[ret − hurdle, ret, ret²]` 的均值与二阶矩做 delta-method。主 lag 为 20 sessions。

| 统计量 | 点估计 | HAC SE | 90% 区间 | 95% 区间 |
|---|---:|---:|---:|---:|
| Pre Sharpe | 1.184 | 0.213 | [0.833, 1.535] | [0.765, 1.602] |
| Post Sharpe | 0.300 | 0.587 | [−0.666, 1.266] | [−0.851, 1.451] |
| Pre − post | 0.883 | 0.625 | [−0.145, 1.911] | [−0.342, 2.108] |

差值的 HAC 单边 `p = 0.0788`、双边 `p = 0.1575`，与 block bootstrap 的
判断一致。HAC 区间是对称、渐近的交叉核验；重尾短样本的主要解读仍以
bootstrap 为主。

## 稳健性与讨论稿数字的来源

- circular block 长度从 1、5、10、20、40 到 60 sessions 时，差值的 90%
  区间全部包含 0；单边、零假设居中的 bootstrap p 值为 0.0796–0.1188。
- HAC lag 从 0、5、10、20、40 到 60 时，差值的 90% 区间全部包含 0；
  单边 p 值为 0.0639–0.1033。
- 非循环的 ordinary moving-block、20 sessions 是额外敏感性：post 中位数
  0.481、90% 区间 `[−0.554, 1.423]`、SD 0.596。它接近讨论稿的
  `0.42 / [−0.56, 1.36] / 0.59`，但会降低窗口两端日期的抽中权重；因此不作为
  主口径。无论采用循环或非循环版本，5% 水平下的决策不变。

## 尾部集中度：可以说什么，不能说什么

按 demeaned excess daily return 计算
`N_conc = (Σx²)² / Σx⁴`：

| 窗口 | 名义 sessions | `N_conc` | `3 × N_conc` |
|---|---:|---:|---:|
| Pre | 4,055 | 306.9 | 920.6 |
| Post | 548 | 18.9 | 56.7 |

这有力说明 post 的平方收益由少数尾部日集中贡献；正态样本的该比率约为
`n/3`，所以 `3 × N_conc`可以作为**四阶矩集中度**的正态参照。

但它没有使用收益的自相关结构，也不是 time-series effective sample size。
因此不得把 18.9 政名为“18.9 个独立观测”，更不能由此严格推出“只有 10 张
彩票”。“结果高度依赖少数极端日”成立；具体独立事件数与到达率模型仍需另行
定义和估计。

## 对研究结论与优先级的修改

1. 保留描述性事实：post Sharpe 点估计明显低于 pre，post trading-only CAGR
   弱，且同期大幅跑输 SPY total return。
2. 删除推断性越界：不得把 `1.18 → 0.30`写成“统计显著衰减”，也不得说比较
   “完全没有信息”或“任何点估计比较都无意义”。正确措辞是“方向为下降，但
   估计很不精确，当前样本不能在 5% 水平区分衰减与抽样波动”。
3. 统计不确定性从 roadmap 的延期项提升为当前结论的必要约束；market impact、
   capacity、queue/partial fill 仍有工程价值，但其结果也必须带不确定性，不能再
   只比较点估计。
4. 暂不把“约 8 年才够”写成已验证的固定事实。所需年限依赖备择差异、单边或
   双边检验、block DGP、尾部事件到达过程以及 pre 是否视为已知；当前结果只足以
   确认 2.2 年 post 窗口在目标问题上低精度。
5. “大幅日到达率 4.85% → 1.64%、条件 payoff 不变”暂不升格为结论，因为
   “大幅日”阈值、事前信息集与估计方法尚未冻结。下一项统计研究应先定义该事件，
   再把到达率和条件 payoff 分开估计并给出区间。

## 可复现产物与验证

- config：`experiments/statistical_uncertainty_v1/config.json`
- runner：`experiments/statistical_uncertainty_v1/run_uncertainty.py`
- tests：`experiments/statistical_uncertainty_v1/test_uncertainty.py`
- local result：
  `experiments/statistical_uncertainty_v1/results/20260801T165656Z_uncertainty_v1/`
- 结果包含 `point_estimate_audit.csv`、`bootstrap_summary.csv`、
  `hac_sharpe_summary.csv`、`return_concentration.csv`、manifest 与 `_SUCCESS`。

实际运行：

```text
python experiments/statistical_uncertainty_v1/test_uncertainty.py
STATISTICAL UNCERTAINTY TESTS PASSED (unit + formal-run audit)

python experiments/statistical_uncertainty_v1/run_uncertainty.py
completed; 8,000 bootstrap replications for every configured method/block/period
```

未运行：引擎回测、data self-test、全 72-cell evaluation；本任务只读已发布正式
run，且对点估计做了逐项对账。未实现 alpha/beta 的 HAC standard errors、事件
到达率模型、条件 payoff 模型或事前 power simulation。
