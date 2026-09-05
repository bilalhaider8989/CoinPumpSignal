"""
websocket_scanner.py - a companion to binance_scanner.py, specifically
for the Squeeze Breakout module.

WHY THIS EXISTS
----------------
binance_scanner.py's REST-based scans loop through symbols one by one,
with a sleep delay between each. That means every symbol is checked at a
slightly different moment, staggered by however long the loop takes to
reach it - and on top of that, GitHub Actions' own cron scheduler can
trigger several minutes late. Neither of those is about which indicator
you use; it's the REST-polling-on-a-schedule architecture itself.

This script opens ONE combined Binance WebSocket connection and watches
every watched symbol AT THE SAME TIME, reacting the instant each one's
candle closes - no per-symbol wait, no scan order to sit through.

WATCHLIST
---------
Uses CONFIG["squeeze_breakout"]["symbol_whitelist"] if you set one.
Left empty (the default), it auto-selects the top `auto_top_n` USDT
pairs by 24h volume every run via get_top_symbols_by_volume() - you
don't hand-maintain a list; "which coins are liquid right now" comes
straight from the API.

HOW EACH RUN WORKS
-------------------
  1. Pick the watchlist (fixed or auto-selected top N by volume).
  2. SEED each symbol with its recent closed candles via a quick REST
     call (concurrently, not sequentially) - this is required: the
     squeeze/breakout math needs ~60+ prior candles of context, and a
     bounded live-only window (a few minutes = a few 1m candles) could
     never accumulate that on its own. This seed step is a ONE-TIME
     REST pull per run, not a repeated per-symbol scan loop.
  3. Open ONE combined WebSocket connection for the whole watchlist and
     append each NEW closed candle onto its seeded history as it
     arrives, evaluating the squeeze+breakout+volume condition
     immediately - this part is genuinely live, no polling delay.
  4. Stop once the TOTAL run (seed + live listening) reaches
     CONFIG["squeeze_breakout"]["websocket_burst"]["max_runtime_seconds"]
     (240s / 4 min by default - safely under a 5-minute cron interval),
     then exit cleanly.

WHAT THIS DOES NOT FIX
-----------------------
The gap BETWEEN scheduled runs. GitHub Actions' `schedule` trigger has a
hard minimum of 5 minutes between runs, and commonly adds its own delay
on top during busy periods - so there's still a multi-minute blind spot
between runs no matter what. Closing that last gap needs a process that
never stops, which means a small always-on host instead of GitHub
Actions (a low-cost VPS, or a free tier like Oracle Cloud's Always Free
compute instance). This script is the best available middle ground
without one - it removes the in-run staggering and the watchlist
guesswork, not the between-run gap.

REQUIRES
--------
    pip install websockets
Add that to your GitHub Actions workflow's install step alongside your
existing `pip install pandas requests`.

USAGE
-----
    python websocket_scanner.py
"""

import asyncio
import json
import time

import pandas as pd
import websockets

from binance_scanner import (
    CONFIG,
    SQUEEZE_STATE_FILE,
    bollinger_bands,
    get_klines,
    get_top_symbols_by_volume,
    load_state,
    save_state,
    send_discord,
)

# Market-data-only endpoint, no API key needed - mirrors the REST
# "data-api.binance.vision" domain binance_scanner.py already uses.
BINANCE_WS_BASE = "wss://data-stream.binance.vision/stream"


def _kline_event_to_row(k: dict) -> dict:
    """Binance kline WebSocket payload -> the same column shape
    get_klines() in binance_scanner.py produces, so bollinger_bands()
    and the dedup logic work identically either way."""
    return {
        "open_time": k["t"], "open": float(k["o"]), "high": float(k["h"]),
        "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"]),
        "close_time": k["T"], "quote_volume": float(k["q"]), "trades": k["n"],
        "taker_buy_base": float(k["V"]), "taker_buy_quote": float(k["Q"]), "ignore": 0,
    }


async def _seed_history(symbols: list[str], interval: str, candle_limit: int) -> dict[str, list[dict]]:
    """One-time REST pull of recent closed candles per symbol, done
    CONCURRENTLY (not in a sequential loop) so this stays fast even for
    50 symbols. This is what lets the live websocket loop evaluate the
    squeeze condition on the very first candle it sees, instead of
    needing to wait ~60 minutes to build up history from scratch."""
    print(f"Seeding history for {len(symbols)} symbols via REST (one-time, concurrent)...")
    t0 = time.time()

    async def _fetch_one(symbol: str) -> tuple[str, list[dict]]:
        try:
            df = await asyncio.to_thread(get_klines, symbol, interval, candle_limit)
            df = df.iloc[:-1]  # drop the still-forming candle
            return symbol, df.to_dict("records")
        except Exception as e:
            print(f"  {symbol}: seed failed ({e})")
            return symbol, []

    results = await asyncio.gather(*[_fetch_one(s) for s in symbols])
    history = {symbol: rows for symbol, rows in results}
    print(f"  Seeding done in {time.time() - t0:.1f}s.")
    return history


