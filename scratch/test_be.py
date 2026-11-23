import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pandas as pd
import numpy as np

os.environ["MPLCONFIGDIR"] = "/tmp/matplotlib"

from config import DATA_FILE, MARKET_TZ, SESSION_END
from backtester import load_data, prepare_datasets
from strategy import find_setup_1m, determine_bias_15m

df1 = load_data(DATA_FILE)
df1, df15 = prepare_datasets(df1)

trading_days = sorted(list(set(df1.index.strftime('%Y-%m-%d'))))
all_trades = []

for dstr in trading_days:
    if dstr < '2022-01-01' or dstr > '2024-12-31':
        continue
    day_1m = df1.loc[dstr]
    if len(day_1m) < 60:
        continue
    t0 = pd.Timestamp(f'{dstr} 09:30', tz=MARKET_TZ)
    if t0 not in day_1m.index:
        continue
    bias, _ = determine_bias_15m(df15, t0)
    if bias == 0:
        continue
    session_1m = day_1m.loc[t0:]
    setup, _ = find_setup_1m(session_1m, bias)
    if setup is None:
        continue
        
    exit_deadline = pd.Timestamp(f'{dstr} {SESSION_END}', tz=MARKET_TZ)
    m1 = df1.loc[setup.setup_time:exit_deadline]
    if m1.empty:
        continue
        
    entry = setup.entry_price
    sl = setup.stop_loss
    risk = setup.risk_points
    direction = setup.direction
    
    # Track with different BE rules:
    # 1. Raw (no BE): did it hit 6.25R before original SL?
    # 2. BE at +2.0R: did it hit 6.25R? If it hit +2R first, then fell back to entry -> BE (0R), else if it hit 6.25R -> win.
    # 3. BE at +3.0R:
    
    # Let's simulate bar-by-bar
    m1_len = len(m1)
    
    def simulate_p2_trade(be_trigger=None):
        curr_sl = sl
        be_active = False
        for i in range(m1_len):
            row = m1.iloc[i]
            h, l = row['high'], row['low']
            
            if direction == 1:
                # Check hit 6.25R
                if (h - entry) / risk >= 6.25:
                    return 6.25
                # Check SL
                if l <= curr_sl:
                    return 0.0 if be_active else -1.03
                # Check BE trigger
                if be_trigger and not be_active and (h - entry) / risk >= be_trigger:
                    be_active = True
                    curr_sl = entry
            else:
                if (entry - l) / risk >= 6.25:
                    return 6.25
                if h >= curr_sl:
                    return 0.0 if be_active else -1.03
                if be_trigger and not be_active and (entry - l) / risk >= be_trigger:
                    be_active = True
                    curr_sl = entry
        return 0.0 if be_active else -1.03

    raw_r = simulate_p2_trade(be_trigger=None)
    be2_r = simulate_p2_trade(be_trigger=2.0)
    be25_r = simulate_p2_trade(be_trigger=2.5)
    be3_r = simulate_p2_trade(be_trigger=3.0)
    
    all_trades.append({
        'date': dstr,
        'direction': setup.direction,
        'risk_points': risk,
        'raw_r': raw_r,
        'be2_r': be2_r,
        'be25_r': be25_r,
        'be3_r': be3_r
    })

tdf = pd.DataFrame(all_trades)
print("=== Phase 2 (Moonshot) Comparison ===")
for col in ['raw_r', 'be2_r', 'be25_r', 'be3_r']:
    wins = (tdf[col] == 6.25).sum()
    be_cnt = (tdf[col] == 0.0).sum()
    losses = (tdf[col] < 0).sum()
    total_r = tdf[col].sum()
    print(f"{col:8s} -> Wins (6.25R): {wins:2d} | BEs (0R): {be_cnt:2d} | Losses (-1R): {losses:3d} | Total R: {total_r:+.2f} R")
