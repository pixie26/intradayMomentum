import argparse
from pathlib import Path

import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
from matplotlib.ticker import FuncFormatter


matplotlib.use("Agg")


def load_intraday_data(merged_parquet: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load merged data, keep RTH minutes, and build daily OHLCV summary."""
    print("Loading raw data...")
    df = pd.read_parquet(merged_parquet)

    if "caldt" in df.columns:
        df["caldt"] = pd.to_datetime(df["caldt"], errors="coerce")
    elif "timestamp" in df.columns:
        df["caldt"] = pd.to_datetime(df["timestamp"].astype(str).str[:19], errors="coerce")
    else:
        raise ValueError("Neither 'caldt' nor 'timestamp' column exists in input data.")

    df = df.dropna(subset=["caldt"]).copy()
    df.set_index("caldt", inplace=True)
    df.sort_index(inplace=True)

    if "timestamp" in df.columns:
        df.drop(columns=["timestamp"], inplace=True)

    df = df.between_time("09:30", "15:59")
    df["day"] = df.index.date

    daily_groups = df.groupby("day")
    df_daily = pd.DataFrame(index=df["day"].unique())
    df_daily["open"] = daily_groups["open"].first()
    df_daily["high"] = daily_groups["high"].max()
    df_daily["low"] = daily_groups["low"].min()
    df_daily["close"] = daily_groups["close"].last()
    df_daily["volume"] = daily_groups["volume"].sum()

    return df, df_daily


def load_dividend_data(dividend_file: Path) -> pd.DataFrame:
    """Load, clean, and standardize local dividend data for date-based merging."""
    print("Loading dividend data...")
    div_start = pd.Timestamp("2008-01-01")
    div_end = pd.Timestamp("2026-12-31")

    div_df = pd.read_csv(dividend_file)
    div_df = div_df.rename(columns={"date": "Date", "dividend": "Dividend"})
    if "Date" not in div_df.columns or "Dividend" not in div_df.columns:
        raise ValueError("Dividend file must contain Date and Dividend columns.")

    div_df["Date"] = pd.to_datetime(div_df["Date"], errors="coerce").dt.normalize()
    div_df["Dividend"] = pd.to_numeric(div_df["Dividend"], errors="coerce")

    div_df = div_df.dropna(subset=["Date", "Dividend"])
    div_df = div_df[(div_df["Date"] >= div_start) & (div_df["Date"] <= div_end)]
    div_df = div_df.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
    div_df.set_index("Date", inplace=True)

    dividends = (
        div_df.copy()
        .reset_index()
        .rename(columns={"Date": "caldt", "Dividend": "dividend"})
    )
    dividends["caldt"] = pd.to_datetime(dividends["caldt"])
    dividends.columns = [col.lower() for col in dividends.columns]
    return dividends


def add_key_variables(spy_intra_data: pd.DataFrame, dividends: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """Build only the base-model features needed for the naive strategy."""
    df = pd.DataFrame(spy_intra_data)
    df["day"] = pd.to_datetime(df["caldt"]).dt.date
    df.set_index("caldt", inplace=True)

    daily_groups = df.groupby("day")
    all_days = df["day"].unique()

    df["move_open"] = np.nan

    for d in range(1, len(all_days)):
        current_day = all_days[d]

        current_day_data = daily_groups.get_group(current_day)

        open_price = current_day_data["open"].iloc[0]
        df.loc[current_day_data.index, "move_open"] = (
            current_day_data["close"] / open_price - 1
        ).abs()

    df["min_from_open"] = (
        ((df.index - df.index.normalize()) / pd.Timedelta(minutes=1)) - (9 * 60 + 30) + 1
    )
    df["minute_of_day"] = df["min_from_open"].round().astype(int)

    minute_groups = df.groupby("minute_of_day")
    df["move_open_rolling_mean"] = minute_groups["move_open"].transform(
        lambda x: x.rolling(window=14, min_periods=13).mean()
    )
    df["sigma_open"] = minute_groups["move_open_rolling_mean"].transform(lambda x: x.shift(1))

    dividends["day"] = pd.to_datetime(dividends["caldt"]).dt.date
    df = df.merge(dividends[["day", "dividend"]], on="day", how="left")
    df["dividend"] = df["dividend"].fillna(0)

    return df, all_days


def run_backtest(
    df: pd.DataFrame,
    all_days: np.ndarray,
    spy_daily_data: pd.DataFrame,
    aum_0: float = 100000.0,
    commission: float = 0.0035,
    min_comm_per_order: float = 0.35,
    trade_freq: int = 30,
) -> pd.DataFrame:
    """Run the base intraday momentum strategy without later enhancements."""
    daily_groups = df.groupby("day")

    strat = pd.DataFrame(index=all_days)
    strat["ret"] = np.nan
    strat["AUM"] = aum_0
    strat["ret_spy"] = np.nan

    df_daily = pd.DataFrame(spy_daily_data)
    df_daily["caldt"] = pd.to_datetime(df_daily["caldt"]).dt.date
    df_daily.set_index("caldt", inplace=True)
    df_daily["ret"] = df_daily["close"].diff() / df_daily["close"].shift()

    for d in range(1, len(all_days)):
        current_day = all_days[d]
        prev_day = all_days[d - 1]

        if prev_day not in daily_groups.groups or current_day not in daily_groups.groups:
            continue

        prev_day_data = daily_groups.get_group(prev_day)
        current_day_data = daily_groups.get_group(current_day)

        if "sigma_open" in current_day_data.columns and current_day_data["sigma_open"].isna().all():
            continue

        prev_close_adjusted = (
            prev_day_data["close"].iloc[-1] - df.loc[current_day_data.index, "dividend"].iloc[-1]
        )

        open_price = current_day_data["open"].iloc[0]
        current_close_prices = current_day_data["close"]
        sigma_open = current_day_data["sigma_open"]

        ub = max(open_price, prev_close_adjusted) * (1 + sigma_open)
        lb = min(open_price, prev_close_adjusted) * (1 - sigma_open)

        # Base model trigger: breakout above/below dynamic noise area boundaries.
        signals = np.zeros_like(current_close_prices)
        signals[current_close_prices > ub] = 1
        signals[current_close_prices < lb] = -1

        previous_aum = strat.loc[prev_day, "AUM"]
        shares = round(previous_aum / open_price)

        # Evaluate entries/reversals only on selected minute buckets.
        trade_indices = np.where(current_day_data["min_from_open"] % trade_freq == 0)[0]
        exposure_marks = np.full(len(current_day_data), np.nan)
        current_pos = 0

        for idx in trade_indices:
            signal = signals[idx]
            if current_pos == 0:
                if signal == 1:
                    current_pos = 1
                elif signal == -1:
                    current_pos = -1
            elif current_pos == 1:
                if signal == -1:
                    current_pos = -1
            elif current_pos == -1:
                if signal == 1:
                    current_pos = 1
            exposure_marks[idx] = current_pos

        exposure = pd.Series(exposure_marks, index=current_day_data.index).ffill().shift(1).fillna(0).values

        # Count exposure flips, including end-of-day flattening.
        trades_count = np.sum(np.abs(np.diff(np.append(exposure, 0))))

        change_1m = current_close_prices.diff().fillna(0)
        gross_pnl = np.sum(exposure * change_1m) * shares
        commission_paid = trades_count * max(min_comm_per_order, commission * shares)
        net_pnl = gross_pnl - commission_paid

        strat.loc[current_day, "AUM"] = previous_aum + net_pnl
        strat.loc[current_day, "ret"] = net_pnl / previous_aum

        spy_ret_today = df_daily.loc[df_daily.index == current_day, "ret"]
        if len(spy_ret_today) > 0:
            strat.loc[current_day, "ret_spy"] = spy_ret_today.values[0]

    return strat


def evaluate_and_plot(
    strat: pd.DataFrame,
    output_aum_png: Path,
    output_return_png: Path,
    aum_0: float,
    commission: float,
) -> dict:
    """Create charts and compute performance statistics."""
    ticker = "SPY"
    strat["AUM_SPX"] = aum_0 * (1 + strat["ret_spy"]).cumprod(skipna=True)
    strat["cumret_pct"] = (strat["AUM"] / aum_0 - 1) * 100
    strat["cumret_spy_pct"] = (strat["AUM_SPX"] / aum_0 - 1) * 100

    valid_period = strat.index[strat["AUM"].notna()]
    if len(valid_period) > 0:
        start_date = pd.to_datetime(valid_period.min())
        end_date = pd.to_datetime(valid_period.max())
        period_label = f"Backtest Period: {start_date:%Y-%m-%d} to {end_date:%Y-%m-%d}"
    else:
        period_label = "Backtest Period: N/A"

    # Chart 1: AUM (portfolio value) comparison.
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.plot(strat.index, strat["AUM"], label="Momentum Strategy", linewidth=2.5, color="#2C3E50")
    ax.plot(
        strat.index,
        strat["AUM_SPX"],
        label=f"{ticker} Buy & Hold",
        linewidth=2,
        color="#E74C3C",
    )

    ax.grid(True, linestyle="--", alpha=0.3)
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    plt.xticks(rotation=45, ha="right")

    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.set_ylabel("Portfolio Value ($)", fontweight="medium")
    ax.set_xlabel("Date", fontweight="medium")

    legend = ax.legend(loc="upper left", frameon=True, fancybox=True, shadow=True)
    legend.get_frame().set_facecolor("white")

    ax.set_title("Intraday Momentum Strategy Performance", fontsize=14, fontweight="bold", pad=24)
    ax.text(
        0.5,
        1.01,
        period_label,
        transform=ax.transAxes,
        ha="center",
        va="bottom",
        fontsize=10,
        color="dimgray",
    )
    fig.suptitle(f"Commission: ${commission}/share", fontsize=10, y=0.02, color="gray")

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("lightgray")
    ax.spines["bottom"].set_color("lightgray")

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.15, top=0.9)
    plt.savefig(output_aum_png, dpi=150, bbox_inches="tight")
    plt.close(fig)

    # Chart 2: cumulative return (%) comparison.
    fig_ret, ax_ret = plt.subplots(figsize=(12, 6))
    ax_ret.plot(
        strat.index,
        strat["cumret_pct"],
        label="Momentum Strategy Return",
        linewidth=2.2,
        color="#2C3E50",
    )
    ax_ret.plot(
        strat.index,
        strat["cumret_spy_pct"],
        label=f"{ticker} Buy & Hold Return",
        linewidth=1.8,
        color="#E74C3C",
    )
    ax_ret.axhline(0, color="gray", linewidth=1, linestyle="--", alpha=0.7)
    ax_ret.grid(True, linestyle="--", alpha=0.3)
    ax_ret.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.0f}%"))
    ax_ret.set_ylabel("Cumulative Return (%)", fontweight="medium")
    ax_ret.set_xlabel("Date", fontweight="medium")
    ax_ret.legend(loc="upper left", frameon=True, fancybox=True)
    ax_ret.spines["top"].set_visible(False)
    ax_ret.spines["right"].set_visible(False)
    ax_ret.spines["left"].set_color("lightgray")
    ax_ret.spines["bottom"].set_color("lightgray")

    ax_ret.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
    ax_ret.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    plt.setp(ax_ret.get_xticklabels(), rotation=45, ha="right")

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.15, top=0.92)
    plt.savefig(output_return_png, dpi=150, bbox_inches="tight")
    plt.close(fig_ret)

    returns = strat["ret"].dropna()
    spy_returns = strat["ret_spy"].dropna()

    # Align series once for all two-series metrics.
    aligned_idx = returns.index.intersection(spy_returns.index)
    returns_aligned = returns.loc[aligned_idx]
    spy_returns_aligned = spy_returns.loc[aligned_idx]

    total_return = (np.prod(1 + returns) - 1) * 100
    annualized_return = (np.prod(1 + returns) ** (252 / len(returns)) - 1) * 100
    annualized_vol = returns.std() * np.sqrt(252) * 100
    sharpe_ratio = np.nan
    if returns.std() != 0:
        sharpe_ratio = returns.mean() / returns.std() * np.sqrt(252)

    downside = returns[returns < 0]
    downside_std_ann = downside.std() * np.sqrt(252)
    sortino_ratio = np.nan
    if downside_std_ann and not np.isnan(downside_std_ann) and downside_std_ann != 0:
        sortino_ratio = (returns.mean() * 252) / downside_std_ann

    hit_ratio = (returns > 0).sum() / (returns.abs() > 0).sum() * 100

    cumulative = strat["AUM"] / strat["AUM"].iloc[0]
    rolling_max = cumulative.expanding().max()
    drawdowns = (cumulative - rolling_max) / rolling_max
    max_drawdown = drawdowns.min() * -100

    calmar_ratio = np.nan
    if max_drawdown != 0:
        calmar_ratio = annualized_return / max_drawdown

    spy_total_return = (np.prod(1 + spy_returns) - 1) * 100
    spy_annualized_return = (np.prod(1 + spy_returns) ** (252 / len(spy_returns)) - 1) * 100
    spy_annualized_vol = spy_returns.std() * np.sqrt(252) * 100

    excess = returns_aligned - spy_returns_aligned
    information_ratio = np.nan
    if excess.std() != 0:
        information_ratio = (excess.mean() / excess.std()) * np.sqrt(252)

    y = returns_aligned
    x = sm.add_constant(spy_returns_aligned)
    model = sm.OLS(y, x).fit()
    alpha = model.params.const * 100 * 252
    beta = model.params["ret_spy"]

    stats = {
        "Total Return (%)": round(total_return, 1),
        "Annualized Return (%)": round(annualized_return, 1),
        "Annualized Volatility (%)": round(annualized_vol, 1),
        "Sharpe Ratio": round(sharpe_ratio, 2),
        "Sortino Ratio": round(sortino_ratio, 2),
        "Calmar Ratio": round(calmar_ratio, 2),
        "Hit Ratio (%)": round(hit_ratio, 1),
        "Maximum Drawdown (%)": round(max_drawdown, 1),
        f"{ticker} Total Return (%)": round(spy_total_return, 1),
        f"{ticker} Annualized Return (%)": round(spy_annualized_return, 1),
        f"{ticker} Annualized Volatility (%)": round(spy_annualized_vol, 1),
        "Information Ratio": round(information_ratio, 2),
        "Alpha (%)": round(alpha, 2),
        "Beta": round(beta, 2),
        "R-squared": round(model.rsquared, 3),
    }
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtesting BeatTheMarket 2008-2026 naive base model")
    parser.add_argument("--workdir", type=str, default=".", help="Working directory")
    parser.add_argument(
        "--merged-parquet",
        type=str,
        default="SPY_1min_2008_202607_merged.parquet",
        help="Pre-merged parquet file",
    )
    parser.add_argument(
        "--dividend-file",
        type=str,
        default="spy_dividends_full.csv",
        help="Dividend csv file",
    )
    parser.add_argument(
        "--output-aum-png",
        type=str,
        default="spy_momentum_equity_curve_2008_2026_naive.png",
        help="Output AUM chart path",
    )
    parser.add_argument(
        "--output-return-png",
        type=str,
        default="spy_momentum_cumreturn_2008_2026_naive.png",
        help="Output cumulative return chart path",
    )
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve()
    merged_parquet = workdir / args.merged_parquet
    dividend_file = workdir / args.dividend_file
    output_aum_png = workdir / args.output_aum_png
    output_return_png = workdir / args.output_return_png

    if not merged_parquet.exists():
        raise FileNotFoundError(
            f"Merged parquet not found: {merged_parquet}. "
            "Please place SPY_1min_2008_202607_merged.parquet in workdir."
        )

    df, df_daily = load_intraday_data(merged_parquet)
    dividends = load_dividend_data(dividend_file)

    spy_intra_data = df.copy().reset_index()
    spy_intra_data.columns = [col.lower() for col in spy_intra_data.columns]
    spy_daily_data = df_daily.copy().reset_index().rename(columns={"index": "caldt"})
    spy_daily_data["caldt"] = pd.to_datetime(spy_daily_data["caldt"])
    spy_daily_data.columns = [col.lower() for col in spy_daily_data.columns]

    # Build feature columns used by the strategy.
    feature_df, all_days = add_key_variables(spy_intra_data, dividends)

    aum_0 = 100000.0
    commission = 0.0035
    strat = run_backtest(
        df=feature_df,
        all_days=all_days,
        spy_daily_data=spy_daily_data,
        aum_0=aum_0,
        commission=commission,
        min_comm_per_order=0.35,
        trade_freq=30,
    )

    # Create performance chart and metric summary.
    stats = evaluate_and_plot(
        strat,
        output_aum_png=output_aum_png,
        output_return_png=output_return_png,
        aum_0=aum_0,
        commission=commission,
    )

    print("\n" + "=" * 45)
    print("    STRATEGY PERFORMANCE SUMMARY")
    print("=" * 45)
    for key, value in stats.items():
        print(f"{key:<25} {value:>8}")
    print("=" * 45)
    print(f"AUM chart saved to: {output_aum_png}")
    print(f"Return chart saved to: {output_return_png}")


if __name__ == "__main__":
    main()
