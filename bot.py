"""Conservative halal stock alerts. Alerts only; no automatic trades.

Requires a manually reviewed approved_halal.csv with columns:
symbol,reviewed_at,source,verified_pure

A symbol is eligible only if its approval is recent AND Mizan provides
complete, current, zero-purification evidence. This is not a fatwa or a
certificate of absolute Sharia purity.
"""
import csv
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
NY = ZoneInfo('America/New_York')
HALAL_FILE = ROOT / 'approved_halal.csv'
STATE_FILE = ROOT / 'alert_state.json'
ALPACA_KEY = os.getenv('ALPACA_API_KEY', '')
ALPACA_SECRET = os.getenv('ALPACA_SECRET_KEY', '')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT = os.getenv('TELEGRAM_CHAT_ID', '')
ALPACA_API = 'https://data.alpaca.markets/v2/stocks'
MIZAN_API = os.getenv('MIZAN_API_URL', 'https://askmizan.com/api/v1').rstrip('/')
ALPACA_FEED = os.getenv('ALPACA_DATA_FEED', 'iex')
MAX_REVIEW_AGE = int(os.getenv('MAX_SCREEN_AGE_DAYS', '30'))
MAX_PURIFY_AGE = int(os.getenv('MAX_PURIFY_AGE_DAYS', '450'))
HTTP = requests.Session()
HEADERS = {'APCA-API-KEY-ID': ALPACA_KEY, 'APCA-API-SECRET-KEY': ALPACA_SECRET}
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('halal-bot')


def market_session(now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    if now.weekday() >= 5:
        return None
    t = now.time()
    if clock_time(4) <= t < clock_time(9, 30):
        return 'PRE'
    if clock_time(9, 30) <= t < clock_time(16):
        return 'REGULAR'
    if clock_time(16) <= t < clock_time(20):
        return 'POST'
    return None


def approved_stocks():
    if not HALAL_FILE.exists():
        LOG.warning('Missing approved_halal.csv')
        return []
    symbols = []
    today = datetime.now(timezone.utc).date()
    with HALAL_FILE.open(newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            symbol = (row.get('symbol') or '').strip().upper()
            if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,9}', symbol):
                continue
            if (row.get('verified_pure') or '').strip().lower() not in ('true', '1', 'yes'):
                continue
            if not (row.get('source') or '').strip():
                continue
            try:
                reviewed = datetime.strptime((row.get('reviewed_at') or '').strip(), '%Y-%m-%d').date()
            except ValueError:
                continue
            age = (today - reviewed).days
            if 0 <= age <= MAX_REVIEW_AGE:
                symbols.append(symbol)
    return sorted(set(symbols))


def mizan_check(symbol):
    """Fail closed. Mizan income_test must explicitly confirm 'permitted'.

    Mizan may instead report 'not checked'; in that case this rejects the
    symbol. Do not loosen this check just to get alerts.
    """
    try:
        r = HTTP.get(f'{MIZAN_API}/screen/{symbol}', timeout=20)
        r.raise_for_status()
        data = r.json()
        business = data.get('business') or {}
        if business.get('main_business') != 'permitted':
            return False
        if business.get('income_test') != 'permitted':
            LOG.info('%s: prohibited-income business test unverified', symbol)
            return False
        screens = data.get('screens') or []
        standards = {'AAOIFI', 'DJIM', 'SP', 'MSCI'}
        if len(screens) != 4 or {s.get('standard') for s in screens} != standards:
            return False
        if any(s.get('compliant') is not True or s.get('borderline') is not False for s in screens):
            return False
        r = HTTP.get(f'{MIZAN_API}/purify/{symbol}', timeout=20)
        r.raise_for_status()
        p = r.json()
        if any(p.get(k) is None for k in ('ratio', 'interest_income', 'period_end', 'latest_annual_filing')):
            return False
        if float(p['ratio']) != 0.0 or float(p['interest_income']) != 0.0:
            return False
        period = datetime.strptime(p['period_end'], '%Y-%m-%d').date()
        latest = datetime.strptime(p['latest_annual_filing'], '%Y-%m-%d').date()
        age = (datetime.now(timezone.utc).date() - period).days
        if period != latest or not 0 <= age <= MAX_PURIFY_AGE:
            return False
        return True
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError) as exc:
        LOG.warning('%s: screening unavailable: %s', symbol, exc)
        return False


def get_bars(symbol, timeframe, start, end):
    response = HTTP.get(
        f'{ALPACA_API}/{symbol}/bars',
        headers=HEADERS,
        params={'timeframe': timeframe, 'start': start.isoformat(), 'end': end.isoformat(),
                'limit': 10000, 'feed': ALPACA_FEED, 'adjustment': 'raw'},
        timeout=25,
    )
    response.raise_for_status()
    rows = response.json().get('bars') or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={'t': 'timestamp', 'o': 'open', 'h': 'high',
                                             'l': 'low', 'c': 'close', 'v': 'volume'})
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df.dropna(subset=['timestamp', 'open', 'high', 'low', 'close', 'volume']).sort_values('timestamp')


