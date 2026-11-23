import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pandas as pd
import numpy as np

os.environ["MPLCONFIGDIR"] = "/tmp/matplotlib"

from config import DATA_FILE, MARKET_TZ
from backtester import load_data, prepare_datasets, run_backtest

df1 = load_data(DATA_FILE)
df1, df15 = prepare_datasets(df1)
trades_df, _ = run_backtest(df1, df15, multi_session=True)
print(f"Loaded {len(trades_df)} trades from 2022 to 2024.")

# Let's verify FundingPips exact simulation
def simulate_fundingpips(trades_df):
    CHALLENGE_FEE = 80.0
    FEE_REFUND_AT_PAYOUT = 4
    STATIC_MAX_LOSS = 1000.0
    DAILY_LOSS_LIMIT = 500.0
    
    # Eval 1: 8% target ($800), min 3 days
    E1_TARGET = 800.0
    E1_MIN_DAYS = 3
    E1_RISK = 100.0
    E1_RR = 2.0  # +$200 win, -$103 loss
    
    # Eval 2: 5% target ($500), min 3 days
    E2_TARGET = 500.0
    E2_MIN_DAYS = 3
    E2_RISK = 100.0
    E2_RR = 2.0  # +$200 win, -$103 loss
    
    # Master Stage
    M_P1_RISK = 100.0
    M_P1_RR = 1.25  # +$125 gross ($100 net at 80%)
    M_P1_PAYOUTS_NEEDED = 2
    
    M_P2_RISK = 80.0
    M_P2_RR = 6.25  # +$500 gross ($400 net at 80%)
    
    PROFIT_SPLIT = 0.80
    
    challenges = []
    masters = []
    
    idx = 0
    n = len(trades_df)
    
    chal_id = 1
    master_id = 1
    
    while idx < n:
        # Start a new challenge
        chal_start_date = trades_df.iloc[idx]["date"]
        state = "EVAL_1"
        balance = 10000.0
        active_days = set()
        day_start_balance = 10000.0
        current_day = chal_start_date
        chal_cost = CHALLENGE_FEE
        chal_blown = False
        chal_passed = False
        
        while idx < n and state in ["EVAL_1", "EVAL_2"]:
            trade = trades_df.iloc[idx]
            trade_date = trade["date"]
            idx += 1
            
            # Check day transition
            if trade_date != current_day:
                current_day = trade_date
                day_start_balance = balance
                
            active_days.add(trade_date)
            max_r = trade["max_r"]
            
            # Trade execution in Eval
            if max_r >= E1_RR:
                balance += E1_RISK * E1_RR
            else:
                balance -= E1_RISK * 1.03
                
            # Check Daily Loss
            if day_start_balance - balance >= DAILY_LOSS_LIMIT:
                chal_blown = True
                break
                
            # Check Static Max Loss
            if balance <= 10000.0 - STATIC_MAX_LOSS:
                chal_blown = True
                break
                
            if state == "EVAL_1":
                if balance >= 10000.0 + E1_TARGET and len(active_days) >= E1_MIN_DAYS:
                    state = "EVAL_2"
                    balance = 10000.0
                    day_start_balance = 10000.0
                    active_days = set()
            elif state == "EVAL_2":
                if balance >= 10000.0 + E2_TARGET and len(active_days) >= E2_MIN_DAYS:
                    chal_passed = True
                    state = "MASTER"
                    break
                    
        challenges.append({
            "challenge_id": f"Chal #{chal_id:03d}",
            "start_date": chal_start_date,
            "passed": chal_passed,
            "failed": chal_blown
        })
        chal_id += 1
        
        if chal_passed and idx < n:
            # We have a Master Account!
            m_start_date = trades_df.iloc[idx]["date"]
            m_balance = 10000.0
            m_day_start_balance = 10000.0
            m_current_day = m_start_date
            last_payout_date = pd.Timestamp(m_start_date)
            payouts_count = 0
            m_p1_payouts = 0
            m_p2_payouts = 0
            total_gross_payouts = 0.0
            total_net_payouts = 0.0
            fee_refunded = 0.0
            m_phase = 1
            m_trades_count = 0
            m_blown = False
            
            while idx < n:
                trade = trades_df.iloc[idx]
                t_date_str = trade["date"]
                t_date = pd.Timestamp(t_date_str)
                idx += 1
                m_trades_count += 1
                
                if t_date_str != m_current_day:
                    m_current_day = t_date_str
                    m_day_start_balance = m_balance
                    
                max_r = trade["max_r"]
                
                # Check trade outcome in Master
                if m_phase == 1:
                    if max_r >= M_P1_RR:
                        m_balance += M_P1_RISK * M_P1_RR
                    else:
                        m_balance -= M_P1_RISK * 1.03
                else:
                    if max_r >= M_P2_RR:
                        m_balance += M_P2_RISK * M_P2_RR
                    else:
                        m_balance -= M_P2_RISK * 1.03
                        
                # Check Daily Loss
                if m_day_start_balance - m_balance >= DAILY_LOSS_LIMIT:
                    m_blown = True
                    break
                    
                # Check Static Max Loss ($9,000 floor)
                if m_balance <= 9000.0:
                    m_blown = True
                    break
                    
                # Check 14-day Bi-Weekly Payout Cycle
                days_since_payout = (t_date - last_payout_date).days
                if days_since_payout >= 14:
                    profit = m_balance - 10000.0
                    if profit >= 100.0:  # Min $100 gross payout threshold
                        payouts_count += 1
                        gross = profit
                        net = gross * PROFIT_SPLIT
                        total_gross_payouts += gross
                        total_net_payouts += net
                        
                        if m_phase == 1:
                            m_p1_payouts += 1
                            if m_p1_payouts >= M_P1_PAYOUTS_NEEDED:
                                m_phase = 2
                        else:
                            m_p2_payouts += 1
                            
                        # 4th Payout Fee Refund
                        if payouts_count == FEE_REFUND_AT_PAYOUT:
                            fee_refunded = CHALLENGE_FEE
                            total_net_payouts += fee_refunded
                            
                        m_balance = 10000.0
                        m_day_start_balance = 10000.0
                        last_payout_date = t_date
                        
            masters.append({
                "master_id": f"Master #{master_id:02d}",
                "start_date": m_start_date,
                "end_date": t_date_str,
                "trades": m_trades_count,
                "payouts_count": payouts_count,
                "gross_payouts": total_gross_payouts,
                "net_payouts": total_net_payouts,
                "fee_refunded": fee_refunded,
                "blown": m_blown
            })
            master_id += 1
            
    chal_df = pd.DataFrame(challenges)
    mast_df = pd.DataFrame(masters)
    
    total_challenges = len(chal_df)
    passed_challenges = chal_df["passed"].sum()
    pass_rate = (passed_challenges / total_challenges * 100) if total_challenges > 0 else 0
    total_challenge_fees = total_challenges * CHALLENGE_FEE
    
    total_net_cash = mast_df["net_payouts"].sum() if not mast_df.empty else 0
    total_refunds = mast_df["fee_refunded"].sum() if not mast_df.empty else 0
    net_overall_profit = total_net_cash - total_challenge_fees
    
    print("\n" + "=" * 65)
    print("   FUNDINGPIPS 2-STEP STANDARD ($10,000) BACKTEST ERGEBNISSE   ")
    print("=" * 65)
    print(f"Gestartete Challenges:       {total_challenges}")
    print(f"Bestandene Challenges (P1+P2): {passed_challenges} ({pass_rate:.1f} % Pass Rate)")
    print(f"Investierte Challenge-Geb.:  ${total_challenge_fees:,.2f}")
    print("-" * 65)
    print(f"Erzeugte Master Accounts:    {len(mast_df)}")
    if not mast_df.empty:
        print(f"Auszahlungen gesamt (Netto): ${total_net_cash:,.2f} (80% Profit Split)")
        print(f"Erhaltene Fee-Refunds:       ${total_refunds:,.2f}")
        print(f"Durchschn. Netto pro Master: ${mast_df['net_payouts'].mean():,.2f}")
        print(f"Durchschn. Payouts pro Master: {mast_df['payouts_count'].mean():.1f} Payouts")
        print(f"Maximaler Netto-Cash 1 Master: ${mast_df['net_payouts'].max():,.2f}")
    print("-" * 65)
    print(f"REINER NETTO-CASHFLOW:       ${net_overall_profit:,.2f}")
    roi = (net_overall_profit / total_challenge_fees * 100) if total_challenge_fees > 0 else 0
    print(f"Gesamt-ROI auf Gebuehren:    +{roi:,.1f} %")
    print("=" * 65)
    
    return chal_df, mast_df

chal_df, mast_df = simulate_fundingpips(trades_df)
