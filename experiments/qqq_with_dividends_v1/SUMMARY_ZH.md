# QQQ 带股息重跑实验摘要(qqq_with_dividends_v1)

日期:2026-08-04(2026-08-05 更新融资)。状态:探索性重跑,**不是**预先注册的经济评估。是
`qqq_first_pass_v1`(无股息)的对照版:同一策略 spec、同一 7 个 cell,但
加入股息序列后引擎以 `ignore_dividends=False / require_dividends=True` 运行。
**2026-08-05 起接入 `financing-rates-v2`**(2007-04-25..2026-07-31,冻结 v1 的纯超集,
重叠区间逐行一致):现金按 SOFR/LIBOR 代理减 50bp 计息、杠杆融资加 100bp、借券 25bp。
此前"现金收益为 0"的假设已被替换,下表所有数字均含融资。

## 新增输入:QQQ 股息序列

- 文件:`data/reference/qqq_dividends_crossvalidated_20260804.csv`(81 个 ex-date,
  2007-06-15..2026-06-22),溯源见同目录 `..._PROVENANCE.md`。
- 三源合并(无单一官方源能以全精度覆盖整个样本):
  1. **Invesco(发行方)**官方产品页 Distributions 表,5 位精确,5 个最近 ex-date
     (2025-06-23..2026-06-22),作发行方真值交叉校验。该页 "View since inception"
     控件因遮挡层无法在 in-app browser 中展开,故发行方表无法整段抓取。
  2. **Nasdaq(上市交易所)**官方报价 API,5 位精确,2012-06-15..2026-06-22
     (52 行;该源缺 2012-09-21 / 2012-12-21 两季)。
  3. **Yahoo Finance** chart API,3 位精度,2007-06-15..2026-06-22 全覆盖
     (24 行用于 2007-2012 及 Nasdaq 缺的两季)。
- 交叉验证:重叠期三源 ex-date 完全一致;Nasdaq 与 Invesco 在最近 5 行 5 位精确
  相等;Yahoo 与另两源在 3 位精度上一致(仅精度差)。2007-2012 Yahoo 3 位
  (±$0.0005)对当时 $30-60 股价、$0.03-0.16 股息可忽略。

## 数据层重跑

- `prepare_spy_data.py` 加 `--dividends` 跑出新 run
  `data/candidates/qqq_v1_observed/runs/20260804T143705Z`(53s,自检 28/28)。
- 股息清洗:81 行入,0 冲突,0 ex-date 落在非交易日,产出 `spy_dividends_clean.csv`。
- 验收门与首跑一致,仅 "Dividend file validated" 由 FAIL 变 PASS;其余
  (1 行无效 OHLC 删除、4 个 halt present_in_halt、git dirty)同首跑,已披露。

## 结果(连续全样本运行后切分;2024-05-01 发表后切点;基准为 QQQ 全收益;含 financing-rates-v2 融资)

| cell | 区间 | CAGR | Sharpe | MDD | 基准(全收益)CAGR |
|---|---|---:|---:|---:|---:|
| corrected_execution × paper_ready × $0.005 | 全样本 | 12.23% | 0.84 | -30.7% | 15.17% |
| 〃 | 发表后 | 17.92% | 0.97 | -14.1% | 24.66% |
| corrected_execution × paper_ready × $0.0025 | 发表后 | 18.35% | 0.99 | -13.8% | 24.66% |
| corrected_execution × halt_aware × $0.0025 | 发表后 | 18.35% | 0.99 | -13.8% | 24.66% |
| paper_spec × paper_ready × $0.001 | 发表后 | 18.84% | 1.02 | -13.4% | 24.66% |
| official_sample_compatible × exploratory × $0 | 发表后 | 18.94% | 1.02 | -13.6% | 24.66% |

> 注:组合 CAGR 含现金利息。amended headline(发表后)组合 CAGR 18.35%,其中
> 纯交易 CAGR(逐日移除现金利息)13.76%、现金利息年化 3.98%——现金 carry 不是策略 alpha。
> 发表前现金利息年化仅 1.06%(2008-2021 利率低),故融资对早期 CAGR 影响小、对发表后(高利率)影响大。

