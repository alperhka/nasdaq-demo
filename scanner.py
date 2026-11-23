"""
NASDAQ ICT Bot - Live Paper Trading Scanner
============================================
Laeuft direkt auf Mac, kein MT5, kein VPS noetig.
Holt echte NAS100 1-Minuten-Kerzen von Yahoo Finance
und zeigt in Echtzeit wann der Bot in einen Trade einsteigen wuerde.

Starten:
    .venv/bin/python scanner.py

Legende:
    [SCAN]   -> Bot scannt gerade
    [SETUP]  -> FVG + Displacement gefunden, wartet auf Retracement
    [ENTRY]  -> Jetzt waere der Einstieg! (Paper Trade)
    [WIN]    -> Take-Profit erreicht
    [LOSS]   -> Stop-Loss getroffen
    [INFO]   -> Session-Info / Status
"""

import sys
import time
import os
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

import pandas as pd
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from config import SESSIONS, FUNDINGPIPS_CONFIG as FP, CONFIG
from indicators import calculate_atr, calculate_ema, detect_displacement, detect_fvgs
from strategy import determine_bias_15m, find_setup_1m, TradeSetup

NY_TZ = ZoneInfo("America/New_York")

# ── Farben fuer Terminal ──────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def log(tag: str, msg: str, color: str = RESET):
    now = datetime.now(NY_TZ).strftime("%H:%M:%S")
    print(f"{color}{BOLD}[{tag}]{RESET} {now} NY | {msg}")

# ── Daten von Yahoo Finance ───────────────────────────────────────────────────

def get_live_bars(symbol: str = "NQ=F", n_bars: int = 200) -> Optional[pd.DataFrame]:
    """
    Holt die letzten n_bars 1-Minuten Kerzen von Yahoo Finance.
    NQ=F = NASDAQ-100 Futures (nahezu identisch mit dem NAS100 bei FundingPips)
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1d", interval="1m", auto_adjust=True)

        if df.empty:
            return None

        df.index = df.index.tz_convert(NY_TZ)
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df = df.tail(n_bars)
        return df

    except Exception as e:
        log("FEHLER", f"Yahoo Finance: {e}", RED)
        return None


# ── Indikatoren & Bias ───────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Berechnet Indikatoren 1:1 identisch zum Backtest."""
    df = df.copy()
    atr_series = calculate_atr(df, length=CONFIG["DISP_ATR_LEN"])
    df["atr"] = atr_series

    disp = detect_displacement(
        df, atr_series,
        mult=CONFIG["DISP_ATR_MULT"],
        max_wick_ratio=CONFIG["DISP_MAX_WICK_RATIO"]
    )
    df["disp_bull"] = disp["disp_bull"]
    df["disp_bear"] = disp["disp_bear"]

    fvgs = detect_fvgs(df, min_points=CONFIG["FVG_MIN_POINTS"])
    df = pd.concat([df, fvgs], axis=1)
    return df


