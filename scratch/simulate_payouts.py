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
    
    # Check max R reached before SL
    max_r = 0.0
    for i in range(len(m1)):
        row = m1.iloc[i]
        h, l = row['high'], row['low']
        if direction == 1:
            if l <= sl:
                break
            r = (h - entry) / risk
            if r > max_r:
                max_r = r
        else:
            if h >= sl:
                break
            r = (entry - l) / risk
            if r > max_r:
                max_r = r
                
    all_trades.append({
        'date': dstr,
        'direction': setup.direction,
        'risk_points': risk,
        'max_r': max_r
    })

trades_df = pd.DataFrame(all_trades)
print(f'Total trades generated: {len(trades_df)}')

def run_lifecycle_simulation(trades_df, max_dd=800.0, phase1_payouts_needed=2, p1_risk=100.0, p1_target_r=1.0, p2_risk=80.0, p2_target_r=6.25):
    accounts = []
    idx = 0
    n = len(trades_df)
    
    while idx < n:
        balance = 10000.0
        dd_floor = balance - max_dd
        
        payouts_100 = 0
        payouts_500 = 0
        total_payout = 0.0
        trades_count = 0
        phase = 1 if phase1_payouts_needed > 0 else 2
        blown = False
        
        while idx < n:
            trade = trades_df.iloc[idx]
            idx += 1
            trades_count += 1
            max_r = trade['max_r']
            
            if phase == 1:
                # Phase 1: Try to get 1% payout ($100)
                if max_r >= p1_target_r:
                    profit = p1_risk * p1_target_r
                    total_payout += profit
                    payouts_100 += 1
                    if payouts_100 >= phase1_payouts_needed:
                        phase = 2
                else:
                    loss = p1_risk * 1.03
                    balance -= loss
            else:
                # Phase 2: Moonshot (Target 6.25R = $500 with $80 risk)
                if max_r >= p2_target_r:
                    profit = p2_risk * p2_target_r
                    total_payout += profit
                    payouts_500 += 1
                else:
                    loss = p2_risk * 1.03
                    balance -= loss
                    
            if balance <= dd_floor:
                blown = True
                break
                
        accounts.append({
            'trades': trades_count,
            'payouts_100': payouts_100,
            'payouts_500': payouts_500,
            'total_payout': total_payout,
            'blown': blown
        })
        
    acc_df = pd.DataFrame(accounts)
    return acc_df

for dd in [500.0, 750.0, 1000.0]:
    acc_df = run_lifecycle_simulation(trades_df, max_dd=dd, phase1_payouts_needed=2)
    print(f"\n=======================================================")
    print(f"=== RESULTS FOR MAX TRAILING DD: ${dd:.0f} ===")
    print(f"=======================================================")
    print(f"Total Accounts simulated:        {len(acc_df)}")
    print(f"Average Payout per Account:      ${acc_df['total_payout'].mean():,.2f}")
    print(f"Median Payout per Account:       ${acc_df['total_payout'].median():,.2f}")
    print(f"Max Payout on single Account:    ${acc_df['total_payout'].max():,.2f}")
    print(f"Accounts with >= $1,000 payout:  {(acc_df['total_payout'] >= 1000).mean()*100:.1f}%")
    print(f"Accounts with >= $1,500 payout:  {(acc_df['total_payout'] >= 1500).mean()*100:.1f}%")
    print(f"Accounts with >= $2,000 payout:  {(acc_df['total_payout'] >= 2000).mean()*100:.1f}%")
    print(f"Average Trades per Account:      {acc_df['trades'].mean():.1f}")