def rsi(close, length=14):
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1/length, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/length, adjust=False).mean()
    rs = up / down.replace(0, float('nan'))
    return 100 - 100 / (1 + rs)


def atr(df, length=14):
    previous = df['close'].shift(1)
    tr = pd.concat([(df['high'] - df['low']).abs(), (df['high'] - previous).abs(),
                    (df['low'] - previous).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/length, adjust=False).mean()


def session_vwap(df):
    typical = (df['high'] + df['low'] + df['close']) / 3
    cumulative_volume = df['volume'].cumsum().replace(0, float('nan'))
    return (typical * df['volume']).cumsum() / cumulative_volume


def current_session_bars(df, session, now):
    if df.empty:
        return df
    local = df['timestamp'].dt.tz_convert(NY)
    same_day = local.dt.date == now.date()
    t = local.dt.hour * 60 + local.dt.minute
    bounds = {'PRE': (240, 570), 'REGULAR': (570, 960), 'POST': (960, 1200)}
    low, high = bounds[session]
    return df.loc[same_day & (t >= low) & (t < high)].copy().reset_index(drop=True)


def analyze(symbol, now, session):
    # Alpaca IEX is a partial venue feed, not full US market coverage.
    start = now.astimezone(timezone.utc) - timedelta(days=3)
    end = now.astimezone(timezone.utc)
    minute = get_bars(symbol, '1Min', start, end)
    five = get_bars(symbol, '5Min', start, end)
    minute = current_session_bars(minute, session, now)
    five = current_session_bars(five, session, now)
    # Need adequate completed history; drop any potentially unfinished candle.
    now_utc = now.astimezone(timezone.utc)
    minute = minute[minute['timestamp'] + pd.Timedelta(minutes=1) <= now_utc].copy()
    five = five[five['timestamp'] + pd.Timedelta(minutes=5) <= now_utc].copy()
    if len(minute) < 30 or len(five) < 25:
        return None
    last = minute.iloc[-1]
    vwap = session_vwap(minute).iloc[-1]
    current_rsi = rsi(five['close']).iloc[-1]
    current_atr = atr(five).iloc[-1]
    avg_volume = minute['volume'].iloc[-21:-1].mean()
    if any(pd.isna(x) for x in (vwap, current_rsi, current_atr, avg_volume)) or avg_volume <= 0:
        return None
    # Conservative momentum setup; NOT a forecast or guarantee.
    if not (last['close'] > vwap and 52 <= current_rsi <= 70 and
            last['volume'] >= 1.5 * avg_volume and current_atr > 0):
        return None
    price = float(last['close'])
    stop = price - 1.5 * float(current_atr)
    target = price + 2.0 * (price - stop)
    return {'symbol': symbol, 'price': price, 'vwap': float(vwap),
            'rsi': float(current_rsi), 'stop': stop, 'target': target,
            'session': session, 'bar_time': last['timestamp'].isoformat()}


def send_telegram(message):
    response = HTTP.post(
        f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
        json={'chat_id': TELEGRAM_CHAT, 'text': message}, timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected message')


def load_state():
    try:
        with STATE_FILE.open(encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding='utf-8')


def run():
    if not all((ALPACA_KEY, ALPACA_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT)):
        LOG.error('Missing required Alpaca/Telegram secrets')
        return
    now = datetime.now(timezone.utc).astimezone(NY)
    session = market_session(now)
    if not session:
        LOG.info('Outside US extended market hours')
        return
    symbols = approved_stocks()
    if not symbols:
        LOG.warning('No manually approved, recently verified pure symbols; no alerts sent')
        return
    state = load_state()
    day = now.strftime('%Y-%m-%d')
    for symbol in symbols:
        if state.get(symbol) == day:
            continue
        if not mizan_check(symbol):
            LOG.info('%s excluded by strict Sharia check', symbol)
            continue
        try:
            result = analyze(symbol, now, session)
            if result is None:
                continue
            msg = (f"US STOCK WATCHLIST SIGNAL (NOT A TRADE)\n"
                   f"{symbol} | {session}\n"
                   f"Price: ${result['price']:.2f}\n"
                   f"VWAP: ${result['vwap']:.2f} | RSI: {result['rsi']:.1f}\n"
                   f"Illustrative stop: ${result['stop']:.2f}\n"
                   f"Illustrative target: ${result['target']:.2f}\n"
                   f"Sharia: strict provisional checks passed; not a purity certificate.\n"
                   f"Alpaca IEX data may be incomplete. No automatic orders.")
            send_telegram(msg)
            state[symbol] = day
            save_state(state)
            LOG.info('Alert sent: %s', symbol)
        except (requests.RequestException, ValueError, RuntimeError, KeyError) as exc:
            LOG.warning('%s analysis/notification failed: %s', symbol, exc)


if __name__ == '__main__':
    run()