def get_live_bias_15m(df_1m: pd.DataFrame, session_start_str: str) -> Tuple[int, str]:
    """Erzeugt 15M-Kerzen und ermittelt den Momentum-Bias vor dem Session-Start."""
    if df_1m.empty or len(df_1m) < 60:
        return 0, "zu_wenig_daten"

    df15 = df_1m.resample("15min", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna()

    df15["ema20"] = calculate_ema(df15["close"], CONFIG["BIAS_EMA_FAST"])
    df15["ema50"] = calculate_ema(df15["close"], CONFIG["BIAS_EMA_SLOW"])

    now_ny = datetime.now(NY_TZ)
    t0 = pd.Timestamp(f"{now_ny.strftime('%Y-%m-%d')} {session_start_str}", tz=NY_TZ)
    return determine_bias_15m(df15, t0)


# ── Setup-Erkennung ───────────────────────────────────────────────────────────

def find_setup(df: pd.DataFrame, session: str, rr: float, bias: int = 0) -> Optional[Dict]:
    """1:1 Backtest-Logik mit strategy.find_setup_1m()."""
    sess    = SESSIONS[session]
    start_h, start_m = map(int, sess["START"].split(":"))
    end_h,   end_m   = map(int, sess["DEADLINE"].split(":"))

    sdf = df[
        ((df.index.hour == start_h) & (df.index.minute >= start_m)) |
        (df.index.hour > start_h)
    ]
    sdf = sdf[
        ((sdf.index.hour == end_h) & (sdf.index.minute <= end_m)) |
        (sdf.index.hour < end_h)
    ]
    if len(sdf) < 5:
        return None

    setup_obj, status = find_setup_1m(sdf, bias=bias, deadline_str=sess["DEADLINE"])
    if setup_obj is None:
        return None

    direction = setup_obj.direction
    entry     = setup_obj.entry_price
    sl        = setup_obj.stop_loss
    risk      = setup_obj.risk_points

    if direction == 1:
        tp = entry + rr * risk
    else:
        tp = entry - rr * risk

    return {
        "direction":    direction,
        "entry":        entry,
        "sl":           sl,
        "tp":           tp,
        "risk_pts":     risk,
        "fvg_top":      setup_obj.fvg_top,
        "fvg_bot":      setup_obj.fvg_bottom,
        "triggered_at": setup_obj.setup_time,
        "waiting":      False,
    }


# ── Paper Trade Tracking ──────────────────────────────────────────────────────

class PaperTrade:
    def __init__(self, setup: Dict, risk_usd: float, rr: float):
        self.direction   = setup["direction"]
        self.entry       = setup["entry"]
        self.sl          = setup["sl"]
        self.tp          = setup["tp"]
        self.risk_pts    = setup["risk_pts"]
        self.risk_usd    = risk_usd
        self.rr          = rr
        self.reward_usd  = risk_usd * rr
        self.opened_at   = datetime.now(NY_TZ)
        self.status      = "open"  # open / win / loss
        self.pnl_usd     = 0.0
        dir_str          = "LONG 🟢" if self.direction == 1 else "SHORT 🔴"
        log("ENTRY", f"{dir_str} @ {self.entry:.2f} | SL: {self.sl:.2f} | TP: {self.tp:.2f} | Risiko: ${risk_usd:.0f} | Ziel: ${self.reward_usd:.0f}", GREEN if self.direction == 1 else RED)

    def check(self, high: float, low: float) -> bool:
        """Prueft ob SL oder TP getroffen. Gibt True zurueck wenn Trade beendet."""
        if self.status != "open":
            return True

        if self.direction == 1:
            if low <= self.sl:
                self.status  = "loss"
                self.pnl_usd = -self.risk_usd
                log("LOSS", f"Stop-Loss getroffen @ {self.sl:.2f} | -{self.risk_usd:.0f}$ 😢", RED)
                return True
            if high >= self.tp:
                self.status  = "win"
                self.pnl_usd = self.reward_usd
                log("WIN ", f"Take-Profit getroffen @ {self.tp:.2f} | +{self.reward_usd:.0f}$ 🎉", GREEN)
                return True
        else:
            if high >= self.sl:
                self.status  = "loss"
                self.pnl_usd = -self.risk_usd
                log("LOSS", f"Stop-Loss getroffen @ {self.sl:.2f} | -{self.risk_usd:.0f}$ 😢", RED)
                return True
            if low <= self.tp:
                self.status  = "win"
                self.pnl_usd = self.reward_usd
                log("WIN ", f"Take-Profit getroffen @ {self.tp:.2f} | +{self.reward_usd:.0f}$ 🎉", GREEN)
                return True

        return False


# ── Haupt-Scanner ─────────────────────────────────────────────────────────────

def run_scanner():
    print(f"\n{BOLD}{CYAN}{'='*60}{RESET}")
    print(f"{BOLD}{CYAN}  NASDAQ ICT BOT — LIVE PAPER SCANNER{RESET}")
    print(f"{BOLD}{CYAN}  Daten: NQ=F (NASDAQ Futures, Yahoo Finance){RESET}")
    print(f"{BOLD}{CYAN}  Kein echtes Geld — nur Signale!{RESET}")
    print(f"{BOLD}{CYAN}{'='*60}{RESET}\n")

    # Phase bestimmen (hier: Master Phase 2 Moonshot zum Anschauen)
    PHASE        = "MASTER_P2"
    RISK_USD     = FP["MASTER_P2_RISK"]   # $80
    RR           = FP["MASTER_P2_RR"]     # 6.25
    SYMBOL       = "NQ=F"

    log("INFO", f"Phase: {PHASE} | Risiko: ${RISK_USD:.0f}/Trade | CRV: 1:{RR}", CYAN)
    log("INFO", f"Sessions: AM {SESSIONS['AM']['START']}–{SESSIONS['AM']['DEADLINE']} | PM {SESSIONS['PM']['START']}–{SESSIONS['PM']['DEADLINE']} NY", CYAN)
    log("INFO", f"Starte Scanner... (Ctrl+C zum Beenden)\n", CYAN)

    # State
    daily_trades:   Dict[str, bool] = {"AM": False, "PM": False}
    active_trade:   Optional[PaperTrade] = None
    active_setup:   Optional[Dict] = None
    last_trade_day: Optional[date] = None
    total_pnl       = 0.0
    total_trades     = 0
    wins             = 0
    scan_count       = 0

    while True:
        try:
            now_ny   = datetime.now(NY_TZ)
            today    = now_ny.date()
            time_str = now_ny.strftime("%H:%M")

            # Taeglich Reset
            if last_trade_day != today:
                daily_trades    = {"AM": False, "PM": False}
                active_setup    = None
                last_trade_day  = today
                log("INFO", f"Neuer Handelstag: {today} | Gesamt PnL: ${total_pnl:.0f} | Trades: {total_trades}", BLUE)

            # Session bestimmen
            current_session = None
            for sess_name, sess_cfg in SESSIONS.items():
                if sess_cfg["START"] <= time_str <= sess_cfg["DEADLINE"]:
                    current_session = sess_name
                    break

            # Markt geschlossen?
            if current_session is None:
                if scan_count % 10 == 0:  # Nur alle 10 Zyklen loggen
                    log("INFO", f"Markt geschlossen | Naechste Session: AM {SESSIONS['AM']['START']} NY", YELLOW)
                scan_count += 1
                time.sleep(30)
                continue

            # Session schon getradet?
            if daily_trades[current_session]:
                if scan_count % 5 == 0:
                    log("INFO", f"{current_session} Session bereits getradet heute", YELLOW)
                scan_count += 1
                time.sleep(60)
                continue

            # Live-Daten holen (mind. 350 Kerzen fuer 15M EMA50 + 1M Indikatoren)
            df = get_live_bars(SYMBOL, n_bars=350)
            if df is None:
                log("FEHLER", "Keine Daten von Yahoo Finance", RED)
                time.sleep(30)
                continue

            # 15M Trend-Bias vor Session-Start berechnen
            bias, bias_desc = get_live_bias_15m(df, SESSIONS[current_session]["START"])

            # 1M Indikatoren vorbereiten (exakt wie Backtest)
            df = compute_indicators(df)

            # Aktiven Trade pruefen
            if active_trade and active_trade.status == "open":
                last_bar = df.iloc[-1]
                closed   = active_trade.check(last_bar["high"], last_bar["low"])
                if closed:
                    total_pnl    += active_trade.pnl_usd
                    total_trades += 1
                    if active_trade.status == "win":
                        wins += 1
                    daily_trades[current_session] = True
                    active_trade  = None
                    active_setup  = None
                    winrate = (wins / total_trades * 100) if total_trades > 0 else 0
                    log("STAT", f"Gesamt: {total_trades} Trades | {winrate:.0f}% Win | PnL: ${total_pnl:.0f}", BLUE)

            # Setup suchen (nur wenn kein aktiver Trade)
            if active_trade is None:
                setup = find_setup(df, current_session, RR, bias=bias)

                if setup:
                    # Retracement eingetreten -> Trade!
                    if not daily_trades[current_session]:
                        log("INFO", f"15M Trend-Bias: {bias} ({bias_desc})", CYAN)
                        active_trade = PaperTrade(setup, RISK_USD, RR)
                        daily_trades[current_session] = False
                else:
                    # Kein Setup
                    last_close = df.iloc[-1]["close"]
                    if scan_count % 3 == 0:
                        log("SCAN", f"{current_session} | Preis: {last_close:.2f} | 15M-Bias: {bias} ({bias_desc}) | Kein Setup...", RESET)

            scan_count += 1
            time.sleep(60)  # Jede Minute neu scannen

        except KeyboardInterrupt:
            print(f"\n{BOLD}Scanner beendet.{RESET}")
            print(f"Gesamt: {total_trades} Trades | Wins: {wins} | PnL: ${total_pnl:.0f}")
            break
        except Exception as e:
            log("FEHLER", f"{e}", RED)
            time.sleep(30)


if __name__ == "__main__":
    run_scanner()
