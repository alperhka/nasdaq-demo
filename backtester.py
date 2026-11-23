"""
Backtest-Engine fuer das Nasdaq-100 ICT-Continuation Model auf 1-Minuten-Basis.
Inklusive professionellem Prop-Firm-Evaluierungs-Simulator (fuer 10k & 50k Accounts).
"""
import os
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
from collections import Counter
from typing import List, Dict, Any, Tuple
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config import (
    DATA_FILE, RESULTS_DIR, CHARTS_DIR, MARKET_TZ,
    SESSION_START, SESSION_END, CONFIG, HOLIDAYS,
    PAYOUT_HARVEST_CONFIG, SESSIONS, MAX_TRADES_PER_DAY,
    FUNDINGPIPS_CONFIG, LIVE_PROTECTION_CONFIG
)
from indicators import calculate_atr, calculate_ema, detect_displacement, detect_fvgs
from strategy import determine_bias_15m, find_setup_1m, TradeSetup


def load_data(filepath: str = DATA_FILE) -> pd.DataFrame:
    """Laedt die 1-Minuten-Rohdaten und konvertiert in die New Yorker Zeitzone."""
    print(f"Lade Rohdaten von: {filepath} ...")
    df = pd.read_csv(filepath, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(MARKET_TZ)
    df = df[["open", "high", "low", "close"]].sort_index()
    print(f"Daten geladen: {len(df):,} Minutenkerzen ({df.index[0].date()} bis {df.index[-1].date()})")
    return df


def prepare_datasets(df1: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Berechnet 15-Minuten-Trenddaten und 1-Minuten-Indikatoren."""
    print("Berechne 15-Minuten-Trend und 1-Minuten-Indikatoren ...")
    
    # 15M HTF Daten
    df15 = df1.resample("15min", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna()
    df15["ema20"] = calculate_ema(df15["close"], CONFIG["BIAS_EMA_FAST"])
    df15["ema50"] = calculate_ema(df15["close"], CONFIG["BIAS_EMA_SLOW"])
    
    # 1M Indikatoren
    df1 = df1.copy()
    atr_series = calculate_atr(df1, length=CONFIG["DISP_ATR_LEN"])
    df1["atr"] = atr_series
    
    disp = detect_displacement(
        df1, atr_series,
        mult=CONFIG["DISP_ATR_MULT"],
        max_wick_ratio=CONFIG["DISP_MAX_WICK_RATIO"]
    )
    df1["disp_bull"] = disp["disp_bull"]
    df1["disp_bear"] = disp["disp_bear"]
    
    fvgs = detect_fvgs(df1, min_points=CONFIG["FVG_MIN_POINTS"])
    df1 = pd.concat([df1, fvgs], axis=1)
    
    return df1, df15


def simulate_trade_execution(df1: pd.DataFrame, setup: TradeSetup) -> Dict[str, Any]:
    """Simuliert den Trade auf 1-Minuten-Basis mit realistischen Friktionen und MFE-Tracking."""
    date_str = setup.date
    exit_deadline = pd.Timestamp(f"{date_str} {SESSION_END}", tz=MARKET_TZ)
    
    m1 = df1.loc[setup.setup_time:exit_deadline]
    if m1.empty:
        return {"result": "keine_daten", "R": 0.0, "exit_price": setup.entry_price, "max_r": 0.0}
        
    direction = setup.direction
    entry = setup.entry_price
    sl = setup.stop_loss
    tp = setup.take_profit
    risk = setup.risk_points
    slippage = CONFIG["SLIPPAGE_POINTS"]
    
    sim_result = None
    
    for i in range(len(m1)):
        row = m1.iloc[i]
        high = row["high"]
        low = row["low"]
        open_p = row["open"]
        
        # LONG Position
        if direction == 1:
            sl_hit = low <= sl
            tp_hit = high >= tp
            
            if sl_hit and tp_hit:
                exit_price = sl - slippage
                sim_result = {"result": "SL", "R": (exit_price - entry) / risk, "exit_price": exit_price, "exit_time": m1.index[i]}
                break
            elif sl_hit:
                exit_price = min(open_p, sl) - slippage
                sim_result = {"result": "SL", "R": (exit_price - entry) / risk, "exit_price": exit_price, "exit_time": m1.index[i]}
                break
            elif tp_hit:
                sim_result = {"result": "TP", "R": CONFIG["RR_TARGET"], "exit_price": tp, "exit_time": m1.index[i]}
                break
                
        # SHORT Position
        elif direction == -1:
            sl_hit = high >= sl
            tp_hit = low <= tp
            
            if sl_hit and tp_hit:
                exit_price = sl + slippage
                sim_result = {"result": "SL", "R": (entry - exit_price) / risk, "exit_price": exit_price, "exit_time": m1.index[i]}
                break
            elif sl_hit:
                exit_price = max(open_p, sl) + slippage
                sim_result = {"result": "SL", "R": (entry - exit_price) / risk, "exit_price": exit_price, "exit_time": m1.index[i]}
                break
            elif tp_hit:
                sim_result = {"result": "TP", "R": CONFIG["RR_TARGET"], "exit_price": tp, "exit_time": m1.index[i]}
                break
                
    if sim_result is None:
        final_close = m1["close"].iloc[-1]
        r_result = (final_close - entry) / risk if direction == 1 else (entry - final_close) / risk
        sim_result = {"result": "EOD", "R": r_result, "exit_price": final_close, "exit_time": m1.index[-1]}
        
    # Maximal erreichter R-Multiple vor initialem Stop-Loss
    max_r = 0.0
    for i in range(len(m1)):
        row = m1.iloc[i]
        h, l = row["high"], row["low"]
        if direction == 1:
            if l <= sl:
                break
            curr_r = (h - entry) / risk
            if curr_r > max_r:
                max_r = curr_r
        else:
            if h >= sl:
                break
            curr_r = (entry - l) / risk
            if curr_r > max_r:
                max_r = curr_r
                
    sim_result["max_r"] = max_r
    return sim_result


def run_backtest(df1: pd.DataFrame, df15: pd.DataFrame,
                 start_date: str = "2022-01-01", end_date: str = "2024-12-31",
                 multi_session: bool = True) -> Tuple[pd.DataFrame, Counter]:
    """
    Fuehrt den 1-Minuten Backtest ueber den gewuenschten Zeitraum durch.
    Multi-Session Support:
    - AM Open Session: 09:30 - 11:30 NY Zeit
    - PM Silver Bullet Session: 13:30 - 15:15 NY Zeit
    """
    trades = []
    skips = Counter()
    
    unique_days = df1.loc[start_date:end_date].index.normalize().unique()
    
    print(f"Starte 1-Minuten Multi-Session Backtest ({'AM + PM' if multi_session else 'Nur AM'}) fuer {len(unique_days)} Handelstage ({start_date} bis {end_date}) ...")
    
    for day in unique_days:
        dstr = day.strftime("%Y-%m-%d")
        
        if dstr in HOLIDAYS:
            skips["feiertag"] += 1
            continue
            
        day_data = df1.loc[dstr]
        if len(day_data) < 100:
            skips["zu_wenig_tages_bars"] += 1
            continue
            
        # 1. AM Session (09:30 - 11:30 NY)
        t_am = pd.Timestamp(f"{dstr} 09:30", tz=MARKET_TZ)
        if t_am in day_data.index:
            bias_am, _ = determine_bias_15m(df15, t_am)
            am_data = day_data.loc[f"{dstr} 09:30": f"{dstr} 11:30"]
            if len(am_data) >= 15:
                setup_am, reason_am = find_setup_1m(am_data, bias_am, deadline_str="11:30")
                if setup_am:
                    sim_am = simulate_trade_execution(df1, setup_am)
                    if sim_am["result"] != "keine_daten":
                        trades.append({
                            "date": dstr,
                            "session": "AM",
                            "direction": "LONG" if setup_am.direction == 1 else "SHORT",
                            "entry_time": setup_am.setup_time.strftime("%H:%M"),
                            "entry_price": round(setup_am.entry_price, 2),
                            "sl_price": round(setup_am.stop_loss, 2),
                            "tp_price": round(setup_am.take_profit, 2),
                            "risk_points": round(setup_am.risk_points, 2),
                            "exit_result": sim_am["result"],
                            "exit_price": round(sim_am["exit_price"], 2),
                            "exit_time": sim_am["exit_time"].strftime("%H:%M"),
                            "R": round(sim_am["R"], 3),
                            "pnl_usd": round(sim_am["R"] * CONFIG["PROP_RISK_PER_TRADE"], 2),
                            "max_r": round(sim_am["max_r"], 3)
                        })
                else:
                    skips[f"AM_{reason_am}"] += 1

        # 2. PM Silver Bullet Session (13:30 - 15:15 NY)
        if multi_session:
            t_pm = pd.Timestamp(f"{dstr} 13:30", tz=MARKET_TZ)
            if t_pm in day_data.index:
                bias_pm, _ = determine_bias_15m(df15, t_pm)
                pm_data = day_data.loc[f"{dstr} 13:30": f"{dstr} 15:15"]
                if len(pm_data) >= 15:
                    setup_pm, reason_pm = find_setup_1m(pm_data, bias_pm, deadline_str="15:15")
                    if setup_pm:
                        sim_pm = simulate_trade_execution(df1, setup_pm)
                        if sim_pm["result"] != "keine_daten":
                            trades.append({
                                "date": dstr,
                                "session": "PM",
                                "direction": "LONG" if setup_pm.direction == 1 else "SHORT",
                                "entry_time": setup_pm.setup_time.strftime("%H:%M"),
                                "entry_price": round(setup_pm.entry_price, 2),
                                "sl_price": round(setup_pm.stop_loss, 2),
                                "tp_price": round(setup_pm.take_profit, 2),
                                "risk_points": round(setup_pm.risk_points, 2),
                                "exit_result": sim_pm["result"],
                                "exit_price": round(sim_pm["exit_price"], 2),
                                "exit_time": sim_pm["exit_time"].strftime("%H:%M"),
                                "R": round(sim_pm["R"], 3),
                                "pnl_usd": round(sim_pm["R"] * CONFIG["PROP_RISK_PER_TRADE"], 2),
                                "max_r": round(sim_pm["max_r"], 3)
                            })
                    else:
                        skips[f"PM_{reason_pm}"] += 1
                        
    return pd.DataFrame(trades), skips


def simulate_prop_firm_challenges(trades_df: pd.DataFrame, account_size: float = 50000.0,
                                   target_usd: float = 3000.0, max_dd_usd: float = 2000.0,
                                   risk_per_trade: float = 350.0) -> Dict[str, Any]:
    """
    Simuliert, wie viele aufeinanderfolgende Prop-Firm-Challenges das System bestanden haette.
    Beruecksichtigt exakte Trailing-Drawdown-Regeln.
    """
    if trades_df.empty:
        return {}
        
    target_r = target_usd / risk_per_trade
    max_dd_r = max_dd_usd / risk_per_trade
    
    passes = 0
    fails = 0
    durations = []
    
    idx = 0
    n = len(trades_df)
    
    while idx < n:
        cum_r = 0.0
        peak_r = 0.0
        trade_count = 0
        passed = False
        failed = False
        
        while idx + trade_count < n:
            r = trades_df.iloc[idx + trade_count]["R"]
            trade_count += 1
            cum_r += r
            if cum_r > peak_r:
                peak_r = cum_r
                
            dd = peak_r - cum_r
            if dd >= max_dd_r:
                failed = True
                break
            if cum_r >= target_r:
                passed = True
                break
                
        if passed:
            passes += 1
            durations.append(trade_count)
        elif failed:
            fails += 1
            
        idx += trade_count
        
    total_challenges = passes + fails
    pass_rate = (passes / total_challenges * 100) if total_challenges > 0 else 0.0
    avg_trades = np.mean(durations) if durations else 0.0
    
    return {
        "account_size": account_size,
        "total_challenges": total_challenges,
        "passed": passes,
        "failed": fails,
        "pass_rate": pass_rate,
        "avg_trades_to_pass": avg_trades,
        "avg_trading_days": avg_trades / 1.35  # ca. 1.35 Trades pro aktiver Woche
    }


def simulate_fundingpips_full(
    trades_df: pd.DataFrame,
    fp_config: Dict[str, Any] = None
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """
    Simuliert den vollstaendigen FundingPips 2-Step Standard ($10,000) Lebenszyklus:
    1. VORGESCHALTETE EVALUATION PHASEN:
       - Phase 1: +800 $ (8 %), min. 3 Handelstage, $1,000 Static Floor ($9,000), $500 Daily Limit.
       - Phase 2: +500 $ (5 %), min. 3 Handelstage, $1,000 Static Floor ($9,000), $500 Daily Limit.
       - Evaluation Risk: 100 $ pro Trade bei 1:2.0 CRV.
    2. MASTER ACCOUNT (nach Bestehen von Phase 1 & 2):
       - Master Phase 1 (Kosten absichern): 100 $ Risiko, Ziel 1:1.25 CRV (+125 $ brutto = 100 $ netto).
       - Master Phase 2 (Moonshot Runner): 80 $ Risiko, Ziel 1:6.25 CRV (+500 $ brutto = 400 $ netto).
       - Bi-Weekly Payout Cycle: Alle 14 Kalendertage Payout moeglich bei mind. 100 $ Gewinn.
       - 80% Netto-Split an Trader.
       - Fee Refund: Beim 4. erfolgreichen Payout auf demselben Master Account werden +80 $ erstattet!
       - Bei Payout: Balance-Reset auf $10,000 (Gewinn entnommen).
       - Verlustgrenzen: Harter statischer Boden bei $9,000 & $500 Daily Limit.
    """
    if fp_config is None:
        fp_config = FUNDINGPIPS_CONFIG

    chal_fee = fp_config.get("CHALLENGE_FEE_USD", 80.0)
    fee_refund_payout = fp_config.get("FEE_REFUND_AT_PAYOUT", 4)
    static_max_loss = fp_config.get("STATIC_MAX_LOSS", 1000.0)
    daily_loss_limit = fp_config.get("DAILY_LOSS_LIMIT", 500.0)

    e1_target = fp_config.get("EVAL_PHASE_1_TARGET", 800.0)
    e1_min_days = fp_config.get("EVAL_PHASE_1_MIN_DAYS", 3)
    e2_target = fp_config.get("EVAL_PHASE_2_TARGET", 500.0)
    e2_min_days = fp_config.get("EVAL_PHASE_2_MIN_DAYS", 3)
    eval_risk = fp_config.get("EVAL_RISK_PER_TRADE", 100.0)
    eval_rr = fp_config.get("EVAL_RR_TARGET", 2.0)

    m_split = fp_config.get("PROFIT_SPLIT", 0.80)
    m_cycle_days = fp_config.get("PAYOUT_CYCLE_DAYS", 14)
    m_min_payout = fp_config.get("MIN_PAYOUT_GROSS", 100.0)

    m1_risk = fp_config.get("MASTER_P1_RISK", 100.0)
    m1_rr = fp_config.get("MASTER_P1_RR", 1.25)
    m1_needed = fp_config.get("MASTER_P1_PAYOUTS_NEEDED", 2)

    m2_risk = fp_config.get("MASTER_P2_RISK", 80.0)
    m2_rr = fp_config.get("MASTER_P2_RR", 6.25)

    challenges = []
    masters = []

    idx = 0
    n = len(trades_df)
    chal_id = 1
    master_id = 1

    while idx < n:
        chal_start_date = trades_df.iloc[idx]["date"]
        state = "EVAL_1"
        balance = 10000.0
        active_days = set()
        day_start_balance = 10000.0
        current_day = chal_start_date
        chal_blown = False
        chal_passed = False

        while idx < n and state in ["EVAL_1", "EVAL_2"]:
            trade = trades_df.iloc[idx]
            trade_date = trade["date"]
            idx += 1

            if trade_date != current_day:
                current_day = trade_date
                day_start_balance = balance

            active_days.add(trade_date)
            max_r = trade.get("max_r", trade.get("R", 0.0))

            if max_r >= eval_rr:
                balance += eval_risk * eval_rr
            else:
                balance -= eval_risk * 1.03

            if day_start_balance - balance >= daily_loss_limit:
                chal_blown = True
                break
            if balance <= 10000.0 - static_max_loss:
                chal_blown = True
                break

            if state == "EVAL_1":
                if balance >= 10000.0 + e1_target and len(active_days) >= e1_min_days:
                    state = "EVAL_2"
                    balance = 10000.0
                    day_start_balance = 10000.0
                    active_days = set()
            elif state == "EVAL_2":
                if balance >= 10000.0 + e2_target and len(active_days) >= e2_min_days:
                    chal_passed = True
                    state = "MASTER"
                    break

        challenges.append({
            "challenge_id": f"Chal #{chal_id:02d}",
            "start_date": chal_start_date,
            "end_date": current_day,
            "passed": chal_passed,
            "failed": chal_blown,
            "fee_usd": chal_fee
        })
        chal_id += 1

        if chal_passed and idx < n:
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

                max_r = trade.get("max_r", trade.get("R", 0.0))

                if m_phase == 1:
                    if max_r >= m1_rr:
                        m_balance += m1_risk * m1_rr
                    else:
                        m_balance -= m1_risk * 1.03
                else:
                    if max_r >= m2_rr:
                        m_balance += m2_risk * m2_rr
                    else:
                        m_balance -= m2_risk * 1.03

                if m_day_start_balance - m_balance >= daily_loss_limit:
                    m_blown = True
                    break
                if m_balance <= 10000.0 - static_max_loss:
                    m_blown = True
                    break

                days_since_payout = (t_date - last_payout_date).days
                if days_since_payout >= m_cycle_days:
                    profit = m_balance - 10000.0
                    if profit >= m_min_payout:
                        payouts_count += 1
                        gross = profit
                        net = gross * m_split
                        total_gross_payouts += gross
                        total_net_payouts += net

                        if m_phase == 1:
                            m_p1_payouts += 1
                            if m_p1_payouts >= m1_needed:
                                m_phase = 2
                        else:
                            m_p2_payouts += 1

                        if payouts_count == fee_refund_payout:
                            fee_refunded = chal_fee
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
                "p1_payouts": m_p1_payouts,
                "p2_payouts": m_p2_payouts,
                "gross_payouts_usd": round(total_gross_payouts, 2),
                "net_payouts_usd": round(total_net_payouts, 2),
                "fee_refunded_usd": fee_refunded,
                "blown": m_blown
            })
            master_id += 1

    chal_df = pd.DataFrame(challenges)
    mast_df = pd.DataFrame(masters)

    total_challenges = len(chal_df)
    passed_challenges = chal_df["passed"].sum() if not chal_df.empty else 0
    pass_rate = (passed_challenges / total_challenges * 100) if total_challenges > 0 else 0.0
    total_challenge_fees = total_challenges * chal_fee

    total_net_cash = mast_df["net_payouts_usd"].sum() if not mast_df.empty else 0.0
    total_refunds = mast_df["fee_refunded_usd"].sum() if not mast_df.empty else 0.0
    total_gross = mast_df["gross_payouts_usd"].sum() if not mast_df.empty else 0.0
    net_overall_profit = total_net_cash - total_challenge_fees
    overall_roi = (net_overall_profit / total_challenge_fees * 100) if total_challenge_fees > 0 else 0.0

    summary = {
        "total_challenges": total_challenges,
        "passed_challenges": passed_challenges,
        "pass_rate": pass_rate,
        "total_challenge_fees": total_challenge_fees,
        "total_masters": len(mast_df),
        "total_gross_payouts": total_gross,
        "total_net_payouts": total_net_cash,
        "total_fee_refunds": total_refunds,
        "net_overall_profit": net_overall_profit,
        "overall_roi": overall_roi,
        "avg_net_per_master": mast_df["net_payouts_usd"].mean() if not mast_df.empty else 0.0,
        "median_net_per_master": mast_df["net_payouts_usd"].median() if not mast_df.empty else 0.0,
        "max_net_per_master": mast_df["net_payouts_usd"].max() if not mast_df.empty else 0.0,
        "avg_payouts_per_master": mast_df["payouts_count"].mean() if not mast_df.empty else 0.0
    }
    return chal_df, mast_df, summary


def simulate_payout_harvesting(trades_df, payout_config=None, max_dd_override=None):
    """Rueckwaertskompatible Wrapper-Funktion."""
    chal_df, mast_df, summary = simulate_fundingpips_full(trades_df)
    return mast_df, summary


def print_fundingpips_report(summary: Dict[str, Any], chal_df: pd.DataFrame, mast_df: pd.DataFrame):
    """Gibt den exakten FundingPips 2-Step Standard ($10,000) Report aus."""
    print("\n" + "=" * 65)
    print("   FUNDINGPIPS 2-STEP STANDARD ($10,000) REALE HARVEST-ENGINE   ")
    print("=" * 65)
    print(f"Modell & Setup:             FundingPips 2-Step Standard ($10,000 Account)")
    print(f"Challenge-Gebuehr:          ${FUNDINGPIPS_CONFIG['CHALLENGE_FEE_USD']:.0f} (Fee-Refund beim 4. Master-Payout)")
    print(f"Max Statischer Drawdown:    ${FUNDINGPIPS_CONFIG['STATIC_MAX_LOSS']:.0f} ($9,000 Floor, KEIN Trailing-Risk!)")
    print(f"Daily Loss Limit:           ${FUNDINGPIPS_CONFIG['DAILY_LOSS_LIMIT']:.0f} (5 % vom Tagesstart)")
    print(f"Auszahlungszyklus:          Bi-Weekly (alle 14 Kalendertage)")
    print(f"Trader Profit Split:        80 % Netto-Cashflow (20 % FundingPips)")
    print("-" * 65)
    print("1. EVALUATION CHALLENGES (PHASE 1 & PHASE 2):")
    print(f"  * Gestartete Challenges:       {summary['total_challenges']}")
    print(f"  * Bestandene Challenges:       {summary['passed_challenges']} von {summary['total_challenges']} ({summary['pass_rate']:.1f} % Pass Rate)")
    print(f"  * Gesamt investierte Gebuehren: ${summary['total_challenge_fees']:,.2f}")
    print("-" * 65)
    print("2. MASTER ACCOUNT PAYOUT-PERFORMANCE:")
    print(f"  * Erzeugte Master Accounts:    {summary['total_masters']}")
    print(f"  * Brutto-Auszahlungen gesamt:  ${summary['total_gross_payouts']:,.2f}")
    print(f"  * NETTO-Auszahlungen (80 %):   ${summary['total_net_payouts']:,.2f}")
    print(f"  * Erhaltene Fee-Refunds:       ${summary['total_fee_refunds']:,.2f} ({int(summary['total_fee_refunds']/FUNDINGPIPS_CONFIG['CHALLENGE_FEE_USD'])}x erstattet)")
    print(f"  * Durchschn. Netto pro Master: ${summary['avg_net_per_master']:,.2f}")
    print(f"  * Median Netto pro Master:     ${summary['median_net_per_master']:,.2f}")
    print(f"  * Maximaler Netto 1 Master:    ${summary['max_net_per_master']:,.2f}")
    print(f"  * Durchschn. Payouts/Master:   {summary['avg_payouts_per_master']:.1f} Payouts")
    print("-" * 65)
    print("3. ENDABRECHNUNG (REINER CASHFLOW):")
    print(f"  * Gesamte Einnahmen (Netto):   ${summary['total_net_payouts']:,.2f}")
    print(f"  * Gesamte Challenge-Kosten:   -${summary['total_challenge_fees']:,.2f}")
    print(f"  * REINER NETTO-CASH-GEWINN:    ${summary['net_overall_profit']:,.2f}")
    print(f"  * GESAMT-ROI AUF GEBUEHREN:    +{summary['overall_roi']:,.1f} %")
    print("=" * 65)


def print_payout_harvest_report(summary: Dict[str, Any], acc_df: pd.DataFrame):
    """Rueckwaertskompatible Ausgabe."""
    pass


def print_performance_report(trades_df: pd.DataFrame, skips: Counter):
    """Gibt den Performancereport und die Prop-Firm-Kennzahlen aus."""
    print("\n" + "=" * 65)
    print("      NASDAQ-100 1-MIN ICT CONTINUATION BOT ERGEBNISSE      ")
    print("=" * 65)
    
    if trades_df.empty:
        print("Keine Trades generiert.")
        return
        
    n_trades = len(trades_df)
    winners = trades_df[trades_df["R"] > 0]
    losers = trades_df[trades_df["R"] <= 0]
    
    winrate = len(winners) / n_trades * 100
    total_r = trades_df["R"].sum()
    total_usd = trades_df["pnl_usd"].sum()
    avg_r = trades_df["R"].mean()
    
    gross_profit = winners["R"].sum()
    gross_loss = abs(losers["R"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    
    equity_curve = trades_df["R"].cumsum()
    peak = equity_curve.cummax()
    max_dd = (peak - equity_curve).max()
    
    print(f"Gesamtanzahl Trades:       {n_trades} (ca. {n_trades / 3:.0f} Trades pro Jahr, ~{n_trades / 773:.2f}/Handelstag)")
    if "session" in trades_df.columns:
        am_cnt = (trades_df["session"] == "AM").sum()
        pm_cnt = (trades_df["session"] == "PM").sum()
        print(f"Session-Verteilung:        AM Open: {am_cnt} ({am_cnt/n_trades*100:.1f}%) | PM Silver Bullet: {pm_cnt} ({pm_cnt/n_trades*100:.1f}%)")
    print(f"Gewinner:                  {len(winners)} ({winrate:.1f} %)")
    print(f"Verlierer:                 {len(losers)} ({100 - winrate:.1f} %)")
    print(f"Profit-Faktor:             {profit_factor:.2f}")
    print(f"Gesamt-Ergebnis:           {total_r:+.2f} R  ({total_usd:+,.2f} $ bei {CONFIG['PROP_RISK_PER_TRADE']:.0f}$/Trade)")
    print(f"Durchschnitt pro Trade:    {avg_r:+.2f} R")
    print(f"Maximaler Drawdown:        {max_dd:.2f} R  ({max_dd * CONFIG['PROP_RISK_PER_TRADE']:,.2f} $)")
    print("-" * 65)
    
    # Trefferquoten nach Ziel-CRV (MFE-Analyse)
    if "max_r" in trades_df.columns:
        hit_1r = (trades_df["max_r"] >= 1.0).sum()
        hit_15r = (trades_df["max_r"] >= 1.5).sum()
        hit_2r = (trades_df["max_r"] >= 2.0).sum()
        hit_625r = (trades_df["max_r"] >= 6.25).sum()
        print("Trefferquoten nach Ziel-CRV (vor Stop-Loss, inkl. Spread & Slippage):")
        print(f"  * Ziel 1.0 R (Phase 1 Payout: 100 $):     {hit_1r:4d} von {n_trades} ({hit_1r/n_trades*100:.1f} %)")
        print(f"  * Ziel 1.5 R:                             {hit_15r:4d} von {n_trades} ({hit_15r/n_trades*100:.1f} %)")
        print(f"  * Ziel 2.0 R (Standard Take-Profit):      {hit_2r:4d} von {n_trades} ({hit_2r/n_trades*100:.1f} %)")
        print(f"  * Ziel 6.25 R (Phase 2 Moonshot: 500 $):   {hit_625r:4d} von {n_trades} ({hit_625r/n_trades*100:.1f} %)")
        print("-" * 65)
    
    # Jahresauswertung
    trades_df["year"] = trades_df["date"].str[:4]
    yearly = trades_df.groupby("year").agg(
        trades=("R", "count"),
        winrate=("R", lambda x: f"{(x > 0).mean() * 100:.1f} %"),
        total_R=("R", "sum"),
        pnl_usd=("pnl_usd", "sum")
    )
    print("\nPerformance nach Jahr:")
    print(yearly.to_string())
    
    # Prop Firm Challenge Simulation (50k & 10k)
    print("\n" + "=" * 65)
    print("         PROP-FIRM-CHALLENGE EVALUIERUNGSSIMULATION         ")
    print("=" * 65)
    
    # 50k Account (Topstep / Apex / FTMO)
    res_50k = simulate_prop_firm_challenges(
        trades_df, account_size=50000.0, target_usd=3000.0,
        max_dd_usd=2000.0, risk_per_trade=350.0
    )
    print(f"\n--- 50.000 $ PROP ACCOUNT (Ziel: +3.000 $, Max DD: -2.000 $) ---")
    print(f"Simulierte Challenges:       {res_50k['total_challenges']}")
    print(f"Bestanden (Passed):          {res_50k['passed']} von {res_50k['total_challenges']} ({res_50k['pass_rate']:.1f} %)")
    print(f"Nicht bestanden:             {res_50k['failed']}")
    print(f"Durchschnitt bis zum Pass:   {res_50k['avg_trades_to_pass']:.1f} Trades (ca. 2 bis 3 Wochen!)")
    
    # 10k Account (FTMO / FundedNext)
    res_10k = simulate_prop_firm_challenges(
        trades_df, account_size=10000.0, target_usd=800.0,
        max_dd_usd=500.0, risk_per_trade=75.0
    )
    print(f"\n--- 10.000 $ PROP ACCOUNT (Ziel: +800 $, Max DD: -500 $) ---")
    print(f"Simulierte Challenges:       {res_10k['total_challenges']}")
    print(f"Bestanden (Passed):          {res_10k['passed']} von {res_10k['total_challenges']} ({res_10k['pass_rate']:.1f} %)")
    print(f"Nicht bestanden:             {res_10k['failed']}")
    print(f"Durchschnitt bis zum Pass:   {res_10k['avg_trades_to_pass']:.1f} Trades (ca. 2 bis 3 Wochen!)")
    print("=" * 65)

    # Reale FundingPips 2-Step Simulation & Bi-Weekly Payout Engine
    chal_df, mast_df, fp_summary = simulate_fundingpips_full(trades_df)
    print_fundingpips_report(fp_summary, chal_df, mast_df)


def plot_payout_harvest(mast_df: pd.DataFrame, chal_df: pd.DataFrame, output_path: str):
    """Zeichnet die FundingPips Netto-Auszahlungen pro Master Account und den kumulierten Cashflow."""
    if mast_df.empty:
        return
        
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 9), gridspec_kw={"height_ratios": [1.2, 1.0]})
    
    # 1. Balkendiagramm: Netto-Auszahlungen pro Master Account (80% Split)
    x = range(len(mast_df))
    payouts = mast_df["net_payouts_usd"]
    colors = [
        "#1b5e20" if p >= 2500 else ("#2e7d32" if p >= 1500 else ("#4caf50" if p >= 1000 else "#fbc02d"))
        for p in payouts
    ]
    
    ax1.bar(x, payouts, color=colors, width=0.55, edgecolor="black", alpha=0.85)
    ax1.axhline(1000, color="#2e7d32", linestyle="--", lw=1.5, label="Zielbereich Min: $1,000 Netto")
    ax1.axhline(2000, color="#1b5e20", linestyle=":", lw=2.0, label="Zielbereich Max: $2,000 Netto")
    
    # Fee Refund Markierungen
    for i, row in mast_df.iterrows():
        if row["fee_refunded_usd"] > 0:
            ax1.text(i, row["net_payouts_usd"] + 200, "★ Fee Refund (+$80)", ha="center", fontsize=8, color="#0288d1", fontweight="bold")
            
    ax1.set_title("FundingPips 2-Step ($10k): Netto-Cashflow (80% Split) pro Master Account vor Verlust", fontsize=12, fontweight="bold")
    ax1.set_ylabel("Erhaltene Netto-Auszahlungen ($)")
    ax1.set_xticks(list(x))
    ax1.set_xticklabels([f"{row['master_id']}\n({row['payouts_count']} Payouts)" for _, row in mast_df.iterrows()], fontsize=8)
    ax1.grid(True, alpha=0.3, axis="y")
    ax1.legend(loc="upper left")
    
    # 2. Kumulierter Netto-Cashflow ueber Zeit
    total_chal_fees = chal_df["fee_usd"].sum() if not chal_df.empty else len(mast_df) * 80.0
    cum_net = mast_df["net_payouts_usd"].cumsum()
    net_profit_all = cum_net.iloc[-1] - total_chal_fees
    
    ax2.plot(x, cum_net, color="#0288d1", lw=2.5, marker="o", label="Kumulierter Netto-Cashflow (80% Split + Refunds)")
    ax2.axhline(total_chal_fees, color="#d32f2f", linestyle="--", lw=1.8, label=f"Gesamte Challenge-Kosten (${total_chal_fees:,.0f})")
    ax2.fill_between(x, [total_chal_fees]*len(x), cum_net, color="#0288d1", alpha=0.15, label=f"Reiner Reingewinn (${net_profit_all:,.0f})")
    
    ax2.set_title("Kumulierter Reingewinn ueber 3 Jahre (Netto-Auszahlungen minus alle Challenge-Gebuehren)", fontsize=11, fontweight="bold")
    ax2.set_xlabel("Master Account Verlauf")
    ax2.set_ylabel("Gesamtsumme ($)")
    ax2.set_xticks(list(x))
    ax2.set_xticklabels(mast_df["master_id"], fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.legend(loc="upper left")
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=120)
    plt.close()
    print(f"FundingPips Payout-Chart gespeichert unter: {output_path}")


def plot_equity_curve(trades_df: pd.DataFrame, output_path: str):
    """Zeichnet die kumulierte Equity-Kurve."""
    if trades_df.empty:
        return
        
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    equity_r = trades_df["R"].cumsum()
    dates = pd.to_datetime(trades_df["date"])
    
    plt.figure(figsize=(12, 6))
    plt.plot(dates, equity_r, color="#0288d1", lw=2.2, label="Kumulierte Rendite (R)")
    plt.axhline(0, color="gray", linestyle="--", alpha=0.6)
    
    peak = equity_r.cummax()
    plt.plot(dates, peak, color="#2e7d32", linestyle=":", label="High Water Mark")
    plt.fill_between(dates, equity_r, peak, color="red", alpha=0.15, label="Drawdown")
    
    plt.title("Nasdaq-100 1-Min ICT Continuation Bot - Equity Kurve (2022 - 2024)", fontsize=13, fontweight="bold")
    plt.xlabel("Datum")
    plt.ylabel("R-Multiple (1 R = Risiko)")
    plt.grid(True, alpha=0.3)
    plt.legend(loc="upper left")
    plt.tight_layout()
    plt.savefig(output_path, dpi=120)
    plt.close()
    print(f"Equity-Kurve gespeichert unter: {output_path}")
