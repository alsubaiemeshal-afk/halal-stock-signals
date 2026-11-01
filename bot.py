import io
import os
import re
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('scanner')
HTTP = requests.Session()
NY = ZoneInfo('America/New_York')
NASDAQ_URLS = [
    'https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt',
    'https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt',
]
ALPACA_KEY = os.getenv('ALPACA_API_KEY', '')
ALPACA_SECRET = os.getenv('ALPACA_SECRET_KEY', '')
TG_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TG_CHAT = os.getenv('TELEGRAM_CHAT_ID', '')
HALAL_FILE = 'approved_halal.csv'
MAX_SYMBOLS = int(os.getenv('MAX_SYMBOLS', '30'))
MIN_PRICE = float(os.getenv('MIN_PRICE', '1'))
MAX_PRICE = float(os.getenv('MAX_PRICE', '30'))
MIN_RVOL = float(os.getenv('MIN_RVOL', '1.5'))
MIN_VOLUME = int(os.getenv('MIN_IEX_SESSION_VOLUME', '50000'))
TIMEOUT = 25


def telegram(message):
    if not TG_TOKEN or not TG_CHAT:
        LOG.warning('Telegram secrets missing; no message sent')
        return False
    r = HTTP.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                  json={'chat_id': TG_CHAT, 'text': message}, timeout=TIMEOUT)
    r.raise_for_status()
    return bool(r.json().get('ok'))


def market_symbols():
    symbols = set()
    success = 0
    for url in NASDAQ_URLS:
        try:
            r = HTTP.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text), sep='|', dtype=str)
            df.columns = df.columns.str.strip()
            col = 'Symbol' if 'Symbol' in df else 'ACT Symbol'
            if col not in df:
                raise ValueError('No symbol column')
            if 'Test Issue' in df:
                df = df[df['Test Issue'].fillna('').str.strip() == 'N']
            if 'ETF' in df:
                df = df[df['ETF'].fillna('').str.strip() == 'N']
            if 'Security Name' in df:
                pattern = r'(?i)\b(?:warrants?|rights?|units?|preferred|depositary|notes?|debentures?|bonds?)\b'
                df = df[~df['Security Name'].fillna('').str.contains(pattern, regex=True)]
            symbols.update(s for s in df[col].dropna().str.strip().str.upper()
                           if re.fullmatch(r'[A-Z]{1,5}', s))
            success += 1
        except Exception as exc:
            LOG.error('NASDAQ download failed (%s): %s', url, exc)
    if success == 0:
        raise RuntimeError('All Nasdaq sources failed')
    LOG.info('NASDAQ symbol count: %d', len(symbols))
    return symbols


def approved_symbols():
    if not os.path.isfile(HALAL_FILE):
        LOG.warning('Missing approved_halal.csv; fail closed')
        return set()
    df = pd.read_csv(HALAL_FILE, dtype=str)
    if 'symbol' not in df.columns:
        raise ValueError('approved_halal.csv must have symbol column')
    return {s for x in df['symbol'].dropna()
            if (s := str(x).strip().upper()) and re.fullmatch(r'[A-Z]{1,5}', s)}


