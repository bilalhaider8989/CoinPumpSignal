"""
backtest.py - walk-forward backtest for nouman_strategy, nouman_scalp,
and squeeze_breakout, using REAL historical Binance data.

IMPORTANT - RUN THIS WHERE BINANCE IS REACHABLE
-------------------------------------------------
This needs to talk to Binance's REST API. If you're running it somewhere
that can't reach data-api.binance.vision (e.g. certain sandboxed dev
environments), it will fail to fetch history. Your own machine and your
existing GitHub Actions workflow both already reach Binance fine (that's
how the live scanners work), so run it from either of those - e.g.
locally with `python backtest.py`, or as a one-off manually-triggered
GitHub Actions job.

HOW IT WORKS
------------
For each strategy:
  1. Pick a symbol list (CONFIG["backtest"]["symbols"], or auto top-N by
     24h volume).
  2. Download real historical candles for every timeframe that strategy
     actually uses (e.g. nouman_scalp uses 1h + 15m), paginated past
     Binance's 1000-candles-per-call cap.
  3. Walk forward one signal-timeframe candle at a time. At each step, a
     temporary get_klines() replacement only reveals data up to THAT
     candle's close - it cannot see the future. The exact same
     check_symbol_*() function the live scanner uses is called - if it
     returns a hit, that's a simulated entry at that candle's close.
  4. Once in a trade, each following candle is checked against
     take-profit / stop-loss / a max holding period (whichever comes
     first) - see CONFIG["backtest"]["exits"]. NONE of the live scanners
     define an exit on their own; these are assumptions for backtesting
     purposes, not a recommendation, and probably the first thing worth
     tuning once you see real numbers.
  5. Reports win rate, average return, total return (simple sum of
     per-trade % returns - NOT compounded, and assumes one fixed-size
     trade at a time per symbol, not realistic position sizing), max
     drawdown, and profit factor. Writes a full per-trade CSV alongside
     the summary if CONFIG["backtest"]["output_csv"] is True.

WHAT THIS DOES NOT DO
-----------------------
- No fees or slippage modelled - real results will be worse than this.
- No realistic position sizing / portfolio-level compounding.
- Same-candle TP+SL ambiguity is resolved conservatively (stop-loss
  wins) since OHLC data alone can't tell you which happened first
  intra-candle.
- This does not know the future and cannot promise the past predicts
  it - a good backtest tells you the strategy wasn't obviously broken
  historically, not that it will make money going forward.

USAGE
-----
    python backtest.py
"""

import time

import numpy as np
import pandas as pd
import requests

import binance_scanner as bs
from binance_scanner import (
    BINANCE_BASE,
    CONFIG,
    _interval_minutes,
    check_symbol_nouman,
    check_symbol_nouman_scalp,
    check_symbol_squeeze,
    get_top_symbols_by_volume,
)

_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]

_CHECK_FNS = {
    "nouman_strategy": check_symbol_nouman,
    "nouman_scalp": check_symbol_nouman_scalp,
    "squeeze_breakout": check_symbol_squeeze,
}


def _required_intervals(strategy: str) -> set:
    if strategy == "nouman_strategy":
        return {"1h", CONFIG["nouman_strategy"]["signal_interval"]}
    if strategy == "nouman_scalp":
        return {"1h", CONFIG["nouman_scalp"]["interval"]}
    if strategy == "squeeze_breakout":
        ivals = {CONFIG["squeeze_breakout"]["interval"]}
        if CONFIG["squeeze_breakout"]["trend_filter"]["enabled"]:
            ivals.add(CONFIG["squeeze_breakout"]["trend_filter"]["interval"])
        return ivals
    raise ValueError(f"Unknown strategy: {strategy}")


def _step_interval(strategy: str) -> str:
    if strategy == "nouman_strategy":
        return CONFIG["nouman_strategy"]["signal_interval"]
    if strategy == "nouman_scalp":
        return CONFIG["nouman_scalp"]["interval"]
    if strategy == "squeeze_breakout":
        return CONFIG["squeeze_breakout"]["interval"]
    raise ValueError(f"Unknown strategy: {strategy}")


def fetch_historical_klines(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """Paginated historical fetch - Binance caps a single call at 1000
    candles, so longer windows need multiple calls walking forward via
    startTime."""
    interval_ms = _interval_minutes(interval) * 60 * 1000
    end_time = int(time.time() * 1000)
    cursor = end_time - days * 24 * 60 * 60 * 1000
    rows: list = []

    while cursor < end_time:
        r = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "startTime": cursor, "limit": 1000},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows.extend(data)
        cursor = data[-1][0] + interval_ms
        if len(data) < 1000:
            break
        time.sleep(CONFIG["backtest"]["request_sleep"])

    if not rows:
        return pd.DataFrame(columns=_KLINE_COLUMNS)

    df = pd.DataFrame(rows, columns=_KLINE_COLUMNS)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df.drop_duplicates(subset="open_time").reset_index(drop=True)


