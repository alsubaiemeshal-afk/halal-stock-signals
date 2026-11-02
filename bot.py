"""Conservative Alpaca IEX 1-minute breakout alerts. No orders are placed."""
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('scanner')
HTTP = requests.Session()
NY = ZoneInfo('America/New_York')
SA = ZoneInfo('Asia/Riyadh')
STATE = Path('.cache/telegram_state.json')
APPROVED = Path('approved_halal.csv')
CANDIDATES = Path('halal_candidates.csv')
KEY = os.getenv('ALPACA_API_KEY', '')
SECRET = os.getenv('ALPACA_SECRET_KEY', '')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT = os.getenv('TELEGRAM_CHAT_ID', '')
MAX_SYMBOLS = max(1, int(os.getenv('MAX_SYMBOLS', '40')))
MAX_AGE = max(1, float(os.getenv('MAX_BAR_AGE_MINUTES', '3')))
MIN_PRICE = float(os.getenv('MIN_PRICE', '1'))
MAX_PRICE = float(os.getenv('MAX_PRICE', '100'))
MIN_RVOL = float(os.getenv('MIN_RVOL', '1.3'))
MIN_VOLUME = int(os.getenv('MIN_IEX_SESSION_VOLUME', '10000'))
RUN_SECONDS = max(0, int(os.getenv('RUN_SECONDS', '210')))
POLL_SECONDS = max(30, int(os.getenv('POLL_SECONDS', '60')))
SUMMARY_HOURS = max(1, int(os.getenv('SUMMARY_HOURS', '6')))
# The supplied approved_halal.csv does not establish a verified Sharia source.
# Require an independently verified, current list before enabling trade signals.
VERIFIED = os.getenv('HALAL_SOURCE_VERIFIED', 'false').lower() == 'true'
MAX_APPROVAL_AGE_DAYS = max(1, int(os.getenv('MAX_APPROVAL_AGE_DAYS', '7')))
NASDAQ_URLS = [
    'https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt',
    'https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt',
]


def utc():
    return datetime.now(timezone.utc)


def parse_dt(s):
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def state_load():
    try:
        x = json.loads(STATE.read_text(encoding='utf-8'))
        if not isinstance(x, dict):
            raise ValueError('bad state')
        x.setdefault('alerts', {})
        x.setdefault('cursor', 0)
        x.setdefault('last_summary', None)
        return x
    except (OSError, ValueError, TypeError):
        return {'alerts': {}, 'cursor': 0, 'last_summary': None}


def state_save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(s, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, STATE)


def telegram(message):
    if not TOKEN or not CHAT:
        LOG.error('Telegram credentials missing')
        return False
    try:
        r = HTTP.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                      json={'chat_id': CHAT, 'text': message}, timeout=20)
        r.raise_for_status()
        return r.json().get('ok') is True
    except (requests.RequestException, ValueError) as e:
        LOG.error('Telegram delivery failed: %s', e)
        return False


def market_symbols():
    result = set()
    successes = 0
    for url in NASDAQ_URLS:
        try:
            r = HTTP.get(url, timeout=20)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text), sep='|', dtype=str)
            df.columns = df.columns.str.strip()
            column = 'Symbol' if 'Symbol' in df else 'ACT Symbol'
            if column not in df:
                raise ValueError('symbol column missing')
            for name in ('Test Issue', 'ETF'):
                if name in df:
                    df = df[df[name].fillna('').str.strip() == 'N']
            if 'Security Name' in df:
                df = df[~df['Security Name'].fillna('').str.contains(
                    r'\b(?:warrants?|rights?|units?|preferred|depositary notes?|debentures?|bonds?)\b',
                    case=False, regex=True)]
            result.update(x for x in df[column].dropna().str.strip().str.upper()
                          if re.fullmatch(r'[A-Z]{1,5}', x))
            successes += 1
        except (requests.RequestException, ValueError, KeyError) as e:
            LOG.warning('NASDAQ source failed: %s', e)
    if not successes:
        raise RuntimeError('NASDAQ listings unavailable')
    return result


