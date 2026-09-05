"""
Free Binance scanner - configurable DEMA200 + SuperTrend + MA9/20 cross,
with Discord notifications. Designed to run on a schedule via GitHub
Actions, so it works even when your own laptop is off.

Optional add-on: "scalp_mode" - a separate 5m early-scalp radar
(volume spike + green candle + DEMA200 trend + SuperTrend bullish),
toggled independently via CONFIG["scalp_mode"]["enabled"]. It does not
touch or depend on the original DEMA/MA-cross logic in any way.

Optional add-on: "nouman_strategy" - a separate trend-pullback radar.
Requirement 1 (1h SuperTrend bullish) reuses the SAME supertrend() already
imported below. Requirement 2 (Smoothed Heiken Ashi candle color + RSI
cross) is new - see the "NOUMAN STRATEGY" section for the exact rules and
the assumption made about the "gray" candle state. Toggled independently
via CONFIG["nouman_strategy"]["enabled"]; own state file, own Discord
message. Does not touch or depend on the DEMA/MA-cross or scalp logic.
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from indicators import dema, supertrend, moving_average

BINANCE_BASE = "https://data-api.binance.vision"
STATE_FILE = Path(__file__).parent / "scanner_state.json"
NOUMAN_STATE_FILE = Path(__file__).parent / "nouman_state.json"
SQUEEZE_STATE_FILE = Path(__file__).parent / "squeeze_state.json"

# ============================================================
# CONFIG - edit these to match your Pine script settings
# ============================================================

CONFIG = {
    # ---- 1. Timeframe (your main focus: 15m) ----
    "interval": "15m",          # 1m, 5m, 15m, 1h, 4h, 1d ...
    "candle_limit": 500,
    "quote_asset": "USDT",      # Binance-only, USDT pairs
    "request_sleep": 0.15,

    # ---- 3. DEMA200 filter (on/off + min % above) ----
    # Set to False by default so only the Nouman Strategy messages send.
    # Flip back to True (along with ma_cross below) if you ever want the
    # original DEMA/MA-cross signals running alongside it again.
    "dema": {
        "enabled": False,
        "length": 200,
        "min_pct_above": 0.3,   # set to 0 to allow ANY close above DEMA
    },

    # SuperTrend (shared: used by the DEMA signal above AND reused by the
    # Nouman Strategy's requirement 1 further down)
    "supertrend": {
        "atr_length": 12,
        "multiplier": 3.0,
    },

    # ---- 2. MA9/MA20 cross (configurable lengths) ----
    # Also off by default - see the "dema" note above.
    "ma_cross": {
        "enabled": False,
        "fast_length": 9,
        "slow_length": 20,
        "type": "EMA",           # SMA, EMA, or WMA
    },

    # ---- 4. Gap confirmation for the MA cross (on/off, % or StdDev) ----
    "ma_gap": {
        "mode": "Off",            # "Off", "% Gap", or "Std Dev"
        "min_pct": 0.3,
        "stdev_length": 20,
        "stdev_multiplier": 1.0,
    },

    # ---- Volume spike (MA9/20 cross only) ----
    # All three are REQUIRED when enabled: any one failing suppresses the
    # alert entirely. "Weight" is expressed as strictness, not a blended
    # score - the 15m multiplier is intentionally harder to clear than the
    # 1h one, so it dominates without needing an opaque formula.
    "volume_spike": {
        "enabled": True,

        # 15-minute spike: the fire candle vs its own recent baseline.
        # Primary signal - matches your scan cadence, hardest bar to clear.
        "lookback_15m": 10,       # candles used to build the baseline
        "multiplier_15m": 1.2,    # fire candle's volume must be >= this x baseline

        # 1-hour spike: sum of the last ~1h of candles vs the same-sized
        # window before it. Secondary confirmation - easier bar to clear,
        # so it has less influence than the 15m check.
        "lookback_1h": 10,        # prior 1h windows averaged for the baseline
        "multiplier_1h": 1.2,

        # 24h USDT liquidity floor - a hard minimum, not a spike check.
        # Fetched once per run for every symbol (1 API call, not per-symbol)
        # and applied BEFORE candles are even fetched, so illiquid pairs
        # never reach the signal logic at all.
        "min_24h_volume_usdt": 3_000_000,
    },

    # ---- Squeeze (MA9/20 cross only) ----
    # INFORMATIONAL when enabled: checks whether MA9/MA20 were tight together
    # for `lookback` candles right before the cross. Shown in the Discord
    # message ("squeeze=yes/no") but never blocks the alert.
    "squeeze": {
        "enabled": True,
        "lookback": 10,       # candles checked immediately before the cross
        "max_pct": 0.15,      # MA9/MA20 gap must stay <= this the whole window
    },

    # ---- SCALP MODE (new, independent layer) ----
    # Master switch: when False, this entire block is skipped and the
    # script behaves EXACTLY as before. When True, it runs as a separate
    # pass after the main scan and posts its own Discord message - it does
    # not read or write ma_armed/dema_trade_taken state at all.
    #
    # Goal: catch early scalps by ranking symbols on 5m volume spike, but
    # ONLY among candidates that also show a green candle / price-up move
    # AND are still trending (above DEMA200, SuperTrend bullish) - same
    # trend filter as your main DEMA signal, just applied on 5m candles.
    "scalp_mode": {
        "enabled": False,        # <-- flip this on/off

        "interval": "5m",
        "candle_limit": 500,     # needs >= dema_length*2 candles of history
        "top_n": 5,              # only the top N ranked candidates get posted

        # 5m volume spike: fire candle vs its own recent baseline
        "vol_lookback": 6,
        "vol_multiplier": 1.5,

        # Price confirmation: candle must close green, and move at least
        # this much (0.0 = any green candle qualifies)
        "min_price_change_pct": 0.0,

        # Trend filter, same idea as the main DEMA200+SuperTrend signal,
        # computed on 5m candles. Reuses CONFIG["supertrend"] params.
        "dema_length": 200,
        "dema_min_pct_above": 0.3,
    },

    # ---- NOUMAN STRATEGY (new, independent layer) ----
    # Requirement 1: 1h SuperTrend must be bullish. Reuses the SAME
    # supertrend() import + CONFIG["supertrend"] params already defined
    # above ("the SuperTrend already in the scanner is good") - just
    # evaluated on the 1h chart specifically, regardless of whatever
    # CONFIG["interval"] the main scan up top is set to.
    #
    # Requirement 2: on a configurable signal timeframe (1h by default,
    # 30m optional), the "Smoothed Heiken Ashi Candles" indicator
    # (jackvmk, TradingView script ROokknI2) shows a GREEN or GRAY candle,
    # together with RSI crossing UP through a configurable level (52).
    #
    # IMPORTANT ASSUMPTION ABOUT "GRAY":
    # The original open-source jackvmk script is strictly 2-color
    # (red/green) - there is no gray state in the published Pine code.
    # To satisfy your "gray or green" rule, this scanner adds gray as a
    # doji/indecision detector: when the smoothed candle's body is a small
    # fraction of its own high-low range (< gray_body_ratio), it's
    # classified GRAY instead of GREEN/RED. If that doesn't match what you
    # see on your chart (e.g. you're running a modified copy of the
    # indicator with its own gray logic), tell me the exact rule/Pine code
    # and I'll swap it in - everything else here is unaffected.
    "nouman_strategy": {
        "enabled": True,          # <-- flip this on/off

        "signal_interval": "1h",  # "1h" or "30m" - the configurable TF
        "candle_limit": 500,

        # Liquidity floor, checked before any candles are fetched for
        # this strategy (independent of CONFIG["volume_spike"] above).
        "min_24h_volume_usdt": 3_000_000,

        # Smoothed Heiken Ashi (jackvmk) smoothing lengths - match these
        # to your chart's indicator settings if you changed them from
        # default (10/10).
        "ha_len1": 10,             # 1st EMA smoothing of raw OHLC
        "ha_len2": 10,             # 2nd EMA smoothing of the HA values
        "gray_body_ratio": 0.15,   # body/range below this => GRAY (see note above)

        # RSI cross-up
        "rsi_length": 14,
        "rsi_cross_level": 52,    # must cross UP through this level

        # Gray-candle sequence rule: if the signal candle is GRAY, the
        # candle immediately before it must NOT be GREEN - it must be
        # GRAY or RED. (No such restriction when the signal candle is
        # GREEN.)
        "gray_prev_candle_rule": True,

        # Optional 1h volume-spike filter - OFF by default. When on, the
        # trailing 1h volume (summed from signal_interval candles) must
        # be >= multiplier x the average of the previous `lookback` 1h
        # windows. The 1h volume itself is always shown in the message
        # regardless of this toggle.
        "volume_1h_filter": {
            "enabled": False,
            "lookback": 10,
            "multiplier": 1.2,
        },

        # Optional "rising volume" filter - OFF by default. When on,
        # each of the previous `lookback` closed candles on the signal
        # timeframe (1h or 30m, whichever signal_interval is set to) must
        # have STRICTLY higher volume than the one before it - i.e. a
        # clean, uninterrupted build-up in volume leading into the signal
        # candle. Any flat or lower step anywhere in that window fails it.
        "volume_increasing_filter": {
            "enabled": False,
            "lookback": 5,
        },
    },

    # ---- SQUEEZE BREAKOUT (new, independent - a LEADING signal) ----
    # Everything else in this file (DEMA/MA-cross, scalp_mode, Nouman
    # Strategy) is a trend-CONFIRMATION system: SuperTrend, DEMA, and MA
    # crosses only fire once a move is already underway - by design they
    # lag the actual pump. This module is different in kind: it looks for
    # a volatility contraction ("squeeze" - Bollinger Bands pinched
    # tight, meaning the coin has gone quiet) immediately followed by a
    # breakout above the bands on rising volume. That's the closest a
    # public-data technical signal gets to catching a move AS it starts
    # rather than after - it's still reactive, not predictive, and will
    # have more false positives than the slower signals above (that's
    # the fundamental trade-off of reacting earlier).
    #
    # OFF by default - it's a fast, noisier 1m/5m signal, meant to be
    # tuned before you trust it. Runs on a configurable small watchlist
    # by default (fewer symbols = each scan pass finishes in seconds, not
    # minutes - see the note on symbol_whitelist below).
    "squeeze_breakout": {
        "enabled": False,

        "interval": "1m",          # "1m" or "5m" - short by design
        "candle_limit": 300,

        # Only scan THESE symbols instead of the full ~500+ USDT pair
        # universe. For 1-5 minute scalping, looping through hundreds of
        # symbols with request_sleep between each call takes real
        # minutes - by the time the loop reaches symbol #300, several
        # minutes have passed since the scan started, which alone can
        # explain "the coin already pumped by the time I saw it," no
        # matter which indicator is used.
        #
        # Leave this EMPTY (the default) and it auto-selects the top
        # `auto_top_n` USDT pairs by 24h volume every run - you don't
        # have to guess which coins to watch; "which coins are liquid
        # right now" is answered by the API, not a prediction. Fill in
        # specific symbols here only if you want a fixed, hand-picked
        # list instead (e.g. ["BTCUSDT", "ETHUSDT", ...]) - that always
        # takes priority over auto-selection when non-empty.
        "symbol_whitelist": [],
        "auto_top_n": 50,                    # used only when whitelist is empty
        "min_24h_volume_usdt": 3_000_000,    # floor applied either way

        # Bollinger Bands (basis = SMA)
        "bb_length": 20,
        "bb_mult": 2.0,

        # "Squeeze" = current band width sits at/near the bottom of its
        # own recent range - i.e. the coin has been unusually quiet.
        "squeeze_lookback": 50,
        "squeeze_percentile": 0.20,   # width must be in the bottom 20% of the lookback window

        # Breakout = close crosses back above the upper band right after
        # being squeezed, confirmed by a volume pop vs recent baseline.
        "vol_lookback": 10,
        "vol_multiplier": 1.5,

        # Used only by the companion websocket_scanner.py (optional) -
        # how long a single live-monitoring burst runs before exiting
        # cleanly. Keep this comfortably under your cron interval (e.g.
        # 240s if you trigger every 5 minutes).
        "websocket_burst": {
            "max_runtime_seconds": 240,
        },
    },

    # ---- 5. Alert time windows (optional quiet hours / focus hours) ----
    # OFF by default - when off, alerts send any time, exactly as before.
    # When on, Discord messages are only actually SENT while the current
    # UTC time (converted to your local offset) falls inside one of the
    # windows below; everything still gets scanned and printed to the
    # console/log either way, just not pushed to Discord outside these
    # hours. This does NOT change what the market does - it only reduces
    # notification noise to the hours you've personally observed being
    # more active. Pre-filled below with the 4 Pakistan-time (UTC+5, no
    # DST) windows you described - edit freely, add/remove windows, or
    # change the offset for your own timezone.
    "alert_time_windows": {
        "enabled": False,
        "timezone_utc_offset": 5,   # PKT = UTC+5. Change for your timezone.
        "windows": [                # local HH:MM 24h, [start, end)
            ("05:00", "06:00"),
            ("13:00", "14:00"),
            ("18:00", "19:30"),
            ("20:30", "21:30"),
        ],
    },

    # ---- 6. Discord notifications (free, no bot needed) ----
    "discord_webhook_url": os.environ.get("DISCORD_WEBHOOK_URL", ""),
}


# ============================================================
# Binance data fetching
# ============================================================

def _interval_minutes(interval: str) -> int:
    """'15m' -> 15, '1h' -> 60, '4h' -> 240, '1d' -> 1440."""
    unit = interval[-1]
    n = int(interval[:-1])
    return {"m": n, "h": n * 60, "d": n * 1440}[unit]


def get_usdt_symbols() -> list[str]:
    r = requests.get(f"{BINANCE_BASE}/api/v3/exchangeInfo", timeout=15)
    r.raise_for_status()
    data = r.json()
    return [
        s["symbol"]
        for s in data["symbols"]
        if s["quoteAsset"] == CONFIG["quote_asset"]
        and s["status"] == "TRADING"
        and s["isSpotTradingAllowed"]
    ]


def get_24h_volumes() -> dict[str, float]:
    """One call for EVERY symbol's rolling 24h USDT volume - used as a
    liquidity floor, not fetched per-symbol."""
    r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=20)
    r.raise_for_status()
    return {d["symbol"]: float(d["quoteVolume"]) for d in r.json()}


def get_top_symbols_by_volume(n: int, min_24h_volume_usdt: float = 0.0,
                               volume_24h: dict | None = None) -> list[str]:
    """USDT pairs only, ranked by 24h quote volume (highest first),
    capped to the top n. Used to auto-build a watchlist for the
    Squeeze Breakout module instead of you having to guess/maintain one
    by hand - "which 50 coins are actually liquid right now" is
    something the API already answers, no prediction needed."""
    symbols = get_usdt_symbols()
    if volume_24h is None:
        volume_24h = get_24h_volumes()
    eligible = [s for s in symbols if volume_24h.get(s, 0.0) >= min_24h_volume_usdt]
    eligible.sort(key=lambda s: volume_24h.get(s, 0.0), reverse=True)
    return eligible[:n]


def get_klines(symbol: str, interval: str | None = None, limit: int | None = None) -> pd.DataFrame:
    """interval/limit default to CONFIG["interval"]/CONFIG["candle_limit"]
    so all existing call sites behave exactly as before. Scalp mode passes
    its own interval/limit explicitly."""
    r = requests.get(
        f"{BINANCE_BASE}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": interval or CONFIG["interval"],
            "limit": limit or CONFIG["candle_limit"],
        },
        timeout=15,
    )
    r.raise_for_status()
    raw = r.json()
    df = pd.DataFrame(
        raw,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ],
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


# ============================================================
# State persistence (one-shot-per-episode memory across runs)
# ============================================================

def load_state(path: Path = STATE_FILE) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    path.write_text(json.dumps(state, indent=2))


# ============================================================
# Discord notification
# ============================================================

def _in_alert_window(now_utc: datetime | None = None) -> bool:
    """True if alerts should be sent right now. Always True when the
    alert_time_windows feature is off (default) - existing behavior is
    unchanged unless you explicitly enable it."""
    cfg = CONFIG["alert_time_windows"]
    if not cfg["enabled"]:
        return True

    now_utc = now_utc or datetime.utcnow()
    local_time = (now_utc + timedelta(hours=cfg["timezone_utc_offset"])).time()

    for start_str, end_str in cfg["windows"]:
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
        if start <= end:
            if start <= local_time < end:
                return True
        else:  # window wraps past midnight, e.g. ("23:00", "01:00")
            if local_time >= start or local_time < end:
                return True
    return False


def send_discord(message: str) -> None:
    if not _in_alert_window():
        print("  (Outside configured alert_time_windows - message logged here but NOT sent to Discord)")
        return
    url = CONFIG["discord_webhook_url"]
    if not url:
        print("  (Discord not configured - set DISCORD_WEBHOOK_URL env var / secret)")
        return
    try:
        requests.post(url, json={"content": message}, timeout=10)
    except requests.RequestException as e:
        print(f"  Discord send failed: {e}")


# ============================================================
# Per-symbol evaluation (ORIGINAL - unchanged)
# ============================================================

def check_symbol(symbol: str, state: dict, volume_24h: dict | None = None) -> list[dict]:
    """Returns a list of hit dicts (can contain 0, 1, or 2 signals per symbol)."""
    dema_cfg = CONFIG["dema"]
    ma_cfg = CONFIG["ma_cross"]
    gap_cfg = CONFIG["ma_gap"]
    vol_cfg = CONFIG["volume_spike"]
    squeeze_cfg = CONFIG["squeeze"]
    volume_24h = volume_24h or {}

    candles_per_hour = max(1, 60 // _interval_minutes(CONFIG["interval"]))

    min_history = max(
        dema_cfg["length"] * 2 if dema_cfg["enabled"] else 0,
        ma_cfg["slow_length"] + gap_cfg["stdev_length"] if ma_cfg["enabled"] else 0,
        ma_cfg["slow_length"] + squeeze_cfg["lookback"] if ma_cfg["enabled"] and squeeze_cfg["enabled"] else 0,
        vol_cfg["lookback_15m"] + 1 if vol_cfg["enabled"] else 0,
        candles_per_hour * (vol_cfg["lookback_1h"] + 1) if vol_cfg["enabled"] else 0,
        50,
    )

    df = get_klines(symbol)
    if len(df) < min_history:
        return []

    df = df.iloc[:-1]  # drop the still-forming candle
    close = df["close"]

    sym_state = state.get(symbol, {
        "dema_trade_taken": False,
        "ma_armed": False,
        "ma_gap_fired": False,
        "squeeze_ok": None,
    })

    hits = []

    # ---- DEMA200 + SuperTrend signal ----
    if dema_cfg["enabled"]:
        dema_val = dema(close, dema_cfg["length"])
        st = supertrend(df, CONFIG["supertrend"]["atr_length"], CONFIG["supertrend"]["multiplier"])

        last_close = close.iloc[-1]
        last_dema = dema_val.iloc[-1]
        distance_pct = (last_close - last_dema) / last_dema * 100
        armed = distance_pct >= dema_cfg["min_pct_above"]
        st_bullish = st["direction"].iloc[-1] < 0

        if not armed:
            sym_state["dema_trade_taken"] = False
        else:
            if not sym_state["dema_trade_taken"] and st_bullish:
                sym_state["dema_trade_taken"] = True
                hits.append({
                    "symbol": symbol,
                    "type": "DEMA200 + SuperTrend BUY",
                    "price": last_close,
                    "detail": f"DEMA200={last_dema:.8f}, {distance_pct:.2f}% above",
                })

    # ---- MA9/20 cross signal ----
    if ma_cfg["enabled"]:
        fast_ma = moving_average(close, ma_cfg["fast_length"], ma_cfg["type"])
        slow_ma = moving_average(close, ma_cfg["slow_length"], ma_cfg["type"])

        crossed_up = fast_ma.iloc[-2] <= slow_ma.iloc[-2] and fast_ma.iloc[-1] > slow_ma.iloc[-1]
        crossed_down = fast_ma.iloc[-2] >= slow_ma.iloc[-2] and fast_ma.iloc[-1] < slow_ma.iloc[-1]

        if crossed_up:
            sym_state["ma_armed"] = True
            sym_state["ma_gap_fired"] = False

            if squeeze_cfg["enabled"]:
                # Were MA9/MA20 tight together for `lookback` candles right
                # before this cross candle? (window excludes the cross candle
                # itself - squeeze describes what came before it)
                gap_pct_series = (fast_ma - slow_ma).abs() / slow_ma * 100
                pre_cross = gap_pct_series.iloc[-(squeeze_cfg["lookback"] + 1):-1]
                sym_state["squeeze_ok"] = bool((pre_cross <= squeeze_cfg["max_pct"]).all())
            else:
                sym_state["squeeze_ok"] = None

        if crossed_down:
            sym_state["ma_armed"] = False
            sym_state["ma_gap_fired"] = False

        if sym_state["ma_armed"] and not sym_state["ma_gap_fired"]:
            gap_raw = fast_ma.iloc[-1] - slow_ma.iloc[-1]
            gap_pct = abs(gap_raw) / slow_ma.iloc[-1] * 100

            if gap_cfg["mode"] == "Off":
                gap_ok = True
            elif gap_cfg["mode"] == "% Gap":
                gap_ok = gap_pct >= gap_cfg["min_pct"]
            else:  # "Std Dev"
                gap_series = fast_ma - slow_ma
                mean = gap_series.rolling(gap_cfg["stdev_length"]).mean().iloc[-1]
                stdev = gap_series.rolling(gap_cfg["stdev_length"]).std().iloc[-1]
                gap_ok = gap_raw >= (mean + gap_cfg["stdev_multiplier"] * stdev)

            # Volume - REQUIRED when enabled: 15m spike (strict) AND 1h
            # spike (looser) both have to clear their own bar. The 24h
            # floor was already applied before this symbol was ever
            # fetched, so it's looked up here only for display.
            ratio_15m = ratio_1h = None
            if vol_cfg["enabled"]:
                vol = df["volume"]

                baseline_15m = vol.iloc[-(vol_cfg["lookback_15m"] + 1):-1].mean()
                ratio_15m = (vol.iloc[-1] / baseline_15m) if baseline_15m > 0 else 0.0
                ok_15m = ratio_15m >= vol_cfg["multiplier_15m"]

                hour_sums = vol.rolling(candles_per_hour).sum()
                current_hour_vol = hour_sums.iloc[-1]
                baseline_1h = hour_sums.iloc[-(vol_cfg["lookback_1h"] + 1):-1].mean()
                ratio_1h = (current_hour_vol / baseline_1h) if baseline_1h and baseline_1h > 0 else 0.0
                ok_1h = ratio_1h >= vol_cfg["multiplier_1h"]

                volume_ok = ok_15m and ok_1h
            else:
                volume_ok = True

            if gap_ok and volume_ok:
                sym_state["ma_gap_fired"] = True
                detail = f"gap={gap_pct:.3f}%"
                if vol_cfg["enabled"]:
                    vol_24h_m = volume_24h.get(symbol, 0.0) / 1_000_000
                    detail += f", vol15m={ratio_15m:.2f}x, vol1h={ratio_1h:.2f}x, vol24h={vol_24h_m:.1f}M"
                if squeeze_cfg["enabled"]:
                    detail += f", squeeze={'yes' if sym_state.get('squeeze_ok') else 'no'}"
                hits.append({
                    "symbol": symbol,
                    "type": "MA9/20 Cross BUY",
                    "price": close.iloc[-1],
                    "detail": detail,
                })

    state[symbol] = sym_state
    return hits


# ============================================================
# SCALP MODE (new, independent - no shared state with check_symbol)
# ============================================================

def check_symbol_scalp(symbol: str) -> dict | None:
    """Snapshot-style check for the 5m scalp radar. No armed/fired state
    across runs on purpose - this is meant to surface *current* early
    movers each run, then get ranked and trimmed to top_n in run_scan().
    Returns a candidate dict, or None if it fails any filter."""
    cfg = CONFIG["scalp_mode"]

    min_history = max(cfg["dema_length"] * 2, cfg["vol_lookback"] + 1, 50)
    df = get_klines(symbol, interval=cfg["interval"], limit=max(cfg["candle_limit"], min_history + 5))
    if len(df) < min_history:
        return None

    df = df.iloc[:-1]  # drop the still-forming candle
    close = df["close"]
    open_ = df["open"]
    vol = df["volume"]

    # -- 5m volume spike: fire candle vs its own recent baseline --
    baseline = vol.iloc[-(cfg["vol_lookback"] + 1):-1].mean()
    ratio = (vol.iloc[-1] / baseline) if baseline > 0 else 0.0
    if ratio < cfg["vol_multiplier"]:
        return None

    # -- green candle / price up % --
    last_open, last_close = open_.iloc[-1], close.iloc[-1]
    change_pct = (last_close - last_open) / last_open * 100
    if not (last_close > last_open and change_pct >= cfg["min_price_change_pct"]):
        return None

    # -- DEMA200 trend filter (5m) --
    dema_val = dema(close, cfg["dema_length"])
    last_dema = dema_val.iloc[-1]
    dema_pct = (last_close - last_dema) / last_dema * 100
    if dema_pct < cfg["dema_min_pct_above"]:
        return None

    # -- SuperTrend bullish (5m), same atr/multiplier as main config --
    st = supertrend(df, CONFIG["supertrend"]["atr_length"], CONFIG["supertrend"]["multiplier"])
    if not (st["direction"].iloc[-1] < 0):
        return None

    return {
        "symbol": symbol,
        "price": last_close,
        "ratio": ratio,
        "change_pct": change_pct,
        "dema_pct": dema_pct,
    }


def run_scalp_scan(symbols: list[str]) -> None:
    cfg = CONFIG["scalp_mode"]
    print(f"\nScalp mode: scanning {len(symbols)} pairs on {cfg['interval']}...")

    candidates = []
    for i, symbol in enumerate(symbols, 1):
        try:
            hit = check_symbol_scalp(symbol)
            if hit:
                candidates.append(hit)
        except Exception as e:
            print(f"  {symbol}: scalp skipped ({e})")
        time.sleep(CONFIG["request_sleep"])

        if i % 50 == 0:
            print(f"  ...{i}/{len(symbols)} scalp-scanned")

    candidates.sort(key=lambda c: c["ratio"], reverse=True)
    top = candidates[: cfg["top_n"]]

    if not top:
        print("  No scalp setups this run.")
        return

    lines = [f"🚀 **Scalp Setup** (5m vol+price+DEMA200+SuperTrend) - top {len(top)}"]
    for c in top:
        lines.append(
            f"{c['symbol']} @ {c['price']} - vol5m={c['ratio']:.2f}x, "
            f"chg={c['change_pct']:.2f}%, DEMA200 +{c['dema_pct']:.2f}%"
        )
    msg = "\n".join(lines)
    print(msg)
    send_discord(msg)


# ============================================================
# NOUMAN STRATEGY (new, independent - own state file, own message)
# ============================================================

def rsi(close: pd.Series, length: int) -> pd.Series:
    """Wilder's RSI (matches TradingView's ta.rsi)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-12)
    return 100 - (100 / (1 + rs))