def backtest_symbol(symbol: str, strategy: str, full_data: dict, exit_cfg: dict, warmup_bars: int) -> list:
    """Walk-forward simulation for one symbol/strategy. Returns a list
    of trade dicts. Temporarily replaces bs.get_klines with a version
    that only reveals data up to the simulated 'now' - no lookahead."""
    step_iv = _step_interval(strategy)
    check_fn = _CHECK_FNS[strategy]
    df_step = full_data[symbol][step_iv]
    n = len(df_step)
    if n < warmup_bars + 5:
        print(f"  {symbol}: not enough {step_iv} history for a warm-up ({n} bars) - skipping")
        return []

    cutoff = {"t": 0}

    def mock_get_klines(sym, interval=None, limit=None):
        tf = full_data[sym][interval]
        sub = tf[tf["close_time"] <= cutoff["t"]]
        if limit:
            sub = sub.tail(limit)
        return sub.reset_index(drop=True)

    original_get_klines = bs.get_klines
    bs.get_klines = mock_get_klines

    trades: list = []
    state: dict = {}
    volume_24h_dummy = {symbol: 999_000_000.0}  # symbol selection already applied the real liquidity floor
    in_trade = False
    entry_price = entry_i = entry_time = None

    try:
        for i in range(warmup_bars, n - 1):
            cutoff["t"] = int(df_step["close_time"].iloc[i])

            if not in_trade:
                try:
                    hit = check_fn(symbol, state, volume_24h_dummy)
                except Exception:
                    hit = None
                if hit:
                    in_trade = True
                    entry_price = float(df_step["close"].iloc[i])
                    entry_i = i
                    entry_time = int(df_step["close_time"].iloc[i])
            else:
                bar = df_step.iloc[i]
                held = i - entry_i
                high_ret = (bar["high"] - entry_price) / entry_price * 100
                low_ret = (bar["low"] - entry_price) / entry_price * 100

                # Same-candle TP+SL is ambiguous from OHLC alone - stop-
                # loss wins in that case (the conservative assumption).
                if low_ret <= -exit_cfg["stop_loss_pct"]:
                    exit_reason, exit_ret = "SL", -exit_cfg["stop_loss_pct"]
                elif high_ret >= exit_cfg["take_profit_pct"]:
                    exit_reason, exit_ret = "TP", exit_cfg["take_profit_pct"]
                elif held >= exit_cfg["max_hold_bars"]:
                    exit_reason = "TIME"
                    exit_ret = (bar["close"] - entry_price) / entry_price * 100
                else:
                    exit_reason = None
                    exit_ret = None

                if exit_reason:
                    trades.append({
                        "symbol": symbol,
                        "entry_time": entry_time,
                        "exit_time": int(bar["close_time"]),
                        "entry_price": entry_price,
                        "return_pct": exit_ret,
                        "reason": exit_reason,
                        "bars_held": held,
                    })
                    in_trade = False
    finally:
        bs.get_klines = original_get_klines

    return trades


def summarize_trades(trades: list) -> dict:
    if not trades:
        return {"count": 0}
    returns = np.array([t["return_pct"] for t in trades])
    wins = returns[returns > 0]
    losses = returns[returns <= 0]
    equity = np.cumsum(returns)
    running_max = np.maximum.accumulate(equity)
    drawdown = equity - running_max
    gross_win = wins.sum() if len(wins) else 0.0
    gross_loss = abs(losses.sum()) if len(losses) else 0.0
    return {
        "count": len(trades),
        "win_rate_pct": 100 * len(wins) / len(trades),
        "avg_return_pct": float(returns.mean()),
        "total_return_pct": float(returns.sum()),
        "avg_win_pct": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_pct": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown_pct": float(drawdown.min()) if len(drawdown) else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
    }


def run_backtest() -> None:
    cfg = CONFIG["backtest"]

    if cfg["symbols"]:
        symbols = cfg["symbols"]
        print(f"Using fixed symbol list ({len(symbols)} symbols).")
    else:
        symbols = get_top_symbols_by_volume(cfg["top_n_symbols"], 3_000_000)
        print(f"Auto-selected top {len(symbols)} symbols by 24h volume: {', '.join(symbols)}")

    for strategy in cfg["strategies"]:
        print(f"\n{'=' * 60}\nBacktesting: {strategy}\n{'=' * 60}")
        intervals = _required_intervals(strategy)
        days = cfg["history_days"].get(strategy, 30)
        print(f"Fetching {days}d of {'/'.join(sorted(intervals))} history for {len(symbols)} symbols...")

        full_data: dict = {}
        for symbol in symbols:
            full_data[symbol] = {}
            for iv in intervals:
                try:
                    full_data[symbol][iv] = fetch_historical_klines(symbol, iv, days)
                except Exception as e:
                    print(f"  {symbol} {iv}: fetch failed ({e})")
                    full_data[symbol][iv] = pd.DataFrame(columns=_KLINE_COLUMNS)
                time.sleep(cfg["request_sleep"])

        all_trades: list = []
        for symbol in symbols:
            print(f"  Walking {symbol}...")
            trades = backtest_symbol(symbol, strategy, full_data, cfg["exits"][strategy], cfg["warmup_bars"])
            all_trades.extend(trades)

        summary = summarize_trades(all_trades)
        print(f"\n--- {strategy} summary ({len(symbols)} symbols, {days}d) ---")
        for k, v in summary.items():
            print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")

        if cfg["output_csv"] and all_trades:
            out_path = f"backtest_{strategy}_trades.csv"
            pd.DataFrame(all_trades).to_csv(out_path, index=False)
            print(f"  Trade log written to {out_path}")


if __name__ == "__main__":
    run_backtest()