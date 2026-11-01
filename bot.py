
import os
import io
import time
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import pandas as pd


# ==========================================
# SETTINGS
# ==========================================

NASDAQ_URL = (
    "https://www.nasdaqtrader.com/"
    "dynamic/SymDir/nasdaqlisted.txt"
)

OTHER_URL = (
    "https://www.nasdaqtrader.com/"
    "dynamic/SymDir/otherlisted.txt"
)

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HALAL_FILE = "approved_halal.csv"
SYMBOLS_FILE = "us_stocks.csv"

MIN_PRICE = 1.0
MAX_PRICE = 30.0
MIN_DAILY_VOLUME = 500000
MIN_RVOL = 1.5

SCAN_INTERVAL = 300
MAX_SYMBOLS_PER_CYCLE = 100

NY = ZoneInfo("America/New_York")

session = requests.Session()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

sent_signals = set()


# ==========================================
# TELEGRAM
# ==========================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram credentials missing")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    response = session.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        },
        timeout=20
    )

    response.raise_for_status()
    return response.json().get("ok", False)


# ==========================================
# NASDAQ SYMBOLS
# ==========================================

def download_symbols(url):
    response = session.get(url, timeout=30)
    response.raise_for_status()

    df = pd.read_csv(
        io.StringIO(response.text),
        sep="|",
        dtype=str
    )

    df.columns = df.columns.str.strip()

    if "Symbol" in df.columns:
        symbol_col = "Symbol"
    elif "ACT Symbol" in df.columns:
        symbol_col = "ACT Symbol"
    else:
        raise ValueError("Symbol column missing")

    df = df[df[symbol_col].notna()].copy()

    if "Test Issue" in df.columns:
        df = df[
            df["Test Issue"].str.strip() == "N"
        ]

    if "ETF" in df.columns:
        df = df[
            df["ETF"].str.strip() == "N"
        ]

    if "Security Name" in df.columns:
        excluded = (
            r"(?i)\b(?:warrants?|rights?|units?|"
            r"preferred|depositary|notes?|"
            r"debentures?|bonds?)\b"
        )

        df = df[
            ~df["Security Name"]
            .fillna("")
            .str.contains(excluded, regex=True)
        ]

    symbols = set()

    for value in df[symbol_col]:
        symbol = str(value).strip().upper()

        if symbol.isalpha() and len(symbol) <= 5:
            symbols.add(symbol)

    return symbols


def get_market_symbols():
    symbols = set()

    for url in [NASDAQ_URL, OTHER_URL]:
        try:
            symbols.update(download_symbols(url))
        except Exception as error:
            logging.error("NASDAQ error: %s", error)

    if symbols:
        pd.DataFrame({
            "symbol": sorted(symbols)
        }).to_csv(SYMBOLS_FILE, index=False)

    return symbols


# ==========================================
# HALAL FILTER
# ==========================================

def load_halal_symbols():
    if not os.path.exists(HALAL_FILE):
        pd.DataFrame(
            columns=["symbol"]
        ).to_csv(HALAL_FILE, index=False)

        return set()

    df = pd.read_csv(HALAL_FILE, dtype=str)

    if "symbol" not in df.columns:
        raise ValueError(
            "approved_halal.csv must contain symbol"
        )

    return {
        str(s).strip().upper()
        for s in df["symbol"].dropna()
        if str(s).strip()
    }


# ==========================================
# ALPACA DATA
# ==========================================

