# Post-publication evaluation v2 正式结果

> 本文保留 frozen v2 当时的正式点估计与 provenance。后续 post-result 统计附录已经完成 HAC 与 block bootstrap；`pre Sharpe 1.18 → post 0.30` 的方向为下降，但差值区间包含 0，不能表述为统计显著衰减。见[统计不确定性附录](STATISTICAL_UNCERTAINTY_V1_ZH.md)。

正式运行：

- run：`20260731T043943Z_formal_spec2_b4d7a8f805b9`
- evaluation commit：`436f3ad3aaad064304bfb9c2eea3f5e4ea741d71`
- Git dirty：`false`
- spec：`config/evaluation_spec_v2.yml`
- spec SHA-256：`b4d7a8f805b9dd3dacdaf3f2c0f286db2c8ed3d797a7ea3d21764ae09b0eb1b4`
- data release manifest SHA-256：
  `fc63122dff1d13df95735ba2e1df0a9763db100e0c3efbfa99adf948d602c530`
- benchmark SHA-256：
  `5dc28af7090d155da99939928170bfc2bf799d853e11c212d40f6564b836bc28`
- financing curve SHA-256：
  `3f4addb685cbbdf5f887aec7837825b3fbefff13d4fdfd2192673b5458698be8`

## Headline

`corrected_execution × paper_ready × with_dividends × $0.005/share slippage`

| 窗口 | 策略 CAGR | SPY total-return CAGR | Excess CAGR | Sharpe vs cash | MDD |
|---|---:|---:|---:|---:|---:|
| Full sample | 15.35% | 11.96% | +3.39pp | 0.99 | −30.22% |
| Pre-publication | 16.52% | 10.70% | +5.82pp | 1.09 | −30.22% |
| Post-publication | 7.01% | 21.74% | −14.73pp | 0.27 | −17.48% |

Post-publication 每股分解：

| 项目 | USD/share |
|---|---:|
| Gross edge | 2.139¢ |
| Commission + slippage | −0.850¢ |
| Funding + borrow | −0.136¢ |
| Trading edge after costs | +1.153¢ |

所以本轮结果不是“成本把信号完全吃掉”。毛边际和扣成本后的交易边际仍为正，
但策略在论文公开后明显跑输同期 SPY；post 的主要问题是低相对收益和低 Sharpe。

## 主要对照

- Post、固定 `$0.005/share`、paper-ready、with-dividends：
  official 5.93% CAGR，paper_spec 7.07%，corrected 7.01%。
- corrected、post、paper-ready、with-dividends 的 slippage grid：
  0.1¢ / 0.25¢ / 0.5¢ / 1.0¢ 分别得到
  7.83% / 7.52% / 7.01% / 6.00% CAGR。
- corrected、post、0.5¢：paper-ready 7.0108%，halt-aware 7.0112%，
  二者经济上没有显著区别。
- corrected、post、paper-ready、0.5¢：with-dividends 7.01%，
  ignore-dividends 7.08%；分红对策略本身影响小，但 benchmark total-return
  CAGR 从 ignore 的 20.27% 变为 with 的 21.74%。

## Coverage 与审计

- 72/72 cells，216/216 唯一 summary rows。
- 334,440 daily rows；`net = gross − cost + cash_interest + financing`
  与 `aum = prev_aum + net` 的最大残差均为 0。
- 16 个 exploratory post rows 无 performance：路径在 post 前已经遇到
  unknown exit 并按 frozen `terminate` policy 终止。报告显示为 unavailable，
  不重启资本、不插值、不制造结果。
- unknown-exit 日的正式 accounting 字段归零；可审计的已知部分保留在
  `known_partial_*` 列。
- frozen v2 发布当时只报告点估计；后来已由[统计不确定性附录](STATISTICAL_UNCERTAINTY_V1_ZH.md)补充 HAC 与 block bootstrap，但不回写本次冻结 run。
- 2008 年从 2008-01-22 开始，2026 年截至 2026-07-09，年度表两端均为部分年度。

正式 HTML：`docs/POST_PUBLICATION_EVALUATION_V2.html`。

较早的
`20260731T041755Z_formal_spec2_b4d7a8f805b9`
在 post-run audit 中发现 16 个 unknown-exit accounting identity 违例，
已被本 run 取代，不得引用。