def approved_symbols():
    if not VERIFIED:
        LOG.warning('HALAL_SOURCE_VERIFIED not enabled; signals disabled (fail closed)')
        return set()
    if not APPROVED.exists() or not CANDIDATES.exists():
        LOG.error('Both approved_halal.csv and halal_candidates.csv are required')
        return set()
    approved = pd.read_csv(APPROVED, dtype=str)
    candidates = pd.read_csv(CANDIDATES, dtype=str)
    if 'symbol' not in approved or not {'symbol', 'status', 'checked_at_utc'} <= set(candidates.columns):
        LOG.error('Approval metadata columns missing')
        return set()
    cutoff = utc() - timedelta(days=MAX_APPROVAL_AGE_DAYS)
    valid = set()
    for _, row in candidates.iterrows():
        symbol = str(row['symbol']).strip().upper()
        checked = parse_dt(row['checked_at_utc'])
        if (re.fullmatch(r'[A-Z]{1,5}', symbol)
                and str(row['status']).strip().lower() == 'approved'
                and checked is not None and cutoff <= checked <= utc() + timedelta(minutes=5)):
            valid.add(symbol)
    return valid & set(approved['symbol'].dropna().str.strip().str.upper())


def bars_for(symbol):
    end = utc()
    # Request most recent completed minute bars; IEX is a partial-market feed.
    params = {'symbols': symbol, 'timeframe': '1Min',
              'start': (end - timedelta(days=5)).isoformat(),
              'end': end.isoformat(), 'limit': 1000, 'feed': 'iex',
              'adjustment': 'raw', 'sort': 'desc'}
    r = HTTP.get('https://data.alpaca.markets/v2/stocks/bars',
                 headers={'APCA-API-KEY-ID': KEY, 'APCA-API-SECRET-KEY': SECRET},
                 params=params, timeout=20)
    r.raise_for_status()
    payload = r.json()
    if payload.get('next_page_token'):
        LOG.info('%s: bars paginated; first page only', symbol)
    bars = payload.get('bars', {}).get(symbol, [])
    if len(bars) < 45:
        return None
    df = pd.DataFrame(bars)
    df['t'] = pd.to_datetime(df['t'], utc=True)
    df = df.sort_values('t').drop_duplicates('t').reset_index(drop=True)
    cutoff = pd.Timestamp.now(tz='UTC').floor('min')
    df = df[df['t'] < cutoff].copy()
    return df if len(df) >= 45 else None


