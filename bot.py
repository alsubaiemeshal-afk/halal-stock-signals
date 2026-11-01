"""
Halal Stock Signals
Mizan + Alpaca + Telegram

Alerts only. No automatic orders.
Fail-closed Sharia screening.
"""

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
from dotenv import load_dotenv


# =====================================
# SETTINGS
# =====================================

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

NY = ZoneInfo("America/New_York")

HALAL_FILE = ROOT / "approved_halal.csv"

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

ALPACA_API = "https://data.alpaca.markets/v2/stocks"
MIZAN_API = os.getenv(
    "MIZAN_API_URL",
    "https://askmizan.com/api/v1"
).rstrip("/")

ALPACA_FEED = os.getenv(
    "ALPACA_DATA_FEED", "iex"
)

MAX_REVIEW_AGE = int(
    os.getenv("MAX_SCREEN_AGE_DAYS", "30")
)

MAX_PURIFY_AGE = int(
    os.getenv("MAX_PURIFY_AGE_DAYS", "450")
)

HTTP = requests.Session()

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


# =====================================
# MARKET SESSION
# =====================================

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


# =====================================
# INDEPENDENT HALAL APPROVAL
# =====================================

def approved_stocks():
    result = []

    if not HALAL_FILE.exists():
        logging.error("approved_halal.csv missing")
        return result

    with HALAL_FILE.open(
        newline="",
        encoding="utf-8-sig"
    ) as f:

        reader = csv.DictReader(f)

        required = {
            "symbol",
            "reviewed_at",
            "source",
            "verified_pure"
        }

        if not required.issubset(
            reader.fieldnames or []
        ):
            logging.error("Invalid CSV columns")
            return result

        for row in reader:
            try:
                symbol = row["symbol"].strip().upper()
                source = row["source"].strip()

                verified = (
                    row["verified_pure"]
                    .strip()
                    .lower() == "true"
                )

                reviewed = datetime.strptime(
                    row["reviewed_at"],
                    "%Y-%m-%d"
                ).date()

                age = (
                    datetime.now(timezone.utc).date()
                    - reviewed
                ).days

                if not re.fullmatch(
                    r"[A-Z]{1,5}(\.[A-Z])?",
                    symbol
                ):
                    continue

                if not verified:
                    continue

                if not source:
                    continue

                if not 0 <= age <= MAX_REVIEW_AGE:
                    continue

                result.append(symbol)

            except (
                ValueError,
                KeyError,
                AttributeError,
                TypeError
            ):
                continue

    return sorted(set(result))


# =====================================
# MIZAN SHARIA CHECK
# =====================================

def mizan_check(symbol):
    """
    Requires:
    - permitted main business
    - four passing standards
    - no borderline verdict
    - latest available interest-income
      purification ratio exactly zero

    This is an additional conservative
    filter, not a full purity certificate.
    """

    try:
        response = HTTP.get(
            f"{MIZAN_API}/screen/{symbol}",
            timeout=20
        )

        response.raise_for_status()
        data = response.json()

        business = data.get("business", {})

        if business.get("main_business") != "permitted":
            return False

        # Mizan explicitly does not check
        # prohibited side-income here.
        if business.get("income_test") is None:
            return False

        screens = data.get("screens", [])

        standards = {
            "AAOIFI",
            "DJIM",
            "SP",
            "MSCI"
        }

        if {
            s.get("standard") for s in screens
        } != standards:
            return False

        for screen in screens:
            if screen.get("compliant") is not True:
                return False

            if screen.get("borderline") is not False:
                return False

        purify_response = HTTP.get(
            f"{MIZAN_API}/purify/{symbol}",
            timeout=20
        )

        purify_response.raise_for_status()
        purify = purify_response.json()

        ratio = purify.get("ratio")
        interest = purify.get("interest_income")
        period = purify.get("period_end")
        latest = purify.get("latest_annual_filing")

        if (
            ratio is None
            or interest is None
            or not period
            or not latest
        ):
            return False

        # A positive purification ratio
        # is not acceptable for this bot.
        if float(ratio) != 0:
            return False

        if float(interest) != 0:
            return False

        report_date = datetime.strptime(
            period, "%Y-%m-%d"
        ).date()

        latest_date = datetime.strptime(
            latest, "%Y-%m-%d"
        ).date()

        if report_date != latest_date:
            return False

        age = (
            datetime.now(timezone.utc).date()
            - report_date
        ).days

        if not 0 <= age <= MAX_PURIFY_AGE:
            return False

        logging.info(
            "Mizan checks passed: %s",
            symbol
        )

        return True

    except (
        requests.RequestException,
        ValueError,
        TypeError,
        KeyError
    ):
        logging.exception(
            "Mizan check failed: %s",
            symbol
        )
        return False


