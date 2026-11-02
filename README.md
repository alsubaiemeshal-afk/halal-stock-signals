# Halal Stock Signals — fast free polling

## Setup
1. In GitHub repository Settings → Secrets and variables → Actions, set ALPACA_API_KEY, ALPACA_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. Never commit secrets.
2. Enable Actions and run **Halal Stock Signals 24H** manually once.
3. Inspect logs: `Approved` and `eligible` must both be above zero. The halal approval file starts empty intentionally and is populated only by verified screening; if sources fail, no signals are sent.
4. Confirm Telegram test workflow sends a message before relying on alerts.

## Operation
- GitHub Actions requests a run every 5 minutes (actual start may be delayed or skipped). Each run polls roughly every 60 seconds for up to ~3.5 minutes. There are gaps between runs.
- Uses Alpaca **IEX** 1-minute candles, not consolidated US market quotes. Free data may omit trades, have delays, and rate limits. Do not treat this as guaranteed real-time coverage.
- Screens up to 40 **verified** symbols per pass; 40 symbols x 4 passes per run can consume many API calls. Reduce MAX_SYMBOLS or increase POLL_SECONDS if rate limited.
- Sends alerts only on technical criteria and fresh completed candles. No signal is guaranteed; no orders are placed.
- TEST_MODE is false in workflow, so no test-mode alerts are presented as trade signals.
- GitHub cache is best-effort; notification deduplication may not persist across all runs.
- Run the halal screening workflow and check source freshness before relying on purity status.

For true continuous second-level streaming, move to an always-on host and use a licensed real-time feed.


## Important diagnostic / safety
- `python diagnose.py` reports missing configuration and whether the approved universe is empty, without printing secrets.
- This project deliberately **does not** label unverified stocks halal. `HALAL_SOURCE_VERIFIED=false` prevents trade alerts. Only set it true after independently validating the source, methodology, and freshness of the screening data.
- The default screening workflow is not an independent guarantee of Sharia compliance.
- A genuine prior-20-bar breakout is required; this fixes the previous resistance comparison.
- No alerts outside the US regular trading session; premarket/after-hours requires a separately validated data and strategy change.
- GitHub Actions is scheduled, not continuously real-time; actual executions can be delayed.
