"""
Indicator math ported from the Pine script:
- DEMA (Double EMA)
- ATR (Wilder's RMA smoothing, matches Pine's ta.atr)
- SuperTrend
- Simple/Exponential MA helpers
"""

import pandas as pd
import numpy as np


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def dema(series: pd.Series, length: int) -> pd.Series:
    ema1 = ema(series, length)
    ema2 = ema(ema1, length)
    return 2.0 * ema1 - ema2


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Wilder's smoothing (RMA) - matches Pine's ta.atr
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def supertrend(df: pd.DataFrame, atr_length: int, multiplier: float) -> pd.DataFrame:
    """
    Returns a DataFrame with columns:
    - supertrend: the line value
    - direction: -1 = bullish, +1 = bearish (matches Pine's ta.supertrend convention)
    """
    hl2 = (df["high"] + df["low"]) / 2
    atr_val = atr(df, atr_length)

    upperband = hl2 + multiplier * atr_val
    lowerband = hl2 - multiplier * atr_val

    final_upper = upperband.copy()
    final_lower = lowerband.copy()
    direction = pd.Series(index=df.index, dtype=float)
    st = pd.Series(index=df.index, dtype=float)

    close = df["close"]

    for i in range(len(df)):
        if i == 0:
            direction.iloc[i] = 1  # start bearish by convention
            st.iloc[i] = final_upper.iloc[i]
            continue

        if upperband.iloc[i] < final_upper.iloc[i - 1] or close.iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = upperband.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]

        if lowerband.iloc[i] > final_lower.iloc[i - 1] or close.iloc[i - 1] < final_lower.iloc[i - 1]:
            final_lower.iloc[i] = lowerband.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]

        prev_dir = direction.iloc[i - 1]
        if prev_dir == 1 and close.iloc[i] > final_upper.iloc[i]:
            direction.iloc[i] = -1
        elif prev_dir == -1 and close.iloc[i] < final_lower.iloc[i]:
            direction.iloc[i] = 1
        else:
            direction.iloc[i] = prev_dir

        st.iloc[i] = final_lower.iloc[i] if direction.iloc[i] == -1 else final_upper.iloc[i]

    return pd.DataFrame({"supertrend": st, "direction": direction})


def moving_average(series: pd.Series, length: int, kind: str = "EMA") -> pd.Series:
    if kind == "SMA":
        return series.rolling(length).mean()
    if kind == "WMA":
        weights = np.arange(1, length + 1)
        return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)
    return ema(series, length)  # default EMA
