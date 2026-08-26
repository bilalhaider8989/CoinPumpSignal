# Free Binance Scanner - Setup Guide

Scans every Binance USDT pair for your strategy and notifies you on
Discord - runs on GitHub's free servers, so it keeps working even when
your laptop is off.

## What each requirement maps to

| # | Requirement | Where |
|---|---|---|
| 1 | Configurable timeframe (default 15m) | `CONFIG["interval"]` in `scanner.py` |
| 2 | MA9/20 cross, configurable lengths | `CONFIG["ma_cross"]` |
| 3 | DEMA200 on/off + min % above (0 allowed) | `CONFIG["dema"]` |
| 4 | MA gap filter: Off / % / Std Dev | `CONFIG["ma_gap"]` |
| 5 | Free notifications, works with laptop off | GitHub Actions + Discord webhook |

---

## Step 1 - Get a Discord webhook (2 minutes, free)

1. In any Discord server you're in (or create one just for yourself): go to
   **Server Settings → Integrations → Webhooks → New Webhook**.
2. Name it, pick the channel, click **Copy Webhook URL**. Keep this handy.

## Step 2 - Put this code in a GitHub repo (free)

1. Create a free GitHub account if you don't have one.
2. Create a **new repository** (can be private).
3. Upload these files, keeping the folder structure:
   ```
   scanner.py
   indicators.py
   requirements.txt
   .github/workflows/scan.yml
   ```

## Step 3 - Add your webhook as a secret

In your repo: **Settings → Secrets and variables → Actions → New repository secret**
- Name: `DISCORD_WEBHOOK_URL`
- Value: paste the URL from Step 1

## Step 4 - Configure the strategy

Open `scanner.py`, edit the `CONFIG` dict at the top:

```python
CONFIG = {
    "interval": "15m",              # 1. your timeframe

    "dema": {
        "enabled": True,            # 3. on/off switch
        "length": 200,
        "min_pct_above": 0.3,       # set to 0 for "any close above DEMA"
    },

    "ma_cross": {
        "enabled": True,
        "fast_length": 9,           # 2. configurable
        "slow_length": 20,          # 2. configurable
        "type": "EMA",              # SMA / EMA / WMA
    },

    "ma_gap": {
        "mode": "Off",              # 4. "Off" / "% Gap" / "Std Dev"
        "min_pct": 0.3,
        "stdev_length": 20,
        "stdev_multiplier": 1.0,
    },
}
```

Commit and push the change.

## Step 5 - Match the cron schedule to your timeframe

In `.github/workflows/scan.yml`, the schedule is `*/15 * * * *` (every 15
min) by default, matching `interval: "15m"`. If you change the timeframe
in `CONFIG`, update this line to match (e.g. `*/5 * * * *` for 5m,
`0 * * * *` for 1h). GitHub Actions cron has a soft minimum of ~5 minutes
and can run a few minutes late during high load - fine for this use case,
not suitable for sub-minute scalping.

## Step 6 - Turn it on

Push everything to GitHub. The workflow starts running automatically on
its schedule. To confirm it works right now without waiting:
**Actions tab → Coin Scanner → Run workflow** (manual trigger button).

You'll get a Discord message like:
```
[DEMA200 + SuperTrend BUY] BTCUSDT @ 61234.5 (DEMA200=60800.12, 0.71% above)
[MA9/20 Cross BUY] ETHUSDT @ 3011.2 (gap=0.412%)
```

## Notes

- Discord webhooks are 100% free, no bot/approval needed, no message limit
  that matters here.
- GitHub Actions free tier: public repos get unlimited scheduled minutes;
  private repos get 2,000 free minutes/month, and this job takes only a
  few minutes per run - won't come close to the limit at 15m intervals.
- This only reads market data and sends notifications - it never places
  real trades.
- Ported signals: DEMA200+SuperTrend and MA9/20 cross only. FBB exits,
  stop loss/take profit, and HPSU are not in this scanner - say the word
  if you want those added too.