def evaluate_squeeze_live(symbol: str, df: pd.DataFrame, state: dict) -> dict | None:
    """Same rules as check_symbol_squeeze() in binance_scanner.py."""
    cfg = CONFIG["squeeze_breakout"]
    min_needed = max(cfg["bb_length"], cfg["squeeze_lookback"], cfg["vol_lookback"]) + 10
    if len(df) < min_needed:
        return None

    bb = bollinger_bands(df["close"], cfg["bb_length"], cfg["bb_mult"])
    close = df["close"]
    vol = df["volume"]

    width_threshold = bb["width"].rolling(cfg["squeeze_lookback"]).quantile(cfg["squeeze_percentile"])
    if pd.isna(width_threshold.iloc[-2]):
        return None
    if not (bb["width"].iloc[-2] <= width_threshold.iloc[-2]):
        return None

    fresh_breakout = close.iloc[-1] > bb["upper"].iloc[-1] and close.iloc[-2] <= bb["upper"].iloc[-2]
    if not fresh_breakout:
        return None

    baseline = vol.iloc[-(cfg["vol_lookback"] + 1):-1].mean()
    vol_ratio = (vol.iloc[-1] / baseline) if baseline > 0 else 0.0
    if vol_ratio < cfg["vol_multiplier"]:
        return None

    candle_key = str(int(df["close_time"].iloc[-1]))
    sym_state = state.get(symbol, {})
    if sym_state.get("last_alert_candle") == candle_key:
        return None
    sym_state["last_alert_candle"] = candle_key
    state[symbol] = sym_state

    return {
        "symbol": symbol,
        "price": close.iloc[-1],
        "vol_ratio": vol_ratio,
        "width_pct": bb["width"].iloc[-1] * 100,
    }


async def _listen_live(symbols: list[str], interval: str, deadline: float,
                        history: dict[str, list[dict]], candle_limit: int,
                        state: dict, hits: list) -> None:
    streams = "/".join(f"{s.lower()}@kline_{interval}" for s in symbols)
    url = f"{BINANCE_WS_BASE}?streams={streams}"

    remaining = deadline - time.time()
    if remaining <= 0:
        print("No time left for live listening after seeding - increase max_runtime_seconds "
              "or shrink auto_top_n/symbol_whitelist.")
        return

    print(f"Connecting to Binance combined kline stream for {len(symbols)} symbols "
          f"({interval}), listening for {remaining:.0f}s...")
    async with websockets.connect(url, ping_interval=20, ping_timeout=60) as ws:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break

            msg = json.loads(raw)
            payload = msg.get("data") or {}
            k = payload.get("k")
            if not k or not k.get("x"):
                continue  # only act once a candle CLOSES

            symbol = payload["s"]
            history.setdefault(symbol, []).append(_kline_event_to_row(k))
            if len(history[symbol]) > candle_limit:
                history[symbol] = history[symbol][-candle_limit:]

            df = pd.DataFrame(history[symbol])
            hit = evaluate_squeeze_live(symbol, df, state)
            if hit:
                hits.append(hit)
                print(f"  LIVE hit: {hit['symbol']} (vol {hit['vol_ratio']:.2f}x)")

    print("Burst window closed, disconnecting.")


def run_websocket_burst() -> None:
    cfg = CONFIG["squeeze_breakout"]
    burst_cfg = cfg.get("websocket_burst", {"max_runtime_seconds": 240})
    max_runtime = burst_cfg.get("max_runtime_seconds", 240)
    start = time.time()
    deadline = start + max_runtime

    if cfg["symbol_whitelist"]:
        symbols = cfg["symbol_whitelist"]
        print(f"Using fixed symbol_whitelist ({len(symbols)} symbols).")
    else:
        print(f"No symbol_whitelist set - auto-selecting top {cfg['auto_top_n']} "
              f"USDT pairs by 24h volume...")
        symbols = get_top_symbols_by_volume(cfg["auto_top_n"], cfg["min_24h_volume_usdt"])
        print(f"  Watching: {', '.join(symbols[:10])}{' ...' if len(symbols) > 10 else ''}")

    state = load_state(SQUEEZE_STATE_FILE)
    hits: list = []

    async def _run():
        history = await _seed_history(symbols, cfg["interval"], cfg["candle_limit"])
        await _listen_live(symbols, cfg["interval"], deadline, history, cfg["candle_limit"], state, hits)

    try:
        asyncio.run(_run())
    except Exception as e:
        print(f"WebSocket session error: {e}")

    save_state(state, SQUEEZE_STATE_FILE)

    if not hits:
        print("No squeeze breakouts this run (live websocket window).")
        return

    header = "⚡ **Squeeze Breakout (live)**"
    lines = [
        f"{h['symbol']} | Vol Spike: {h['vol_ratio']:.2f}x | BB Width: {h['width_pct']:.2f}%"
        for h in hits
    ]
    msg = header + "\n" + "\n".join(lines)
    print(msg)
    send_discord(msg)


if __name__ == "__main__":
    run_websocket_burst()