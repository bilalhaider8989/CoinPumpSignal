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
     24h volume), then drop anything in CONFIG["backtest"]["symbol_blacklist"].
  2. Download real historical candles for every timeframe that strategy
     actually uses (e.g. nouman_scalp uses 1h + 15m), paginated past
     Binance's 1000-candles-per-call cap.
  3. Walk forward one signal-timeframe candle at a time - but now
     CHRONOLOGICALLY ACROSS ALL SYMBOLS TOGETHER (not one symbol fully,
     then the next), so a shared daily loss counter and shared account
     balance make sense. At each step, a temporary get_klines()
     replacement only reveals data up to THAT candle's close for THAT
     symbol - it cannot see the future. The exact same check_symbol_*()
     function the live scanner uses is called - if it returns a hit,
     that's a simulated entry at that candle's close.
  4. Once in a trade, each following candle for that symbol is checked
     against take-profit / stop-loss / a max holding period (whichever
     comes first) - see CONFIG["backtest"]["exits"]. NONE of the live
     scanners define an exit on their own; these are assumptions for
     backtesting purposes, not a recommendation.
  5. Reports win rate, average return, total return (simple sum of
     per-trade % returns - NOT compounded), max drawdown, profit
     factor, and total $ P&L (see RISK MANAGEMENT below). Writes a
     full per-trade CSV alongside the summary if
     CONFIG["backtest"]["output_csv"] is True.

RISK MANAGEMENT (new)
----------------------
See CONFIG["backtest"]["risk_management"]:
  - "risk_pct_per_trade": each trade's $ position size is calculated so
    that if its stop-loss is hit, the loss equals this % of
    "starting_balance" (position_size = balance * risk_pct / stop_loss_pct).
    This is a FIXED reference balance, not a compounding account - if
    you want compounding, feed the "total_pnl_usd" result back in as a
    new starting_balance for your next run.
  - "daily_loss_limit": after this many CONSECUTIVE losing trades close
    on the same UTC calendar day (across ANY symbol), no NEW trades are
    opened for the rest of that day. Resets on the next win and at the
    next UTC day. A 0%-or-negative-return exit (e.g. a "TIME" exit that
    closed flat) counts as a loss for this counter. Set to 0 to disable.
  - "max_concurrent_positions": caps how many symbols can have an open
    trade at once, so the circuit breaker's "1% of balance per trade"
    math doesn't get quietly violated by five simultaneous positions
    all sized off the same balance.

CAVEAT: with multiple symbols able to hold positions at the same time,
position sizes are each computed off the same fixed "starting_balance",
NOT off "balance minus what's already committed to open trades" - so
actual simultaneous capital-at-risk can exceed one trade's risk_pct if
several positions are open together. max_concurrent_positions limits
how bad that can get; it does not eliminate it. Real portfolio-level
capital accounting would be a further step.

WHAT THIS DOES NOT DO
-----------------------
- No fees or slippage modelled - real results will be worse than this.
- No true compounding equity curve (see RISK MANAGEMENT above).
- Same-candle TP+SL ambiguity is resolved conservatively (stop-loss
  wins) since OHLC data alone can't tell you which happened first
  intra-candle.
- No automatic coin-category filtering (meme/defi/etc.) - you supply
  the exact symbols to exclude via "symbol_blacklist".
- This does not know the future and cannot promise the past predicts
  it - a good backtest tells you the strategy wasn't obviously broken
  historically, not that it will make money going forward.

USAGE
-----
    python backtest.py
