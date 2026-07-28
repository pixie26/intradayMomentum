# 运行所需但未包含的输入

本交付包没有包含大型市场数据和真实分红数据。正式运行至少需要：

1. `SPY_1min_2008_202607_merged.parquet`
2. `spy_dividends_full.csv`

正式经济评估还建议增加：

3. 独立daily SPY raw-close数据；
4. 每日现金利率、融资利率和SPY borrow rate；
5. 如研究容量，分钟级spread/quote或可估计impact的数据。

将原始数据放入项目根目录或按README配置路径后，先运行数据pipeline，再由run directory驱动engine。