def analyze(symbol):
    df = bars_for(symbol)
    if df is None:
        return None
    last_time = df.iloc[-1]['t'].to_pydatetime()
    age = (utc() - last_time).total_seconds() / 60
    if age < 0 or age > MAX_AGE:
        return None
    # Only regular session; no implicit premarket/after-hours signal claims.
    ny = last_time.astimezone(NY)
    if ny.weekday() >= 5 or not (9 * 60 + 30 <= ny.hour * 60 + ny.minute < 16 * 60):
        return None
    dates = df['t'].dt.tz_convert(NY).dt.date
    today = df.loc[dates == ny.date()].copy()
    if len(today) < 22:
        return None
    df['ema9'] = df['c'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['c'].ewm(span=21, adjust=False).mean()
    change = df['c'].diff()
    up = change.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    down = (-change.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = up / down.replace(0, float('nan'))
    df['rsi'] = 100 - 100 / (1 + rs)
    df.loc[(down == 0) & (up > 0), 'rsi'] = 100
    df.loc[(down == 0) & (up == 0), 'rsi'] = 50
    prev = df['c'].shift(1)
    tr = pd.concat([df['h'] - df['l'], (df['h'] - prev).abs(),
                    (df['l'] - prev).abs()], axis=1).max(axis=1)
    df['atr'] = tr.ewm(alpha=1/14, adjust=False).mean()
    df['rvol'] = df['v'] / df['v'].shift(1).rolling(20).mean().replace(0, float('nan'))
    typical = (df['h'] + df['l'] + df['c']) / 3
    df['vwap'] = ((typical * df['v']).groupby(dates).cumsum()
                  / df['v'].groupby(dates).cumsum().replace(0, float('nan')))
    last = df.iloc[-1]
    before = df.iloc[-2]
    if pd.isna(last[['ema9', 'ema21', 'rsi', 'atr', 'rvol', 'vwap']]).any():
        return None
    price = float(last['c'])
    # Genuine new 20-bar breakout, not a persistent bullish trend.
    resistance = float(df['h'].shift(1).rolling(20).max().iloc[-1])
    breakout = float(before['c']) <= resistance < price
    volume = int(today['v'].sum())
    qualifies = (MIN_PRICE <= price <= MAX_PRICE and volume >= MIN_VOLUME
                 and breakout and price > float(last['vwap'])
                 and float(last['ema9']) > float(last['ema21'])
                 and 50 <= float(last['rsi']) <= 70
                 and float(last['rvol']) >= MIN_RVOL
                 and float(last['atr']) > 0)
    if not qualifies:
        return None
    return {'symbol': symbol, 'price': price, 'bar': last_time.isoformat(),
            'age': age, 'rsi': float(last['rsi']), 'rvol': float(last['rvol']),
            'atr': float(last['atr']), 'resistance': resistance}


def signal_message(x):
    price = x['price']
    stop = max(0.01, price - 1.5 * x['atr'])
    risk = price - stop
    sa_time = parse_dt(x['bar']).astimezone(SA).strftime('%Y-%m-%d %H:%M')
    return (f'🚨 إشارة اختراق فني جديدة — ليست أمر شراء\n\n'
            f'📈 السهم: {x["symbol"]}\n💵 إغلاق شمعة IEX: ${price:.2f}\n'
            f'📊 مقاومة 20 شمعة: ${x["resistance"]:.2f}\n'
            f'🛑 وقف استرشادي: ${stop:.2f}\n'
            f'🎯 هدف 1 استرشادي: ${price+risk:.2f}\n'
            f'🎯 هدف 2 استرشادي: ${price+2*risk:.2f}\n'
            f'⚠️ مخاطرة سعرية تقريبية: {risk/price*100:.2f}%\n'
            f'RSI {x["rsi"]:.1f} | RVOL {x["rvol"]:.2f}\n'
            f'🕒 توقيت الشمعة (السعودية): {sa_time}\n'
            f'⌛ عمر الشمعة: {x["age"]:.1f} دقيقة\n\n'
            'المصدر: Alpaca IEX فقط (تغطية جزئية). الأسعار ليست عرض تنفيذ مباشر. '
            'الاعتماد الشرعي يعتمد على القائمة التي تحقق منها المشغل، وليس ضمانًا من البوت.')


def main():
    if not KEY or not SECRET:
        raise RuntimeError('Missing Alpaca GitHub secrets')
    state = state_load()
    expiry = utc() - timedelta(days=7)
    state['alerts'] = {k: v for k, v in state['alerts'].items()
                       if parse_dt(v) is not None and parse_dt(v) >= expiry}
    market = market_symbols()
    halal = approved_symbols()
    eligible = sorted(market & halal)
    if not eligible:
        LOG.warning('No eligible verified symbols; no trade alerts')
    cursor = int(state.get('cursor', 0)) % max(1, len(eligible))
    batch = [eligible[(cursor+i) % len(eligible)] for i in range(min(MAX_SYMBOLS, len(eligible)))] if eligible else []
    state['cursor'] = (cursor + len(batch)) % len(eligible) if eligible else 0
    # Save rotation before analysis so failures do not permanently pin the first batch.
    state_save(state)
    analyzed = sent = errors = 0
    for symbol in batch:
        try:
            x = analyze(symbol)
            analyzed += 1
            if x is None:
                continue
            alert_id = f'{symbol}|{x["bar"]}'
            if alert_id in state['alerts']:
                continue
            if telegram(signal_message(x)):
                state['alerts'][alert_id] = utc().isoformat()
                state_save(state)
                sent += 1
            else:
                errors += 1
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            errors += 1
            LOG.warning('Analysis failed %s: %s', symbol, e)
    last_summary = parse_dt(state.get('last_summary'))
    if last_summary is None or utc() - last_summary >= timedelta(hours=SUMMARY_HOURS):
        msg = (f'📊 تقرير البوت — {utc().astimezone(SA):%Y-%m-%d %H:%M} السعودية\n'
               f'السوق: {len(market)} | القائمة المتحقق منها: {len(halal)}\n'
               f'المؤهلة: {len(eligible)} | فُحصت: {analyzed}\n'
               f'إشارات مرسلة: {sent} | أخطاء: {errors}\n'
               f'تفعيل مصدر النقاوة بعد التحقق المستقل: {VERIFIED}\n'
               'IEX جزئي، لا يوجد تنفيذ صفقات.')
        if telegram(msg):
            state['last_summary'] = utc().isoformat()
    state_save(state)
    LOG.info('Eligible=%d analyzed=%d sent=%d errors=%d cursor=%d',
             len(eligible), analyzed, sent, errors, state['cursor'])


if __name__ == '__main__':
    deadline = time.monotonic() + RUN_SECONDS
    while True:
        try:
            main()
        except Exception:
            LOG.exception('Scanner iteration failed')
        if time.monotonic() + POLL_SECONDS >= deadline:
            break
        time.sleep(POLL_SECONDS)