def get_bars(symbol):
    url = (
        "https://data.alpaca.markets/"
        "v2/stocks/bars"
    )

    now = datetime.now(timezone.utc)

    params = {
        "symbols": symbol,
        "timeframe": "5Min",
        "start": (
            now - timedelta(days=10)
        ).isoformat(),
        "end": now.isoformat(),
        "limit": 1000,
        "feed": "iex",
        "sort": "asc",
        "adjustment": "raw"
    }

    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET
    }

    response = session.get(
        url,
        headers=headers,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    bars = response.json().get(
        "bars", {}
    ).get(symbol, [])

    if len(bars) < 50:
        return None

    df = pd.DataFrame(bars)

    df["t"] = pd.to_datetime(
        df["t"], utc=True
    )

    return df.sort_values("t").reset_index(drop=True)


# ==========================================
# INDICATORS
# ==========================================

def indicators(df):
    df = df.copy()

    df["ema9"] = df["c"].ewm(
        span=9, adjust=False
    ).mean()

    df["ema21"] = df["c"].ewm(
        span=21, adjust=False
    ).mean()

    delta = df["c"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1/14, adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1/14, adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(
        0, float("nan")
    )

    df["rsi"] = 100 - 100 / (1 + rs)

    df.loc[
        (avg_loss == 0) & (avg_gain > 0),
        "rsi"
    ] = 100

    previous_close = df["c"].shift(1)

    tr = pd.concat([
        df["h"] - df["l"],
        (df["h"] - previous_close).abs(),
        (df["l"] - previous_close).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.ewm(
        alpha=1/14, adjust=False
    ).mean()

    df["rvol"] = (
        df["v"] /
        df["v"].shift(1).rolling(20).mean()
    )

    dates = df["t"].dt.tz_convert(NY).dt.date

    typical = (
        df["h"] + df["l"] + df["c"]
    ) / 3

    pv = typical * df["v"]

    df["vwap"] = (
        pv.groupby(dates).cumsum() /
        df["v"].groupby(dates)
        .cumsum().replace(0, float("nan"))
    )

    return df


# ==========================================
# MARKET HOURS
# ==========================================

def market_open():
    now = datetime.now(NY)

    if now.weekday() >= 5:
        return False

    minutes = now.hour * 60 + now.minute

    return 570 <= minutes < 960


# ==========================================
# ANALYSIS
# ==========================================

def analyze(symbol):
    df = get_bars(symbol)

    if df is None:
        return None

    # Exclude unfinished candles
    now = pd.Timestamp.now(tz="UTC")

    df = df[
        df["t"] + pd.Timedelta(minutes=5)
        <= now
    ].copy()

    if len(df) < 50:
        return None

    df = indicators(df)

    last = df.iloc[-1]

    local_time = last["t"].tz_convert(NY)
    current_ny = datetime.now(NY)

    if local_time.date() != current_ny.date():
        return None

    if not 570 <= (
        local_time.hour * 60 + local_time.minute
    ) < 960:
        return None

    if (
        now - last["t"]
        > pd.Timedelta(minutes=15)
    ):
        return None

    price = float(last["c"])

    if not MIN_PRICE <= price <= MAX_PRICE:
        return None

    session_dates = (
        df["t"].dt.tz_convert(NY).dt.date
    )

    daily_volume = df.loc[
        session_dates == local_time.date(),
        "v"
    ].sum()

    if daily_volume < MIN_DAILY_VOLUME:
        return None

    required = [
        "vwap", "ema9", "ema21",
        "rsi", "rvol", "atr"
    ]

    if pd.isna(last[required]).any():
        return None

    if not (
        price > last["vwap"]
        and last["ema9"] > last["ema21"]
        and 50 <= last["rsi"] <= 70
        and last["rvol"] >= MIN_RVOL
        and last["atr"] > 0
    ):
        return None

    atr = float(last["atr"])

    return {
        "symbol": symbol,
        "bar_time": last["t"].isoformat(),
        "entry": price,
        "stop": max(0.01, price - 1.5 * atr),
        "target": price + 3 * atr,
        "rsi": float(last["rsi"]),
        "rvol": float(last["rvol"]),
        "volume": int(daily_volume)
    }


# ==========================================
# SIGNAL MESSAGE
# ==========================================

def format_signal(s):
    return (
        "📈 NASDAQ TRADING SIGNAL\n\n"
        f"Stock: {s['symbol']}\n"
        "Sharia: Approved whitelist\n\n"
        f"Entry: ${s['entry']:.2f}\n"
        f"Stop Loss: ${s['stop']:.2f}\n"
        f"Target: ${s['target']:.2f}\n\n"
        f"RSI: {s['rsi']:.1f}\n"
        f"RVOL: {s['rvol']:.2f}\n"
        f"IEX Volume: {s['volume']:,}\n\n"
        "⚠️ Technical alert only."
    )


# ==========================================
# MAIN SCANNER
# ==========================================

def scan():
    global sent_signals

    if not market_open():
        logging.info("US regular market is closed")
        return

    market_symbols = get_market_symbols()
    halal_symbols = load_halal_symbols()

    eligible = sorted(
        market_symbols.intersection(halal_symbols)
    )

    logging.info(
        "Market: %d | Halal: %d | Eligible: %d",
        len(market_symbols),
        len(halal_symbols),
        len(eligible)
    )

    if not eligible:
        logging.warning(
            "No approved halal stocks. "
            "Signals disabled."
        )
        return

    today = datetime.now(NY).date()

    sent_signals = {
        key for key in sent_signals
        if key[0] == today
    }

    for symbol in eligible[:MAX_SYMBOLS_PER_CYCLE]:
        try:
            signal = analyze(symbol)

            if signal is None:
                continue

            key = (today, symbol)

            if key in sent_signals:
                continue

            if send_telegram(format_signal(signal)):
                sent_signals.add(key)
                logging.info("Signal sent: %s", symbol)

        except Exception as error:
            logging.error(
                "Error for %s: %s",
                symbol, error
            )

        time.sleep(1)


def main():
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise RuntimeError(
            "Missing Alpaca API keys"
        )

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Missing Telegram credentials"
        )

    logging.info("NASDAQ HALAL BOT STARTED")

    send_telegram(
        "🤖 NASDAQ Halal Scanner Started\n"
        "NASDAQ Trader connected\n"
        "Alpaca IEX enabled\n"
        "Halal whitelist required\n"
        "Signals only - no auto trading"
    )

    while True:
        try:
            scan()
        except KeyboardInterrupt:
            break
        except Exception:
            logging.exception("Scanner error")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
