
import io
import json
import logging
import time
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

LOG = logging.getLogger("scanner")
HTTP = requests.Session()
NY = ZoneInfo("America/New_York")
SA = ZoneInfo("Asia/Riyadh")

NASDAQ_URLS = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

HALAL_FILE = "approved_halal.csv"
STATE_FILE = Path(".cache/telegram_state.json")

MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "40"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "1"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "100"))
MIN_RVOL = float(os.getenv("MIN_RVOL", "1.3"))
MIN_VOLUME = int(
    os.getenv("MIN_IEX_SESSION_VOLUME", "10000")
)

TEST_MODE = os.getenv(
    "TEST_MODE", "false"
).lower() == "true"

TIMEOUT = 25
SUMMARY_HOURS = 6
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
RUN_SECONDS = max(0, int(os.getenv("RUN_SECONDS", "210")))
ALERT_EXPIRY_DAYS = 7


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        LOG.warning("Telegram credentials missing")
        return False

    try:
        response = HTTP.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": message,
            },
            timeout=TIMEOUT
        )
        response.raise_for_status()

        return response.json().get("ok") is True

    except requests.RequestException as exc:
        LOG.error("Telegram delivery error: %s", exc)
        return False


def load_state():
    if not STATE_FILE.exists():
        return {
            "last_summary": None,
            "alerts": {}
        }

    try:
        with STATE_FILE.open(
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("Invalid state")

        if not isinstance(data.get("alerts"), dict):
            data["alerts"] = {}

        return data

    except (OSError, ValueError):
        LOG.warning("Invalid Telegram state")
        return {
            "last_summary": None,
            "alerts": {}
        }


def save_state(state):
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp = STATE_FILE.with_suffix(".tmp")

    with temp.open(
        "w", encoding="utf-8"
    ) as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temp, STATE_FILE)


def market_symbols():
    symbols = set()
    success = 0

    for url in NASDAQ_URLS:
        try:
            response = HTTP.get(
                url, timeout=TIMEOUT
            )
            response.raise_for_status()

            df = pd.read_csv(
                io.StringIO(response.text),
                sep="|",
                dtype=str
            )

            df.columns = df.columns.str.strip()

            column = (
                "Symbol"
                if "Symbol" in df.columns
                else "ACT Symbol"
            )

            if column not in df:
                raise ValueError("Missing symbol column")

            if "Test Issue" in df:
                df = df[
                    df["Test Issue"].fillna("").str.strip() == "N"
                ]

            if "ETF" in df:
                df = df[
                    df["ETF"].fillna("").str.strip() == "N"
                ]

            if "Security Name" in df:
                pattern = (
                    r"(?i)\b(?:warrants?|rights?|units?|"
                    r"preferred|depositary notes?|"
                    r"debentures?|bonds?)\b"
                )
                df = df[
                    ~df["Security Name"].fillna("").str.contains(
                        pattern, regex=True
                    )
                ]

            symbols.update(
                s for s in df[column].dropna().str.strip().str.upper()
                if re.fullmatch(r"[A-Z]{1,5}", s)
            )

            success += 1

        except Exception as exc:
            LOG.error("NASDAQ download failed: %s", exc)

    if success == 0:
        raise RuntimeError("All Nasdaq sources failed")

    LOG.info("NASDAQ symbol count: %d", len(symbols))
    return symbols


def approved_symbols():
    if not os.path.isfile(HALAL_FILE):
        LOG.warning("Missing approved halal file")
        return set()

    df = pd.read_csv(HALAL_FILE, dtype=str)

    if "symbol" not in df.columns:
        raise ValueError("Missing symbol column")

    return {
        s for s in df["symbol"].dropna().str.strip().str.upper()
        if re.fullmatch(r"[A-Z]{1,5}", s)
    }