"""

import time
from datetime import datetime, timezone

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


def _utc_date(close_time_ms: int):
    return datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc).date()


def run_backtest_strategy(symbols: list, strategy: str, full_data: dict, exit_cfg: dict,
                           warmup_bars: int, risk_cfg: dict) -> tuple[list, int]:
    """
    Chronological, cross-symbol walk-forward simulation for ONE strategy
    across ALL symbols at once (rather than fully walking one symbol,
    then the next), so a shared daily loss circuit breaker and a shared
    reference account balance are meaningful. Temporarily replaces
    bs.get_klines with a version that only reveals data up to the
    simulated "now" for whichever symbol is being checked - no lookahead.

    Returns (trades, circuit_breaker_skip_count).
    """
    step_iv = _step_interval(strategy)
    check_fn = _CHECK_FNS[strategy]

    # Build one global, time-ordered list of (close_time, symbol, bar_index)
    # events - every candle close, for every symbol, that has enough
    # warm-up history behind it (mirrors the old per-symbol
    # range(warmup_bars, n - 1) window).
    events: list = []
    df_by_symbol: dict = {}
    for symbol in symbols:
        df_step = full_data[symbol][step_iv]
        n = len(df_step)
        if n < warmup_bars + 5:
            print(f"  {symbol}: not enough {step_iv} history for a warm-up ({n} bars) - skipping")
            continue
        df_by_symbol[symbol] = df_step
        for i in range(warmup_bars, n - 1):
            events.append((int(df_step["close_time"].iloc[i]), symbol, i))

    events.sort(key=lambda e: e[0])  # chronological across ALL symbols

    risk_pct = risk_cfg.get("risk_pct_per_trade", 1.0)
    balance_ref = risk_cfg.get("starting_balance", 10_000.0)
    daily_loss_limit = risk_cfg.get("daily_loss_limit", 0)
    max_concurrent = risk_cfg.get("max_concurrent_positions") or float("inf")

    state: dict = {}          # shared strategy state (candle de-dup etc.), same shape the live scanner uses
    open_trades: dict = {}    # symbol -> entry info, for symbols currently in a trade
    trades: list = []
    consecutive_losses_today = 0
    current_day = None
    circuit_breaker_skips = 0

    cutoff = {"t": 0}

    def mock_get_klines(sym, interval=None, limit=None):
        tf = full_data[sym][interval]
        sub = tf[tf["close_time"] <= cutoff["t"]]
        if limit:
            sub = sub.tail(limit)
        return sub.reset_index(drop=True)

    original_get_klines = bs.get_klines
    bs.get_klines = mock_get_klines

    try:
        for close_time, symbol, i in events:
            day = _utc_date(close_time)
            if day != current_day:
                current_day = day
                consecutive_losses_today = 0  # new UTC day - circuit breaker resets

            bar = df_by_symbol[symbol].iloc[i]

            if symbol in open_trades:
                # ---- Check exit for an already-open position ----
                trade = open_trades[symbol]
                held = i - trade["entry_i"]
                high_ret = (bar["high"] - trade["entry_price"]) / trade["entry_price"] * 100
                low_ret = (bar["low"] - trade["entry_price"]) / trade["entry_price"] * 100

                if low_ret <= -exit_cfg["stop_loss_pct"]:
                    exit_reason, exit_ret = "SL", -exit_cfg["stop_loss_pct"]
                elif high_ret >= exit_cfg["take_profit_pct"]:
                    exit_reason, exit_ret = "TP", exit_cfg["take_profit_pct"]
                elif held >= exit_cfg["max_hold_bars"]:
                    exit_reason = "TIME"
                    exit_ret = (bar["close"] - trade["entry_price"]) / trade["entry_price"] * 100
                else:
                    continue  # still open, nothing to do on this bar

                pnl_usd = trade["position_size_usd"] * exit_ret / 100
                trades.append({
                    "symbol": symbol,
                    "entry_time": trade["entry_time"],
                    "exit_time": int(bar["close_time"]),
                    "entry_price": trade["entry_price"],
                    "return_pct": exit_ret,
                    "reason": exit_reason,
                    "bars_held": held,
                    "position_size_usd": trade["position_size_usd"],
                    "pnl_usd": pnl_usd,
                })
                del open_trades[symbol]

                # Circuit breaker bookkeeping: any non-positive exit counts
                # as a "loss" here (a flat TIME-out included).
                if exit_ret <= 0:
                    consecutive_losses_today += 1
                else:
                    consecutive_losses_today = 0

            else:
                # ---- Look for a new entry ----
                if daily_loss_limit and consecutive_losses_today >= daily_loss_limit:
                    circuit_breaker_skips += 1
                    continue  # tripped for the rest of today
                if len(open_trades) >= max_concurrent:
                    continue  # already at the concurrent-position cap

                cutoff["t"] = close_time
                try:
                    hit = check_fn(symbol, state, {symbol: 999_000_000.0})
                except Exception:
                    hit = None
                if hit:
                    entry_price = float(bar["close"])
                    position_size_usd = balance_ref * (risk_pct / 100) / (exit_cfg["stop_loss_pct"] / 100)
                    open_trades[symbol] = {
                        "entry_price": entry_price,
                        "entry_i": i,
                        "entry_time": close_time,
                        "position_size_usd": position_size_usd,
                    }
    finally:
        bs.get_klines = original_get_klines

    return trades, circuit_breaker_skips


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
    pnl_usd = np.array([t.get("pnl_usd", 0.0) for t in trades])

    return {
        "count": len(trades),
        "win_rate_pct": 100 * len(wins) / len(trades),
        "avg_return_pct": float(returns.mean()),
        "total_return_pct": float(returns.sum()),
        "avg_win_pct": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_pct": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown_pct": float(drawdown.min()) if len(drawdown) else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "total_pnl_usd": float(pnl_usd.sum()),
    }


def run_backtest() -> None:
    cfg = CONFIG["backtest"]
    risk_cfg = cfg.get("risk_management", {})

    if cfg["symbols"]:
        symbols = cfg["symbols"]
        print(f"Using fixed symbol list ({len(symbols)} symbols).")
    else:
        symbols = get_top_symbols_by_volume(cfg["top_n_symbols"], 3_000_000)
        print(f"Auto-selected top {len(symbols)} symbols by 24h volume: {', '.join(symbols)}")

    blacklist = set(cfg.get("symbol_blacklist", []))
    if blacklist:
        before = len(symbols)
        symbols = [s for s in symbols if s not in blacklist]
        removed = blacklist & set(symbols) or (blacklist if before != len(symbols) else set())
        print(f"  Blacklist excluded {before - len(symbols)} symbol(s) (blacklist: {sorted(blacklist)})")

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

        trades, cb_skips = run_backtest_strategy(
            symbols, strategy, full_data, cfg["exits"][strategy], cfg["warmup_bars"], risk_cfg
        )

        summary = summarize_trades(trades)
        print(f"\n--- {strategy} summary ({len(symbols)} symbols, {days}d) ---")
        for k, v in summary.items():
            print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")
        if risk_cfg.get("daily_loss_limit"):
            print(f"  circuit_breaker_skips: {cb_skips} (new entries skipped after "
                  f"{risk_cfg['daily_loss_limit']} same-day consecutive losses)")

        if cfg["output_csv"] and trades:
            out_path = f"backtest_{strategy}_trades.csv"
            pd.DataFrame(trades).to_csv(out_path, index=False)
            print(f"  Trade log written to {out_path}")


if __name__ == "__main__":
    run_backtest()