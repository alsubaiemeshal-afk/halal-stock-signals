
import os
import csv
import json
import logging
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# ======================================
# CONFIGURATION
# ======================================

ROOT = Path(__file__).resolve().parent
HALAL_FILE = ROOT / "approved_halal.csv"

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

DATA_URL = "https://data.alpaca.markets/v2/stocks"
DATA_FEED = "iex"

NY = ZoneInfo("America/New_York")

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

HTTP = requests.Session()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ======================================
# MARKET HOURS
# ======================================

def market_session():
    now = datetime.now(NY)

    if now.weekday() >= 5:
        return None

    minute = now.hour * 60 + now.minute

    if 240 <= minute < 570:
        return "PRE-MARKET"

    if 570 <= minute < 960:
        return "REGULAR"

    if 960 <= minute < 1200:
        return "AFTER-HOURS"

    return None


# ======================================
# VERIFIED HALAL STOCKS
# ======================================

def approved_stocks():
    stocks = []

    if not HALAL_FILE.exists():
        logging.warning("Halal file missing")
        return stocks

    with HALAL_FILE.open(
        newline="", encoding="utf-8-sig"
    ) as f:

        reader = csv.DictReader(f)

        required = {
            "symbol",
            "verified_pure",
            "reviewed_at",
            "source"
        }

        if not required.issubset(reader.fieldnames or []):
            logging.error("Invalid halal CSV columns")
            return stocks

        for row in reader:
            try:
                symbol = row["symbol"].strip().upper()

                if not re.fullmatch(
                    r"[A-Z]{1,5}(\.[A-Z])?", symbol
                ):
                    continue

                if row["verified_pure"].strip().lower() != "true":
                    continue

                if not row["source"].strip():
                    continue

                reviewed = datetime.strptime(
                    row["reviewed_at"], "%Y-%m-%d"
                ).date()

                age = (
                    datetime.now(timezone.utc).date()
                    - reviewed
                ).days

                if not 0 <= age <= 30:
                    continue

                stocks.append(symbol)

            except (ValueError, KeyError, AttributeError):
                continue

    return sorted(set(stocks))


# ======================================
# ALPACA MARKET DATA
# ======================================

def get_bars(symbol, timeframe, start, end):

    response = HTTP.get(
        f"{DATA_URL}/{symbol}/bars",
        headers=HEADERS,
        params={
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "feed": DATA_FEED,
            "adjustment": "raw",
            "limit": 10000
        },
        timeout=25
    )

    response.raise_for_status()

    payload = response.json()
    rows = payload.get("bars", [])

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).rename(
        columns={
            "t": "time",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume"
        }
    )

    df["time"] = pd.to_datetime(
        df["time"], utc=True
    )

    return df.sort_values("time").set_index("time")


# ======================================
# INDICATORS
# ======================================

def calculate_rsi(close, period=14):

    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    rs = avg_gain / avg_loss.replace(
        0, float("nan")
    )

    result = 100 - (100 / (1 + rs))

    result = result.mask(
        (avg_loss == 0) & (avg_gain > 0),
        100
    )

    result = result.mask(
        (avg_gain == 0) & (avg_loss > 0),
        0
    )

    return result


def calculate_atr(df, period=14):

    previous_close = df["close"].shift()

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    return true_range.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


def calculate_vwap(df):

    typical_price = (
        df["high"] + df["low"] + df["close"]
    ) / 3

    cumulative_volume = df["volume"].cumsum()

    return (
        (typical_price * df["volume"]).cumsum()
        / cumulative_volume.replace(0, float("nan"))
    )


# ======================================
# SESSION DATA
# ======================================

def filter_session(df, session):

    if df.empty:
        return df

    df = df.tz_convert(NY)

    today = datetime.now(NY).date()

    df = df[df.index.date == today]

    if session == "PRE-MARKET":
        return df.between_time(
            "04:00", "09:29"
        ).copy()

    if session == "REGULAR":
        return df.between_time(
            "09:30", "15:59"
        ).copy()

    if session == "AFTER-HOURS":
        return df.between_time(
            "16:00", "19:59"
        ).copy()

    return df.iloc[0:0].copy()