def get_bars(symbol):
    end = now_utc()
    start = end - timedelta(days=14)

    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }

    params = {
        "symbols": symbol,
        "timeframe": "1Min",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 500,
        "feed": "iex",
        "adjustment": "raw",
        "sort": "desc",
    }

    response = HTTP.get(
        "https://data.alpaca.markets/v2/stocks/bars",
        headers=headers,
        params=params,
        timeout=TIMEOUT
    )
    response.raise_for_status()

    bars = response.json().get(
        "bars", {}
    ).get(symbol, [])

    if len(bars) < 40:
        return None

    df = pd.DataFrame(bars)

    df["t"] = pd.to_datetime(
        df["t"], utc=True
    )

    df = (
        df.sort_values("t")
        .drop_duplicates("t")
        .reset_index(drop=True)
    )

    # Exclude unfinished one-minute candles.
    cutoff = (
        pd.Timestamp.now(tz="UTC")
        .floor("min")
    )

    df = df[df["t"] < cutoff].copy()

    return df if len(df) >= 40 else None


def analyze(symbol):
    df = get_bars(symbol)

    if df is None:
        return None

    df["ema9"] = df["c"].ewm(
        span=9, adjust=False
    ).mean()

    df["ema21"] = df["c"].ewm(
        span=21, adjust=False
    ).mean()

    change = df["c"].diff()

    up = change.clip(lower=0).ewm(
        alpha=1 / 14, adjust=False
    ).mean()

    down = (-change.clip(upper=0)).ewm(
        alpha=1 / 14, adjust=False
    ).mean()

    rs = up / down.replace(0, float("nan"))

    df["rsi"] = 100 - 100 / (1 + rs)

    df.loc[
        (down == 0) & (up > 0),
        "rsi"
    ] = 100

    df.loc[
        (down == 0) & (up == 0),
        "rsi"
    ] = 50

    previous = df["c"].shift(1)

    tr = pd.concat([
        df["h"] - df["l"],
        (df["h"] - previous).abs(),
        (df["l"] - previous).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1 / 14, adjust=False
    ).mean()

    df["rvol"] = (
        df["v"]
        / df["v"].shift(1).rolling(20).mean()
    )

    dates = df["t"].dt.tz_convert(NY).dt.date

    typical = (
        df["h"] + df["l"] + df["c"]
    ) / 3

    df["vwap"] = (
        (typical * df["v"]).groupby(dates).cumsum()
        / df["v"].groupby(dates).cumsum().replace(
            0, float("nan")
        )
    )

    last = df.iloc[-1]

    required = [
        "ema9", "ema21", "rsi",
        "atr", "rvol", "vwap"
    ]

    if pd.isna(last[required]).any():
        return None

    price = float(last["c"])

    day_volume = int(
        df.loc[dates == dates.iloc[-1], "v"].sum()
    )

    qualifies = (
        MIN_PRICE <= price <= MAX_PRICE
        and day_volume >= MIN_VOLUME
        and price > last["vwap"]
        and last["ema9"] > last["ema21"]
        and 50 <= last["rsi"] <= 70
        and last["rvol"] >= MIN_RVOL
        and last["atr"] > 0
    )

    age_minutes = (
        now_utc() - last["t"].to_pydatetime()
    ).total_seconds() / 60

    return {
        "symbol": symbol,
        "price": price,
        "bar": last["t"].isoformat(),
        "age": age_minutes,
        "rsi": float(last["rsi"]),
        "rvol": float(last["rvol"]),
        "atr": float(last["atr"]),
        "qualifies": bool(qualifies)
    }


def signal_message(result):
    price = result["price"]
    atr = result["atr"]

    # Indicative technical levels, not guaranteed fills.
    stop = max(0.01, price - 1.5 * atr)
    risk = price - stop

    target1 = price + risk
    target2 = price + 2 * risk

    risk_pct = risk / price * 100

    candle = parse_time(result["bar"])
    candle_text = (
        candle.astimezone(SA).strftime("%Y-%m-%d %H:%M")
        if candle else result["bar"]
    )

    return (
        "🚨 فرصة مضاربة جديدة — سهم مجتاز للفحص\n\n"
        f"📈 السهم: {result['symbol']}\n"
        f"💵 السعر المرجعي: ${price:.2f}\n"
        f"🎯 الدخول الاسترشادي: ${price:.2f}\n"
        f"🛑 وقف الخسارة الاسترشادي: ${stop:.2f}\n"
        f"🎯 الهدف الأول: ${target1:.2f}\n"
        f"🎯 الهدف الثاني: ${target2:.2f}\n"
        f"⚠️ المسافة لوقف الخسارة: {risk_pct:.2f}%\n\n"
        f"RSI: {result['rsi']:.1f}\n"
        f"RVOL: {result['rvol']:.2f}\n"
        f"ATR: ${atr:.3f}\n"
        f"🕒 شمعة الإشارة بتوقيت السعودية: {candle_text}\n\n"
        "المستويات تقديرية مبنية على ATR، "
        "وليست أوامر منفذة أو ضمانًا للسعر.\n"
        "بيانات IEX فقط."
    )


def summary_message(
    market_count,
    approved_count,
    eligible_count,
    analyzed,
    errors,
    fresh_count,
    historical_count
):
    local_time = now_utc().astimezone(SA).strftime(
        "%Y-%m-%d %H:%M"
    )

    return (
        "📊 التقرير الدوري لبوت الأسهم\n\n"
        f"🕒 التوقيت السعودي: {local_time}\n"
        f"📋 الأسهم الأمريكية المكتشفة: {market_count}\n"
        f"✅ المجتازة للفحص الشرعي الآلي: {approved_count}\n"
        f"🔎 المؤهلة للتحليل: {eligible_count}\n"
        f"📈 الأسهم المحللة: {analyzed}\n"
        f"🚨 الفرص الجديدة: {fresh_count}\n"
        f"📉 مطابقات قديمة: {historical_count}\n"
        f"⚠️ أخطاء التحليل: {errors}\n\n"
        "🧪 وضع الاختبار: "
        + ("مفعّل" if TEST_MODE else "غير مفعّل")
        + "\nلا يوجد تنفيذ تلقائي للصفقات."
    )


def main():
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise RuntimeError(
            "Set Alpaca credentials in GitHub Secrets"
        )

    state = load_state()

    # Remove old alert IDs to keep state small.
    expiry = now_utc() - timedelta(
        days=ALERT_EXPIRY_DAYS
    )

    state["alerts"] = {
        key: value
        for key, value in state["alerts"].items()
        if (
            parse_time(value) is not None
            and parse_time(value) >= expiry
        )
    }

    market = market_symbols()
    halal = approved_symbols()
    eligible = sorted(market & halal)

    LOG.info(
        "Approved=%d eligible=%d",
        len(halal),
        len(eligible)
    )

    analyzed = 0
    errors = 0
    fresh_count = 0
    historical_count = 0

    for symbol in eligible[:MAX_SYMBOLS]:
        try:
            result = analyze(symbol)

            if result is None:
                continue

            analyzed += 1

            if not result["qualifies"]:
                continue

            if result["age"] > 4:
                historical_count += 1
                continue

            alert_id = (
                f"{symbol}|{result['bar']}"
            )

            if alert_id in state["alerts"]:
                continue

            if telegram(signal_message(result)):
                state["alerts"][alert_id] = (
                    now_utc().isoformat()
                )
                fresh_count += 1

                # Persist successful sends immediately.
                save_state(state)
            else:
                errors += 1

        except Exception as exc:
            errors += 1
            LOG.error("%s: %s", symbol, exc)

    last_summary = parse_time(
        state.get("last_summary")
    )

    summary_due = (
        last_summary is None
        or now_utc() - last_summary
        >= timedelta(hours=SUMMARY_HOURS)
    )

    if summary_due:
        message = summary_message(
            len(market),
            len(halal),
            len(eligible),
            analyzed,
            errors,
            fresh_count,
            historical_count
        )

        if telegram(message):
            state["last_summary"] = (
                now_utc().isoformat()
            )
        else:
            LOG.warning("Summary not delivered")

    save_state(state)

    LOG.info(
        "Analyzed=%d Errors=%d Fresh=%d Old=%d",
        analyzed,
        errors,
        fresh_count,
        historical_count
    )


if __name__ == "__main__":
    deadline = time.monotonic() + RUN_SECONDS
    while True:
        try:
            main()
        except Exception:
            LOG.exception("Scanner iteration failed")
        if time.monotonic() + POLL_SECONDS >= deadline:
            break
        time.sleep(POLL_SECONDS)
