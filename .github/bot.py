"""Conservative alert-only US equities scanner. No broker order endpoints."""
import os, time, csv, json, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests
import pandas as pd
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
API='https://data.alpaca.markets/v2/stocks'
HEADERS={'APCA-API-KEY-ID':os.getenv('ALPACA_API_KEY',''), 'APCA-API-SECRET-KEY':os.getenv('ALPACA_SECRET_KEY','')}
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','')
CHAT=os.getenv('TELEGRAM_CHAT_ID','')
STATE=ROOT/'sent_alerts.json'
SESSION=requests.Session()

def approved():
    result=[]
    with open(ROOT/'approved_halal.csv',newline='',encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            try:
                reviewed=datetime.strptime(r['reviewed_at'],'%Y-%m-%d').date()
                age=(datetime.now(timezone.utc).date()-reviewed).days
                if r['verified_pure'].strip().lower()!='true' or not (0<=age<=int(os.getenv('MAX_SCREEN_AGE_DAYS','30'))): continue
                if not r['source'].strip(): continue
                symbol=r['symbol'].strip().upper()
                if symbol.isascii() and symbol.replace('.','').isalpha(): result.append(symbol)
            except (ValueError,KeyError,AttributeError): continue
    return sorted(set(result))

def bars(symbol, timeframe, start, end):
    response=SESSION.get(f'{API}/{symbol}/bars',headers=HEADERS,params={'timeframe':timeframe,'start':start.isoformat().replace('+00:00','Z'),'end':end.isoformat().replace('+00:00','Z'),'limit':10000,'feed':'iex','adjustment':'raw'},timeout=25)
    response.raise_for_status()
    rows=response.json().get('bars',[])
    if not rows:return pd.DataFrame()
    df=pd.DataFrame(rows).rename(columns={'t':'time','o':'open','h':'high','l':'low','c':'close','v':'volume'})
    df['time']=pd.to_datetime(df['time'],utc=True)
    return df.sort_values('time').set_index('time')

def rsi(close, period=14):
    delta=close.diff(); up=delta.clip(lower=0); down=-delta.clip(upper=0)
    avg_up=up.ewm(alpha=1/period,adjust=False,min_periods=period).mean()
    avg_down=down.ewm(alpha=1/period,adjust=False,min_periods=period).mean()
    rs=avg_up/avg_down.replace(0,float('nan'))
    return (100-100/(1+rs)).fillna(100).where(avg_up.ne(0),0)

def atr(df,period=14):
    prev=df.close.shift()
    tr=pd.concat([(df.high-df.low),(df.high-prev).abs(),(df.low-prev).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/period,adjust=False,min_periods=period).mean()

def signal(one,five):
    if len(one)<35 or len(five)<25:return None
    ny=ZoneInfo('America/New_York')
    one=one.copy(); five=five.copy()
    one=one.between_time('09:30','15:59')
    five=five.between_time('09:30','15:59')
    if len(one)<30 or len(five)<22:return None
    # One session only, and all timestamps are UTC; group by New York session date.
    d=one.index.tz_convert(ny).date[-1]
    one=one[one.index.tz_convert(ny).date==d]
    five=five[five.index.tz_convert(ny).date==d]
    if len(one)<25 or len(five)<20:return None
    one['vwap']=( ((one.high+one.low+one.close)/3)*one.volume).cumsum()/one.volume.cumsum().replace(0,float('nan'))
    five['ema8']=five.close.ewm(span=8,adjust=False).mean()
    five['ema20']=five.close.ewm(span=20,adjust=False).mean()
    one['rsi']=rsi(one.close)
    one['atr']=atr(one)
    one['rvol']=one.volume/one.volume.shift(1).rolling(20,min_periods=20).mean()
    x=one.iloc[-1]; trend=five.iloc[-1]
    if pd.isna(x[['vwap','rsi','atr','rvol']]).any():return None
    # Avoid late/stale bars; caller already drops the potentially unfinished candle.
    if not (x.close>x.vwap and trend.ema8>trend.ema20 and 50<=x.rsi<=70 and x.rvol>=1.5 and x.atr>0):return None
    # breakout over preceding 5 completed candles
    if x.close<=one.high.iloc[-6:-1].max():return None
    entry=float(x.close); stop=entry-max(float(x.atr)*1.5,entry*0.003)
    risk=entry-stop
    if stop<=0:return None
    return {'time':str(one.index[-1]),'entry':entry,'stop':stop,'target1':entry+1.5*risk,'target2':entry+2*risk,'rsi':float(x.rsi),'rvol':float(x.rvol)}

def notify(msg):
    r=SESSION.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':CHAT,'text':msg},timeout=20)
    r.raise_for_status()

def run():
    if not all([TOKEN,CHAT,HEADERS['APCA-API-KEY-ID'],HEADERS['APCA-API-SECRET-KEY']]):raise RuntimeError('Missing .env credentials')
    sent=json.loads(STATE.read_text()) if STATE.exists() else {}
    notify('🤖 بوت إشارات الأسهم النقية بدأ العمل. لا ينفذ أي صفقات.')
    while True:
        try:
            ny_now=datetime.now(ZoneInfo('America/New_York'))
            if ny_now.weekday()<5 and (9,35)<=(ny_now.hour,ny_now.minute)<=(15,55):
                symbols=approved()
                if not symbols:logging.warning('No verified current pure-stock list. Alerts blocked.')
                now=datetime.now(timezone.utc)
                for symbol in symbols:
                    try:
                        # Delay endpoint by 90 seconds to avoid unfinished bars.
                        end=now-timedelta(seconds=90)
                        one=bars(symbol,'1Min',end-timedelta(days=2),end)
                        five=bars(symbol,'5Min',end-timedelta(days=3),end)
                        result=signal(one,five)
                        if not result:continue
                        key=f'{symbol}:{result["time"]}'
                        if key in sent:continue
                        msg=(f'📈 إشارة شراء تجريبية: {symbol}\n'
                             f'الدخول المرجعي: ${result["entry"]:.2f}\n'
                             f'وقف الخسارة: ${result["stop"]:.2f}\n'
                             f'الهدف 1: ${result["target1"]:.2f}\n'
                             f'الهدف 2: ${result["target2"]:.2f}\n'
                             f'RSI: {result["rsi"]:.1f} | RVOL: {result["rvol"]:.2f}\n'
                             'بيانات IEX وليست كل السوق. تحقق من السعر والفحص الشرعي في دراية قبل التنفيذ.\n'
                             '⚠️ تنبيه تحليلي وليس أمر تداول أو ضمان ربح.')
                        notify(msg); sent[key]=datetime.now(timezone.utc).isoformat()
                        sent={k:v for k,v in sent.items() if (datetime.now(timezone.utc)-datetime.fromisoformat(v)).days<7}
                        STATE.write_text(json.dumps(sent,indent=2))
                    except Exception:logging.exception('Symbol failed: %s',symbol)
        except Exception:logging.exception('Scanner cycle failed')
        time.sleep(max(60,int(os.getenv('POLL_SECONDS','75'))))

if __name__=='__main__':run()