# ======================================
# SIGNAL ENGINE
# ======================================

def analyze(symbol, one, five, session):

    one = filter_session(one, session)
    five = filter_session(five, session)

    if len(one) < 35 or len(five) < 25:
        return None

    one["vwap"] = calculate_vwap(one)

    one["rsi"] = calculate_rsi(
        one["close"], 14
    )

    one["atr"] = calculate_atr(
        one, 14
    )

    one["rvol"] = (
        one["volume"]
        / one["volume"].shift(1).rolling(
            20, min_periods=20
        ).mean()
    )

    five["ema8"] = five["close"].ewm(
        span=8, adjust=False
    ).mean()

    five["ema20"] = five["close"].ewm(
        span=20, adjust=False
    ).mean()

    x = one.iloc[-1]
    trend = five.iloc[-1]

    values = [
        x["vwap"],
        x["rsi"],
        x["atr"],
        x["rvol"],
        trend["ema8"],
        trend["ema20"]
    ]

    if not all(pd.notna(v) for v in values):
        return None

    if not (
        x["close"] > x["vwap"]
        and trend["ema8"] > trend["ema20"]
        and 50 <= x["rsi"] <= 70
        and x["rvol"] >= 1.5
        and x["atr"] > 0
    ):
        return None

    previous_high = one["high"].iloc[-6:-1].max()

    if x["close"] <= previous_high:
        return None

    entry = float(x["close"])
    stop = entry - float(x["atr"]) * 1.5

    if stop <= 0:
        return None

    risk = entry - stop

    return {
        "symbol": symbol,
        "session": session,
        "time": one.index[-1].isoformat(),
        "entry": entry,
        "stop": stop,
        "target1": entry + risk * 1.5,
        "target2": entry + risk * 2,
        "rsi": float(x["rsi"]),
        "rvol": float(x["rvol"])
    }


# ======================================
# TELEGRAM
# ======================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    response = HTTP.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT,
            "text": message
        },
        timeout=20
    )

    response.raise_for_status()


def format_alert(result):

    return (
        "📈 HALAL STOCK SIGNAL\n\n"
        f"السهم: {result['symbol']}\n"
        f"الجلسة: {result['session']}\n\n"
        f"💰 الدخول: ${result['entry']:.2f}\n"
        f"🛑 وقف الخسارة: ${result['stop']:.2f}\n"
        f"🎯 الهدف الأول: ${result['target1']:.2f}\n"
        f"🎯 الهدف الثاني: ${result['target2']:.2f}\n\n"
        f"RSI: {result['rsi']:.1f}\n"
        f"RVOL: {result['rvol']:.2f}\n\n"
        "⚠️ إشارة فنية وليست ضمانًا للربح"
    )


# ======================================
# MAIN SCANNER
# ======================================

def run():

    if not all([
        ALPACA_KEY,
        ALPACA_SECRET,
        TELEGRAM_TOKEN,
        TELEGRAM_CHAT
    ]):
        raise RuntimeError(
            "Missing GitHub Secrets"
        )

    session = market_session()

    if not session:
        logging.info(
            "Outside US extended market hours"
        )
        return

    stocks = approved_stocks()

    if not stocks:
        logging.warning(
            "No verified pure halal stocks"
        )
        return

    logging.info(
        "Scanning %d stocks - %s",
        len(stocks),
        session
    )

    end = datetime.now(timezone.utc) - timedelta(
        seconds=90
    )

    start = end - timedelta(days=3)

    for symbol in stocks:

        try:
            one = get_bars(
                symbol, "1Min", start, end
            )

            five = get_bars(
                symbol, "5Min", start, end
            )

            result = analyze(
                symbol, one, five, session
            )

            if result:
                send_telegram(
                    format_alert(result)
                )

                logging.info(
                    "Signal sent: %s", symbol
                )

            else:
                logging.info(
                    "No signal: %s", symbol
                )

        except Exception:
            logging.exception(
                "Error scanning %s", symbol
            )

    logging.info("Scan completed")


if __name__ == "__main__":
    run()