# =====================================
# ALPACA DATA
# =====================================

def get_bars(symbol, timeframe, start, end):
    response = HTTP.get(
        f"{ALPACA_API}/{symbol}/bars",
        headers=HEADERS,
        params={
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 10000,
            "feed": ALPACA_FEED,
            "adjustment": "raw"
        },
        timeout=25
    )

    response.raise_for_status()

    rows = response.json().get("bars", [])

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

    return (
        df.sort_values("time")
        .set_index("time")
    )


# =====================================
# INDICATORS
# =====================================

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

    result = 100 - 100 / (1 + rs)

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
    previous = df["close"].shift()

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous).abs(),
            (df["low"] - previous).abs()
        ],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


def calculate_vwap(df):
    typical = (
        df["high"]
        + df["low"]
        + df["close"]
    ) / 3

    volume = df["volume"].cumsum()

    return (
        (typical * df["volume"]).cumsum()
        / volume.replace(0, float("nan"))
    )


# =====================================
# FILTER MARKET SESSION
# =====================================

def filter_session(df, session):
    if df.empty:
        return df

    df = df.tz_convert(NY)

    today = datetime.now(NY).date()
    df = df[df.index.date == today].copy()

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


# =====================================
# SIGNAL ANALYSIS
# =====================================

def analyze(symbol, one, five, session):
    one = filter_session(one, session)
    five = filter_session(five, session)

    if len(one) < 35 or len(five) < 25:
        return None

    one["vwap"] = calculate_vwap(one)
    one["rsi"] = calculate_rsi(one["close"])
    one["atr"] = calculate_atr(one)

    one["rvol"] = (
        one["volume"]
        / one["volume"].shift(1).rolling(
            20, min_periods=20
        ).mean()
    )

    five["ema8"] = five["close"].ewm(
        span=8,
        adjust=False
    ).mean()

    five["ema20"] = five["close"].ewm(
        span=20,
        adjust=False
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

    conditions = (
        x["close"] > x["vwap"]
        and trend["ema8"] > trend["ema20"]
        and 50 <= x["rsi"] <= 70
        and x["rvol"] >= 1.5
        and x["atr"] > 0
    )

    if not conditions:
        return None

    previous_high = (
        one["high"].iloc[-6:-1].max()
    )

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


# =====================================
# TELEGRAM
# =====================================

def send_telegram(message):
    response = HTTP.post(
        (
            "https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/sendMessage"
        ),
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


# =====================================
# MAIN
# =====================================

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
            "No independently verified pure stocks"
        )
        return

    logging.info(
        "Scanning %d stocks in %s",
        len(stocks),
        session
    )

    end = (
        datetime.now(timezone.utc)
        - timedelta(seconds=90)
    )

    start = end - timedelta(days=3)

    for symbol in stocks:
        try:
            # Sharia checks happen first.
            if not mizan_check(symbol):
                logging.info(
                    "Sharia check rejected: %s",
                    symbol
                )
                continue

            one = get_bars(
                symbol,
                "1Min",
                start,
                end
            )

            five = get_bars(
                symbol,
                "5Min",
                start,
                end
            )

            result = analyze(
                symbol,
                one,
                five,
                session
            )

            if result:
                send_telegram(
                    format_alert(result)
                )

                logging.info(
                    "Alert sent: %s",
                    symbol
                )
            else:
                logging.info(
                    "No signal: %s",
                    symbol
                )

        except Exception:
            logging.exception(
                "Error scanning %s",
                symbol
            )

    logging.info("Scan completed")


if __name__ == "__main__":
    run()
    
