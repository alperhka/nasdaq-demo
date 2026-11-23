"""
Hauptprogramm: Startet den vollstaendigen 1-Minuten-Backtest fuer das ICT-Continuation Modell
inklusive Prop-Firm-Evaluierungs-Simulation.
Verwendung:
  .venv/bin/python run.py
"""
import os
import sys
import pandas as pd

from config import DATA_FILE, RESULTS_DIR
from backtester import (
    load_data, prepare_datasets, run_backtest, 
    print_performance_report, plot_equity_curve,
    simulate_fundingpips_full, plot_payout_harvest
)


def main():
    if not os.path.exists(DATA_FILE):
        print(f"Fehler: Datendatei nicht gefunden unter {DATA_FILE}")
        sys.exit(1)
        
    os.makedirs(RESULTS_DIR, exist_ok=True)
    
    # 1. Daten laden
    df1 = load_data(DATA_FILE)
    
    # 2. 15M Trend & 1M Indikatoren berechnen
    df1, df15 = prepare_datasets(df1)
    
    # 3. 1-Minuten-Backtest ausfuehren
    trades_df, skips = run_backtest(df1, df15, start_date="2022-01-01", end_date="2024-12-31")
    
    # 4. Report & Prop Firm Analyse ausgeben
    print_performance_report(trades_df, skips)
    
    # 5. Speichern & Charts generieren
    if not trades_df.empty:
        csv_path = os.path.join(RESULTS_DIR, "trades_ict_1min.csv")
        trades_df.to_csv(csv_path, index=False)
        print(f"\nAlle Trades als CSV gespeichert unter: {csv_path}")
        
        plot_path = os.path.join(RESULTS_DIR, "equity_curve_1min.png")
        plot_equity_curve(trades_df, plot_path)
        
        # FundingPips Reale Evaluation & Master Payout Simulation
        chal_df, mast_df, fp_summary = simulate_fundingpips_full(trades_df)
        
        chal_csv = os.path.join(RESULTS_DIR, "fundingpips_challenges.csv")
        chal_df.to_csv(chal_csv, index=False)
        print(f"Challenges-Historie gespeichert unter: {chal_csv}")
        
        mast_csv = os.path.join(RESULTS_DIR, "fundingpips_masters.csv")
        mast_df.to_csv(mast_csv, index=False)
        print(f"Master-Accounts-Historie gespeichert unter: {mast_csv}")
        
        # Rueckwaertskompatible CSV fuer Vorversionen
        acc_csv = os.path.join(RESULTS_DIR, "payout_harvest_accounts.csv")
        mast_df.to_csv(acc_csv, index=False)
        
        payout_chart = os.path.join(RESULTS_DIR, "payout_curve_10k.png")
        plot_payout_harvest(mast_df, chal_df, payout_chart)


if __name__ == "__main__":
    main()
