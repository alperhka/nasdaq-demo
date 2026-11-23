import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pandas as pd
import numpy as np

os.environ["MPLCONFIGDIR"] = "/tmp/matplotlib"

from config import DATA_FILE, MARKET_TZ, CONFIG, HOLIDAYS
from backtester import load_data, prepare_datasets
from strategy import determine_bias_15m

df1 = load_data(DATA_FILE)
df1, df15 = prepare_datasets(df1)

# Let's test an updated session setup searcher that can:
# 1. Trade both AM (09:30 - 11:30) and PM (13:30 - 15:15)
# 2. Allow up to 2 trades per day (e.g. 1 in AM, 1 in PM, or second in AM if first finished)

def find_setups_in_window(window_1m, bias, start_time_str, end_time_str, min_points=2.0, max_trades=1):
    setups = []
    active_fvg = None
    
    for i in range(len(window_1m)):
        t = window_1m.index[i]
        t_str = t.strftime("%H:%M")
        if t_str > end_time_str:
            break
            
        c = window_1m.iloc[i]
        
        if active_fvg is None and len(setups) < max_trades:
            disp_bull = c.get("disp_bull", False) or (i > 0 and window_1m["disp_bull"].iloc[i-1])
            disp_bear = c.get("disp_bear", False) or (i > 0 and window_1m["disp_bear"].iloc[i-1])
            
            # LONG
            if bias >= 0 and c.get("fvg_bull", False) and disp_bull:
                fvg_top = c["fvg_bull_top"]
                fvg_bot = c["fvg_bull_bottom"]
                sl = fvg_bot - CONFIG["STOP_BUFFER_POINTS"]
                entry = fvg_top + CONFIG["SPREAD_POINTS"]
                risk = entry - sl
                if CONFIG["MIN_STOP_POINTS"] <= risk <= CONFIG["MAX_STOP_POINTS"]:
                    active_fvg = {
                        "direction": 1, "created_idx": i, "setup_time": t,
                        "entry": entry, "sl": sl, "risk": risk
                    }
            # SHORT
            elif bias <= 0 and c.get("fvg_bear", False) and disp_bear:
                fvg_top = c["fvg_bear_top"]
                fvg_bot = c["fvg_bear_bottom"]
                sl = fvg_top + CONFIG["STOP_BUFFER_POINTS"]
                entry = fvg_bot - CONFIG["SPREAD_POINTS"]
                risk = sl - entry
                if CONFIG["MIN_STOP_POINTS"] <= risk <= CONFIG["MAX_STOP_POINTS"]:
                    active_fvg = {
                        "direction": -1, "created_idx": i, "setup_time": t,
                        "entry": entry, "sl": sl, "risk": risk
                    }
                    
        elif active_fvg is not None and i > active_fvg["created_idx"]:
            direction = active_fvg["direction"]
            if direction == 1:
                if c["low"] <= active_fvg["entry"]:
                    if c["low"] > active_fvg["sl"]:
                        setups.append(active_fvg)
                        active_fvg = None
                    else:
                        active_fvg = None
                elif c["close"] > active_fvg["entry"] + active_fvg["risk"] * 2.5:
                    active_fvg = None
            else:
                if c["high"] >= active_fvg["entry"]:
                    if c["high"] < active_fvg["sl"]:
                        setups.append(active_fvg)
                        active_fvg = None
                    else:
                        active_fvg = None
                elif c["close"] < active_fvg["entry"] - active_fvg["risk"] * 2.5:
                    active_fvg = None
                    
    return setups

# Run simulation across 2022-2024
trading_days = sorted(list(set(df1.index.strftime('%Y-%m-%d'))))
all_sim_trades = []

for dstr in trading_days:
    if dstr < '2022-01-01' or dstr > '2024-12-31' or dstr in HOLIDAYS:
        continue
    day_data = df1.loc[dstr]
    if len(day_data) < 100:
        continue
        
    t_am_start = pd.Timestamp(f"{dstr} 09:30", tz=MARKET_TZ)
    if t_am_start not in day_data.index:
        continue
        
    # Bias at 09:30
    bias_am, _ = determine_bias_15m(df15, t_am_start)
    
    # 1. AM Session (09:30 - 11:30)
    am_window = day_data.loc[f"{dstr} 09:30": f"{dstr} 11:30"]
    am_setups = find_setups_in_window(am_window, bias_am, "09:30", "11:30", max_trades=1)
    
    # 2. PM Session (13:30 - 15:15)
    t_pm_start = pd.Timestamp(f"{dstr} 13:30", tz=MARKET_TZ)
    bias_pm, _ = determine_bias_15m(df15, t_pm_start)
    pm_window = day_data.loc[f"{dstr} 13:30": f"{dstr} 15:15"]
    pm_setups = find_setups_in_window(pm_window, bias_pm, "13:30", "15:15", max_trades=1)
    
    day_setups = am_setups + pm_setups
    
    # Simulate each setup
    for s in day_setups:
        exit_deadline = pd.Timestamp(f"{dstr} 16:00", tz=MARKET_TZ)
        m1 = day_data.loc[s["setup_time"]:exit_deadline]
        if m1.empty:
            continue
            
        entry = s["entry"]
        sl = s["sl"]
        risk = s["risk"]
        direction = s["direction"]
        
        # Track max R before SL
        max_r = 0.0
        for i in range(len(m1)):
            row = m1.iloc[i]
            h, l = row["high"], row["low"]
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
                    
        all_sim_trades.append({
            "date": dstr,
            "session": "AM" if s["setup_time"].hour < 12 else "PM",
            "direction": direction,
            "risk": risk,
            "max_r": max_r
        })

sim_df = pd.DataFrame(all_sim_trades)
print(f"Total Trades with AM + PM: {len(sim_df)} (vorher: 415)")
print(f"Trades per Year: {len(sim_df) / 3:.1f} (~{len(sim_df) / (750):.2f} Trades pro Handelstag!)")
print(f"AM Trades: {(sim_df['session'] == 'AM').sum()}")
print(f"PM Trades: {(sim_df['session'] == 'PM').sum()}")

# Check Hit Rates
print("\nHit Rates:")
for k in [1.0, 2.0, 6.25]:
    cnt = (sim_df['max_r'] >= k).sum()
    pct = cnt / len(sim_df) * 100
    print(f"Hit {k:4.2f}R: {cnt:3d} / {len(sim_df)} ({pct:.1f}%)")

from backtester import simulate_payout_harvesting, print_payout_harvest_report

acc_df, summary = simulate_payout_harvesting(sim_df, max_dd_override=1000.0)
print_payout_harvest_report(summary, acc_df)