def smoothed_heiken_ashi(df: pd.DataFrame, len1: int, len2: int, gray_body_ratio: float) -> pd.DataFrame:
    """Reimplementation of jackvmk's "Smoothed Heiken Ashi Candles v1"
    (TradingView ROokknI2): raw OHLC is EMA-smoothed (len1), converted to
    Heiken Ashi, then EMA-smoothed again (len2). The published script only
    outputs red/green (col = o2>c2 ? red : lime) - GRAY is an addition
    here, layered ONLY on top of GREEN: a candle that would be green but
    whose smoothed body is a small fraction of its own high-low range
    (< gray_body_ratio) is reclassified GRAY instead, representing a
    weakening/stalling uptrend candle. A RED candle stays RED no matter
    how small its body is - it never gets reclassified as GRAY. See the
    CONFIG note for why/how to adjust this.
    """
    o = df["open"].ewm(span=len1, adjust=False).mean()
    c = df["close"].ewm(span=len1, adjust=False).mean()
    h = df["high"].ewm(span=len1, adjust=False).mean()
    l = df["low"].ewm(span=len1, adjust=False).mean()

    ha_close = (o + h + l + c) / 4
    ha_open = pd.Series(index=df.index, dtype=float)
    ha_open.iloc[0] = (o.iloc[0] + c.iloc[0]) / 2
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2
    ha_high = pd.concat([h, ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([l, ha_open, ha_close], axis=1).min(axis=1)

    o2 = ha_open.ewm(span=len2, adjust=False).mean()
    c2 = ha_close.ewm(span=len2, adjust=False).mean()
    h2 = ha_high.ewm(span=len2, adjust=False).mean()
    l2 = ha_low.ewm(span=len2, adjust=False).mean()

    body = (c2 - o2).abs()
    rng = (h2 - l2).replace(0, 1e-12)
    body_ratio = body / rng

    # Base color exactly matches the original indicator's rule.
    color = pd.Series("RED", index=df.index, dtype=object)
    is_green = c2 >= o2
    color[is_green] = "GREEN"
    # Gray only overrides a GREEN candle with a small body - RED is never
    # touched, so a small red body still reports/alerts as RED.
    color[is_green & (body_ratio < gray_body_ratio)] = "GRAY"

    return pd.DataFrame({"o2": o2, "c2": c2, "h2": h2, "l2": l2, "color": color})


def _fmt_vol(n: float) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def check_symbol_nouman(symbol: str, state: dict, volume_24h: dict) -> dict | None:
    """Returns a hit dict if BOTH requirements fire on this run, else None."""
    cfg = CONFIG["nouman_strategy"]
    st_cfg = CONFIG["supertrend"]
    vol_cfg = cfg["volume_1h_filter"]
    vol_inc_cfg = cfg["volume_increasing_filter"]

    # ---- Requirement 1: 1h SuperTrend must be bullish ----
    df_st = get_klines(symbol, interval="1h", limit=max(300, st_cfg["atr_length"] * 5))
    if len(df_st) < st_cfg["atr_length"] * 3:
        return None
    df_st = df_st.iloc[:-1]  # drop the still-forming candle
    st = supertrend(df_st, st_cfg["atr_length"], st_cfg["multiplier"])
    if not (st["direction"].iloc[-1] < 0):
        return None

    # ---- Requirement 2: signal-timeframe candle color + RSI cross ----
    candles_per_hour = max(1, 60 // _interval_minutes(cfg["signal_interval"]))
    min_needed = max(cfg["ha_len1"], cfg["ha_len2"], cfg["rsi_length"], vol_inc_cfg["lookback"]) * 4 + 10
    df_sig = get_klines(symbol, interval=cfg["signal_interval"], limit=max(cfg["candle_limit"], min_needed))
    if len(df_sig) < min_needed:
        return None
    df_sig = df_sig.iloc[:-1]  # drop the still-forming candle

    ha = smoothed_heiken_ashi(df_sig, cfg["ha_len1"], cfg["ha_len2"], cfg["gray_body_ratio"])
    rsi_series = rsi(df_sig["close"], cfg["rsi_length"])

    last_color = ha["color"].iloc[-1]
    prev_color = ha["color"].iloc[-2] if len(ha) > 1 else None

    if last_color not in ("GREEN", "GRAY"):
        return None

    # Gray-candle sequence rule: previous candle must not be GREEN.
    if last_color == "GRAY" and cfg["gray_prev_candle_rule"] and prev_color == "GREEN":
        return None

    # RSI cross-UP: this is a genuine crossover, not a level check - the
    # PREVIOUS closed candle's RSI must be at/below the level and the
    # CURRENT (signal) candle's RSI must be above it. That's true whether
    # the previous value was 45, 51.9, or anything else <= level, so
    # "crossing from ~45 up through 52" and "crossing from 51 up through
    # 52" both satisfy this the same way.
    level = cfg["rsi_cross_level"]
    rsi_prev, rsi_now = rsi_series.iloc[-2], rsi_series.iloc[-1]
    crossed_up = rsi_prev <= level and rsi_now > level
    if not crossed_up:
        return None

    # ---- 1h volume: always computed for display; filter is optional ----
    vol_1h = df_sig["volume"].iloc[-candles_per_hour:].sum()
    if vol_cfg["enabled"]:
        hour_sums = df_sig["volume"].rolling(candles_per_hour).sum()
        baseline = hour_sums.iloc[-(vol_cfg["lookback"] + 1):-1].mean()
        ratio = (vol_1h / baseline) if baseline and baseline > 0 else 0.0
        if ratio < vol_cfg["multiplier"]:
            return None

    # ---- Optional: volume must rise every candle for the last N candles ----
    if vol_inc_cfg["enabled"]:
        recent_vols = df_sig["volume"].iloc[-vol_inc_cfg["lookback"]:]
        if len(recent_vols) < vol_inc_cfg["lookback"] or not recent_vols.diff().iloc[1:].gt(0).all():
            return None

    # ---- De-dup: only alert once per closed signal candle ----
    candle_key = str(int(df_sig["close_time"].iloc[-1]))
    sym_state = state.get(symbol, {})
    if sym_state.get("last_alert_candle") == candle_key:
        return None
    sym_state["last_alert_candle"] = candle_key
    state[symbol] = sym_state

    return {
        "symbol": symbol,
        "price": df_sig["close"].iloc[-1],
        "rsi_prev": rsi_prev,
        "rsi": rsi_now,
        "color": last_color,
        "vol_1h": vol_1h,
        "vol_24h": volume_24h.get(symbol, 0.0),
    }


def run_nouman_scan(symbols: list[str], volume_24h: dict) -> None:
    cfg = CONFIG["nouman_strategy"]
    print(f"\nNouman Strategy: scanning {len(symbols)} pairs "
          f"(1h SuperTrend + {cfg['signal_interval']} HA/RSI)...")

    state = load_state(NOUMAN_STATE_FILE)
    hits = []
    for i, symbol in enumerate(symbols, 1):
        try:
            hit = check_symbol_nouman(symbol, state, volume_24h)
            if hit:
                hits.append(hit)
        except Exception as e:
            print(f"  {symbol}: nouman skipped ({e})")
        time.sleep(CONFIG["request_sleep"])

        if i % 50 == 0:
            print(f"  ...{i}/{len(symbols)} nouman-scanned")

    save_state(state, NOUMAN_STATE_FILE)

    if not hits:
        print("  No Nouman Strategy signals this run.")
        return

    header = "🎯 **Nouman Strategy**"
    lines = [
        f"{h['symbol']} | 1h Vol: {_fmt_vol(h['vol_1h'])} USDT | "
        f"24h Vol: {_fmt_vol(h['vol_24h'])} USDT | "
        f"RSI: {h['rsi_prev']:.1f}→{h['rsi']:.1f} | "
        f"Candle: {h['color']} | SuperTrend: Bullish"
        for h in hits
    ]

    # The strategy name is printed ONCE at the top of the batch, not per
    # coin. Discord hard-caps messages at 2000 chars, so long batches are
    # split into multiple messages - only the first carries the plain
    # header, later chunks are marked "(cont'd)" so it's still obvious
    # they belong to the same run.
    chunk_lines: list[str] = []
    is_first_chunk = True

    def _flush():
        nonlocal chunk_lines, is_first_chunk
        if not chunk_lines:
            return
        title = header if is_first_chunk else f"{header} (cont'd)"
        msg = title + "\n" + "\n".join(chunk_lines)
        print(msg)
        send_discord(msg)
        chunk_lines = []
        is_first_chunk = False

    for line in lines:
        projected_len = len(header) + 1 + sum(len(l) + 1 for l in chunk_lines) + len(line) + 1
        if chunk_lines and projected_len > 1900:
            _flush()
        chunk_lines.append(line)
    _flush()


# ============================================================
# SQUEEZE BREAKOUT (new, independent - a LEADING signal, see CONFIG note)
# ============================================================

def bollinger_bands(close: pd.Series, length: int, mult: float) -> pd.DataFrame:
    basis = close.rolling(length).mean()
    std = close.rolling(length).std()
    upper = basis + mult * std
    lower = basis - mult * std
    width = (upper - lower) / basis
    return pd.DataFrame({"basis": basis, "upper": upper, "lower": lower, "width": width})


def check_symbol_squeeze(symbol: str, state: dict, volume_24h: dict) -> dict | None:
    cfg = CONFIG["squeeze_breakout"]
    min_needed = max(cfg["bb_length"], cfg["squeeze_lookback"], cfg["vol_lookback"]) + 10

    df = get_klines(symbol, interval=cfg["interval"], limit=max(cfg["candle_limit"], min_needed))
    if len(df) < min_needed:
        return None
    df = df.iloc[:-1]  # drop the still-forming candle

    bb = bollinger_bands(df["close"], cfg["bb_length"], cfg["bb_mult"])
    close = df["close"]
    vol = df["volume"]

    # Squeeze: the bar just before the breakout candle had a band width
    # in the bottom `squeeze_percentile` of its own recent history.
    width_threshold = bb["width"].rolling(cfg["squeeze_lookback"]).quantile(cfg["squeeze_percentile"])
    was_squeezed = bb["width"].iloc[-2] <= width_threshold.iloc[-2]
    if not was_squeezed:
        return None

    # Breakout: this candle closes above the upper band, the previous
    # one did not (a fresh break, not an already-extended move).
    fresh_breakout = close.iloc[-1] > bb["upper"].iloc[-1] and close.iloc[-2] <= bb["upper"].iloc[-2]
    if not fresh_breakout:
        return None

    # Volume confirmation.
    baseline = vol.iloc[-(cfg["vol_lookback"] + 1):-1].mean()
    vol_ratio = (vol.iloc[-1] / baseline) if baseline > 0 else 0.0
    if vol_ratio < cfg["vol_multiplier"]:
        return None

    # De-dup: only alert once per closed candle.
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
        "vol_24h": volume_24h.get(symbol, 0.0),
    }


def run_squeeze_scan(symbols: list[str], volume_24h: dict) -> None:
    cfg = CONFIG["squeeze_breakout"]
    print(f"\nSqueeze Breakout: scanning {len(symbols)} pairs on {cfg['interval']}...")

    state = load_state(SQUEEZE_STATE_FILE)
    hits = []
    for i, symbol in enumerate(symbols, 1):
        try:
            hit = check_symbol_squeeze(symbol, state, volume_24h)
            if hit:
                hits.append(hit)
        except Exception as e:
            print(f"  {symbol}: squeeze skipped ({e})")
        time.sleep(CONFIG["request_sleep"])

        if i % 50 == 0:
            print(f"  ...{i}/{len(symbols)} squeeze-scanned")

    save_state(state, SQUEEZE_STATE_FILE)

    if not hits:
        print("  No squeeze breakouts this run.")
        return

    header = "⚡ **Squeeze Breakout**"
    lines = [
        f"{h['symbol']} | 24h Vol: {_fmt_vol(h['vol_24h'])} USDT | "
        f"Vol Spike: {h['vol_ratio']:.2f}x | BB Width: {h['width_pct']:.2f}%"
        for h in hits
    ]
    msg = header + "\n" + "\n".join(lines)
    print(msg)
    send_discord(msg)


# ============================================================
# Main scan loop
# ============================================================

def run_scan() -> None:
    print(f"Fetching Binance {CONFIG['quote_asset']} pairs...")
    symbols_all = get_usdt_symbols()
    symbols = symbols_all

    volume_24h: dict[str, float] = {}
    vol_cfg = CONFIG["volume_spike"]
    nouman_cfg = CONFIG["nouman_strategy"]
    squeeze_cfg = CONFIG["squeeze_breakout"]
    if vol_cfg["enabled"] or nouman_cfg["enabled"] or squeeze_cfg["enabled"]:
        print("Fetching 24h volume for the liquidity floor (1 call, all symbols)...")
        volume_24h = get_24h_volumes()

    if vol_cfg["enabled"]:
        before = len(symbols)
        symbols = [s for s in symbols if volume_24h.get(s, 0.0) >= vol_cfg["min_24h_volume_usdt"]]
        floor_m = vol_cfg["min_24h_volume_usdt"] / 1_000_000
        print(f"  {before} pairs -> {len(symbols)} pairs clear the {floor_m:.1f}M 24h floor")

    print(f"Scanning {len(symbols)} pairs on {CONFIG['interval']} timeframe...\n")

    state = load_state()
    all_hits = []

    main_scan_active = CONFIG["dema"]["enabled"] or CONFIG["ma_cross"]["enabled"]
    if not main_scan_active:
        print("  (DEMA + MA-cross both disabled - skipping the main scan loop entirely)")
    else:
        for i, symbol in enumerate(symbols, 1):
            try:
                hits = check_symbol(symbol, state, volume_24h)
                for h in hits:
                    all_hits.append(h)
                    msg = f"[{h['type']}] {h['symbol']} @ {h['price']} ({h['detail']})"
                    print(msg)
                    send_discord(msg)
            except Exception as e:
                print(f"  {symbol}: skipped ({e})")
            time.sleep(CONFIG["request_sleep"])

            if i % 50 == 0:
                print(f"  ...{i}/{len(symbols)} scanned")

        save_state(state)
        print(f"\nDone. {len(all_hits)} fresh signal(s) found.")

    # ---- Scalp mode: fully separate pass, only runs if switched on ----
    if CONFIG["scalp_mode"]["enabled"]:
        run_scalp_scan(symbols)

    # ---- Nouman Strategy: fully separate pass, own liquidity floor ----
    if nouman_cfg["enabled"]:
        floor = nouman_cfg["min_24h_volume_usdt"]
        nouman_symbols = [s for s in symbols_all if volume_24h.get(s, 0.0) >= floor]
        run_nouman_scan(nouman_symbols, volume_24h)

    # ---- Squeeze Breakout: fully separate pass, own liquidity floor ----
    # Uses your fixed symbol_whitelist if you set one; otherwise
    # auto-selects the top auto_top_n USDT pairs by 24h volume so you
    # never have to hand-maintain a watchlist.
    if squeeze_cfg["enabled"]:
        if squeeze_cfg["symbol_whitelist"]:
            squeeze_symbols = squeeze_cfg["symbol_whitelist"]
        else:
            squeeze_symbols = get_top_symbols_by_volume(
                squeeze_cfg["auto_top_n"], squeeze_cfg["min_24h_volume_usdt"], volume_24h)
        run_squeeze_scan(squeeze_symbols, volume_24h)


if __name__ == "__main__":
    run_scan()