def get_bars(symbol):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=14)
    headers = {'APCA-API-KEY-ID': ALPACA_KEY, 'APCA-API-SECRET-KEY': ALPACA_SECRET}
    params = {'symbols': symbol, 'timeframe': '5Min', 'start': start.isoformat(),
              'end': end.isoformat(), 'limit': 1000, 'feed': 'iex',
              'adjustment': 'raw', 'sort': 'desc'}
    r = HTTP.get('https://data.alpaca.markets/v2/stocks/bars',
                 headers=headers, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    bars = r.json().get('bars', {}).get(symbol, [])
    if len(bars) < 40:
        return None
    df = pd.DataFrame(bars)
    df['t'] = pd.to_datetime(df['t'], utc=True)
    df = df.sort_values('t').drop_duplicates('t').reset_index(drop=True)
    # Drop unfinished candle, including during live sessions
    df = df[df['t'] + pd.Timedelta(minutes=5) <= pd.Timestamp.now(tz='UTC')].copy()
    return df if len(df) >= 40 else None


def analyze(symbol):
    df = get_bars(symbol)
    if df is None:
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
    tr = pd.concat([df['h']-df['l'], (df['h']-prev).abs(), (df['l']-prev).abs()], axis=1).max(axis=1)
    df['atr'] = tr.ewm(alpha=1/14, adjust=False).mean()
    df['rvol'] = df['v'] / df['v'].shift(1).rolling(20).mean()
    dates = df['t'].dt.tz_convert(NY).dt.date
    typical = (df['h'] + df['l'] + df['c']) / 3
    df['vwap'] = (typical * df['v']).groupby(dates).cumsum() / df['v'].groupby(dates).cumsum().replace(0, float('nan'))
    last = df.iloc[-1]
    required = ['ema9', 'ema21', 'rsi', 'atr', 'rvol', 'vwap']
    if pd.isna(last[required]).any():
        return None
    price = float(last['c'])
    day_volume = int(df.loc[dates == dates.iloc[-1], 'v'].sum())
    qualifies = (MIN_PRICE <= price <= MAX_PRICE and day_volume >= MIN_VOLUME
                 and price > last['vwap'] and last['ema9'] > last['ema21']
                 and 50 <= last['rsi'] <= 70 and last['rvol'] >= MIN_RVOL
                 and last['atr'] > 0)
    age_minutes = (pd.Timestamp.now(tz='UTC') - last['t']).total_seconds() / 60
    return {'symbol': symbol, 'price': price, 'bar': last['t'],
            'age': age_minutes, 'rsi': float(last['rsi']),
            'rvol': float(last['rvol']), 'atr': float(last['atr']),
            'qualifies': bool(qualifies)}


def main():
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise RuntimeError('Set ALPACA_API_KEY and ALPACA_SECRET_KEY in GitHub Secrets')
    LOG.info('24/7 TEST: starting one scan, even if market closed')
    market = market_symbols()
    halal = approved_symbols()
    eligible = sorted(market & halal)
    LOG.info('Approved=%d eligible=%d', len(halal), len(eligible))
    checked = 0
    fresh_signals = []
    historical_matches = 0
    errors = 0
    for symbol in eligible[:MAX_SYMBOLS]:
        try:
            result = analyze(symbol)
            if result is None:
                continue
            checked += 1
            if result['qualifies']:
                if result['age'] <= 15:
                    fresh_signals.append(result)
                else:
                    historical_matches += 1
        except Exception as exc:
            errors += 1
            LOG.error('%s: %s', symbol, exc)
    lines = ['🤖 Nasdaq halal scanner — 24/7 TEST',
             f'UTC: {datetime.now(timezone.utc):%Y-%m-%d %H:%M}',
             f'Nasdaq symbols: {len(market)}',
             f'Halal approved: {len(halal)}',
             f'Eligible: {len(eligible)}',
             f'Analyzed: {checked} | Errors: {errors}',
             f'Fresh matches: {len(fresh_signals)}',
             f'Old-candle matches (not signals): {historical_matches}',
             '⚠️ IEX feed only; no trade execution.']
    if not eligible:
        lines.append('No approved symbols: add independently verified symbols to approved_halal.csv.')
    for s in fresh_signals[:5]:
        lines.append(f"📈 {s['symbol']} ${s['price']:.2f} | RSI {s['rsi']:.1f} | RVOL {s['rvol']:.2f} | candle {s['bar']:%H:%M} UTC")
    message = '\n'.join(lines)
    LOG.info('\n%s', message)
    if not telegram(message):
        LOG.warning('Telegram message not delivered')


if __name__ == '__main__':
    main()
