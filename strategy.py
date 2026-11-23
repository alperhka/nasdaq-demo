"""
ICT Continuation Strategie-Logik fuer NASDAQ-100 auf 1-Minuten-Basis:
1. 15-Minuten Trend / Bias Filter (Vor 09:30 geschlossen)
2. 1-Minuten Displacement & Fair Value Gap (FVG) im New-York-Fenster (09:30 - 11:00)
3. Limit-Order Retracement-Einstieg an der FVG-Kante
4. Konservatives Risikomanagement mit festem 1:2.0 CRV
"""
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any
import numpy as np
import pandas as pd
from config import CONFIG, SESSION_START, ENTRY_DEADLINE, SESSION_END, MARKET_TZ


@dataclass
class TradeSetup:
    date: str
    direction: int                # +1 fuer Long, -1 fuer Short
    setup_time: pd.Timestamp     # Zeitpunkt der FVG-Entstehung
    fvg_top: float
    fvg_bottom: float
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_points: float


def determine_bias_15m(df15: pd.DataFrame, t0: pd.Timestamp) -> Tuple[int, str]:
    """
    Ermittelt den uebergeordneten Momentum-Bias aus den 15-Minuten-Kerzen,
    die VOR t0 (09:30 NY Zeit) bereits vollstaendig geschlossen waren.
    """
    if not CONFIG["USE_BIAS_FILTER"]:
        return 0, "filter_deaktiviert"

    closed_15m = df15[df15.index + pd.Timedelta(minutes=15) <= t0]
    if len(closed_15m) < CONFIG["BIAS_EMA_SLOW"]:
        return 0, "zu_wenig_daten"

    last_row = closed_15m.iloc[-1]
    close = last_row["close"]
    ema20 = last_row["ema20"]
    ema50 = last_row["ema50"]

    if close > ema50 and ema20 > ema50:
        return 1, "bullish"
    elif close < ema50 and ema20 < ema50:
        return -1, "bearish"
    else:
        return 0, "neutral"


def find_setup_1m(session_1m: pd.DataFrame, bias: int, deadline_str: str = "11:30") -> Tuple[Optional[TradeSetup], str]:
    """
    Sucht auf 1-Minuten-Kerzen im Session-Fenster nach einem FVG-Retracement Setup.
    - Wenn bias == 1: nur Longs
    - Wenn bias == -1: nur Shorts
    - Wenn bias == 0 (neutral): Richtungsentscheidung durch die erste institutionelle Displacement-Kerze
    """
    active_fvg = None

    for i in range(len(session_1m)):
        current_time = session_1m.index[i]
        time_str = current_time.strftime("%H:%M")

        if time_str > deadline_str:
            break

        c = session_1m.iloc[i]

        # Wenn noch keine FVG aktiv ist: Suche nach neuer FVG mit Displacement
        if active_fvg is None:
            disp_bull = c.get("disp_bull", False) or (i > 0 and session_1m["disp_bull"].iloc[i-1])
            disp_bear = c.get("disp_bear", False) or (i > 0 and session_1m["disp_bear"].iloc[i-1])

            # LONG Setup: Erlaubt wenn Bias bullish (+1) oder neutral (0) mit bullischem Displacement
            if bias >= 0 and c.get("fvg_bull", False) and disp_bull:
                fvg_top = c["fvg_bull_top"]
                fvg_bot = c["fvg_bull_bottom"]
                
                sl = fvg_bot - CONFIG["STOP_BUFFER_POINTS"]
                entry = fvg_top + CONFIG["SPREAD_POINTS"]
                risk = entry - sl

                if CONFIG["MIN_STOP_POINTS"] <= risk <= CONFIG["MAX_STOP_POINTS"]:
                    active_fvg = {
                        "direction": 1,
                        "created_idx": i,
                        "setup_time": current_time,
                        "fvg_top": fvg_top,
                        "fvg_bot": fvg_bot,
                        "entry": entry,
                        "sl": sl,
                        "risk": risk,
                        "tp": entry + CONFIG["RR_TARGET"] * risk
                    }

            # SHORT Setup: Erlaubt wenn Bias bearish (-1) oder neutral (0) mit baerischem Displacement
            elif bias <= 0 and c.get("fvg_bear", False) and disp_bear:
                fvg_top = c["fvg_bear_top"]
                fvg_bot = c["fvg_bear_bottom"]

                sl = fvg_top + CONFIG["STOP_BUFFER_POINTS"]
                entry = fvg_bot - CONFIG["SPREAD_POINTS"]
                risk = sl - entry

                if CONFIG["MIN_STOP_POINTS"] <= risk <= CONFIG["MAX_STOP_POINTS"]:
                    active_fvg = {
                        "direction": -1,
                        "created_idx": i,
                        "setup_time": current_time,
                        "fvg_top": fvg_top,
                        "fvg_bot": fvg_bot,
                        "entry": entry,
                        "sl": sl,
                        "risk": risk,
                        "tp": entry - CONFIG["RR_TARGET"] * risk
                    }

        # Wenn eine FVG aktiv ist: Pruefe Retracement
        elif active_fvg is not None and i > active_fvg["created_idx"]:
            direction = active_fvg["direction"]

            # LONG Retracement
            if direction == 1:
                if c["low"] <= active_fvg["entry"]:
                    if c["low"] > active_fvg["sl"]:
                        return TradeSetup(
                            date=current_time.strftime("%Y-%m-%d"),
                            direction=1,
                            setup_time=current_time,
                            fvg_top=active_fvg["fvg_top"],
                            fvg_bottom=active_fvg["fvg_bot"],
                            entry_price=active_fvg["entry"],
                            stop_loss=active_fvg["sl"],
                            take_profit=active_fvg["tp"],
                            risk_points=active_fvg["risk"]
                        ), "ok"
                    else:
                        active_fvg = None  # Sofort durch Stop gefallen
                elif c["close"] > active_fvg["fvg_top"] + active_fvg["risk"] * 2.5:
                    active_fvg = None      # Kurs ohne Retracement weggelaufen

            # SHORT Retracement
            elif direction == -1:
                if c["high"] >= active_fvg["entry"]:
                    if c["high"] < active_fvg["sl"]:
                        return TradeSetup(
                            date=current_time.strftime("%Y-%m-%d"),
                            direction=-1,
                            setup_time=current_time,
                            fvg_top=active_fvg["fvg_top"],
                            fvg_bottom=active_fvg["fvg_bot"],
                            entry_price=active_fvg["entry"],
                            stop_loss=active_fvg["sl"],
                            take_profit=active_fvg["tp"],
                            risk_points=active_fvg["risk"]
                        ), "ok"
                    else:
                        active_fvg = None
                elif c["close"] < active_fvg["fvg_bot"] - active_fvg["risk"] * 2.5:
                    active_fvg = None

    if active_fvg is not None:
        return None, "fvg_ohne_retracement"
    return None, "keine_fvg_mit_displacement"
