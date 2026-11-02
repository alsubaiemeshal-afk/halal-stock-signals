"""Check configuration without exposing secrets or placing orders."""
import os
from pathlib import Path
import pandas as pd

for name in ('ALPACA_API_KEY', 'ALPACA_SECRET_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
    print(f'{name}: {"SET" if os.getenv(name) else "MISSING"}')
for name in ('approved_halal.csv', 'halal_candidates.csv'):
    path = Path(name)
    if not path.exists():
        print(f'{name}: MISSING')
    else:
        try:
            df = pd.read_csv(path)
            print(f'{name}: {len(df)} rows; columns={list(df.columns)}')
        except Exception as e:
            print(f'{name}: INVALID ({type(e).__name__})')
print('HALAL_SOURCE_VERIFIED:', os.getenv('HALAL_SOURCE_VERIFIED', 'false'))