## 与无股息首跑(v1)的差异

> 下述股息对比取自 2026-08-04 的 0% 融资版本(现金收益为 0);当前版本已叠加
> financing-rates-v2,故下表数字不再与该对比直接可比。股息本身的影响仍如所述。

- 策略本身隔夜空仓,股息只影响除权日开盘缺口调整与基准:0% 融资下发表后 headline
  ($0.005)CAGR 13.17%->13.51%,Sharpe 0.95->0.97;amended 类比($0.0025)
  13.59%->13.93% / 0.98->1.00。变化很小,定性结论不变。
- 基准由价格买入持有(24.00%)升为全收益(24.66%):QQQ 发表后股息率约
  0.5%/年。策略仍只赚基准的一半左右,alpha 不敌 beta 的结论不变。
- **halt_aware 与 paper_ready 数字仍完全相同**:QQQ 上 4 个熔断日都是
  halt_anomaly,被两个层级同时排除,无 halt_adjusted 日。

## 融资接入的影响(2026-08-05)

- 接入 financing-rates-v2 后,amended headline 发表后组合 CAGR 由 0% 融资的
  13.93% 升至 18.35%:几乎全部增量来自现金利息(年化 3.98%),纯交易 CAGR
  仅 13.76%。发表前组合 CAGR 14.05%(纯交易 12.90%、现金利息 1.06%)--早期
  低利率使增量小。**现金 carry 不是策略 alpha**,比较时应看纯交易口径。
- 杠杆融资成本与借券成本很小(全样本 funding -$7.4k、borrow -$0.8k,vs
  现金利息 +$184k):策略多为 1x 多空对冲,隔夜空仓,借入现金/借券敞口低。

## 统计不确定性(`uncertainty_results/`)

对 amended 类比 cell(corrected_execution × halt_aware × $0.0025,带息)做
8,000 抽样、20 会话循环块 bootstrap + Bartlett-Newey-West HAC,复用
`statistical_uncertainty_v1/run_uncertainty.py` 的已审计估计量(不重写统计代码):

- Sharpe:发表前 0.995,发表后 0.994,差 +0.001(基本持平)。
- pre-minus-post 90% bootstrap 区间 [-1.01, +1.06]:含零,无显著变化。
- 发表后 Sharpe 90% bootstrap 区间 [0.00, 1.92]、HAC(lag=20)区间
  [0.06, 1.93]:均排除零,但区间宽(发表后仅 318 个会话)。
- 结论:QQQ **无发表后衰减**;与 SPY 加编"不能判定衰减显著"同向,但 QQQ
  是因为根本没有衰减。

## 未做 / 风险

- 同首跑:未做市场冲击/容量、排队成交模型;未核对第二数据源;2007-06-01
  整日缺失与熔断日零星成交建议找厂商或第二数据源核实。
- 股息源:2007-2012 为 Yahoo 3 位精度(已交叉验证、影响可忽略);若升级为
  正式 QQQ 发布,应从 Invesco 取全段发行方精确值。
- 厂商价 raw-vs-adjusted 已由 Yahoo 第三方交叉验证确认(见 `crosscheck_raw_vs_adjusted/`):
  逐年价位吻合至美元级、81 个除权日开盘跳空相关 0.996。厂商价为 raw 未调整,
  引擎股息调整与全收益基准均正确,无二次调整/重复计息。管道 `evidence_supports_raw_prices=false`
  是低分红率下的已知弱测试误报。
- 已接入 financing-rates-v2(2007-04-25..2026-07-31,冻结 v1 纯超集):现金
  SOFR/LIBOR 代理减 50bp、杠杆融资加 100bp、借券 25bp。超集证明:与 v1 重叠
  区间(2008-01-22..2026-07-09)逐行一致;冻结 SPY v1 评价不受影响。
- 升级为正式发布前:先冻结 QQQ 自己的评估 spec,invalid-row-policy 回到
  error 并人工裁决那一行,干净 git HEAD,确定性重跑哈希比对。
