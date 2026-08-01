# EOD 价格来源实验 v1

本实验只改变 EOD flatten 的价格和明确列示的增量成本，不改变信号、日内成交、
仓位规则、分红、融资曲线或冻结的 post-publication 切点。它是看过结果后的执行敏感性，
不是新的 OOS，也不替代 frozen v2 结果。

## 主结果

| EOD 方案 | Full CAGR | Pre CAGR | Post CAGR | Post Sharpe | Post MDD | vs 15:59 Post CAGR |
|---|---:|---:|---:|---:|---:|---:|
| 15:59 minute close | 16.70% | 18.00% | 7.52% | 0.30 | -17.1% | +0.00pp |
| Independent daily close, no extra auction cost | 16.91% | 18.01% | 9.11% | 0.40 | -16.4% | +1.59pp |
| Independent daily close + 0.5 bp auction cost | 15.82% | 16.92% | 8.00% | 0.33 | -17.3% | +0.48pp |
| Independent daily close + 1 bp auction cost | 14.73% | 15.83% | 6.90% | 0.26 | -18.3% | -0.62pp |
| Last 5 scheduled-minute TWAP | 15.38% | 16.31% | 8.75% | 0.38 | -14.6% | +1.23pp |
| Last 10 scheduled-minute TWAP | 14.87% | 15.82% | 8.11% | 0.35 | -13.7% | +0.59pp |
| Last 15 scheduled-minute TWAP | 14.51% | 15.34% | 8.56% | 0.38 | -13.3% | +1.04pp |
| Last 30 scheduled-minute TWAP | 13.81% | 14.35% | 9.87% | 0.47 | -11.6% | +2.35pp |

Post-publication 下：

- 15:59 minute close：CAGR 7.52%，Sharpe 0.30。
- 独立 daily close、未加 auction 成本：CAGR 9.11%，相对基线 +1.59pp。
- 独立 daily close + 0.5 bp：CAGR 8.00%；以 0.5/1 bp 两点线性插值，
  post CAGR 与基线相等的增量 auction 成本约为 0.72 bp（仅为局部近似）。
- 独立 daily close + 1 bp EOD auction 成本：CAGR 6.90%，相对基线 -0.62pp。
- 尾盘 10-minute TWAP：CAGR 8.11%，相对基线 +0.59pp。

## 价格源诊断

在基线实际需要 EOD flatten 的 1355 个 session 中，独立 daily close
相对 minute close 的有符号差中位数为 $0.0000，
绝对差 P95 为 $0.1500，最大值为 $5.2500；
364 个 session 的绝对差超过 $0.05。这个量级说明 cross-source
close 差异不能自动解释为可捕获的 closing-auction alpha。

最大差出现在 2025-04-09：minute close 为 $543.37，而 daily close 为 $548.62。
把这一天的 daily-close 变体收益替换回 minute-close 基线后，该变体 post CAGR 为
8.63%，说明结论并非只由这一日决定，
但这一日对相对表现有实质影响，不能忽略。

## 解释与边界

- `独立 daily close` 来自 Yahoo raw daily close，是独立 close 代理，不是真实 MOC fill、
  官方 closing-auction imbalance feed 或券商成交回报。
- 1 bp auction 成本是相对基础 `$0.0025/share` slippage 的单边增量，只在 EOD 退出腿收费。
- TWAP 是最后 10 个计划分钟的 minute close 等份成交的等价平均价；完整日为
  15:50–15:59，半日市取最后 10 分钟。未建模 participation、queue、partial fill 和 impact。
- 所有变体沿各自 AUM 连续复利并重新计算随后仓位，不是固定仓位的事后 PnL 加减。
- 当前只报告点估计；HAC、block bootstrap、真实 auction/TAQ 校验仍未做。

## 研究判断

1. EOD close 口径对策略具有经济显著影响，不能继续把它当成无关紧要的记账细节。
2. 没有跨时期占优的方案：所有 TWAP 窗口在 post 均高于 15:59 close，但在 full/pre
   均明显更差。尤其 30-minute TWAP 的 post CAGR 最高、full/pre CAGR 最低，不能据此
   事后选择窗口。
3. 独立 daily close 的 post 优势只在增量 auction 成本低于约 0.72 bp 时保留；1 bp
   已使其低于 15:59 基线。加上 cross-source 差异和 2025-04-09 极端日，目前不能把
   daily-close 结果解释为可复制的 auction alpha。
4. 因此不修改 frozen v2 headline。下一步若继续，应取得 official close/auction print、
   MOC 成交或 TAQ 级证据，并加入 participation、impact 与 partial-fill 模型。

## 审计信息

- run：`20260801T104328Z_eod_close_source_v1`
- classification：`exploratory_post_result_execution_sensitivity`
- data：2008-01-22 至 2026-07-09
- Git commit：`6072b428ffa9941da053037f8cf1bceea77b8fd7`；dirty：`true`
- config SHA-256：`bb0c986f851188bd0f830818157443358d8e24e850901a58892b0d8b3426acca`
- engine SHA-256：`c2b6062e5da0471fde245d3cae8f62dee434667a6a34eb0a876ebd2bc0875e3f`
- daily close SHA-256：`5dc28af7090d155da99939928170bfc2bf799d853e11c212d40f6564b836bc28`
- financing SHA-256：`3f4addb685cbbdf5f887aec7837825b3fbefff13d4fdfd2192673b5458698be8`

完整交互前不需要的审计表、逐日结果和极端 close 差异见本 run 的 rebuildable results 目录；
可读 HTML：`docs/EOD_CLOSE_SOURCE_EXPERIMENT_V1_ZH.html`。
