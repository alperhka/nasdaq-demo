"""
Technische Bausteine fuer das ICT Continuation Model:
- ATR (Average True Range)
- EMA (Exponential Moving Average) fuer HTF Bias
- Displacement (Kraftvolle Ausbruchskerzen)
- FVG (Fair Value Gaps / Imbalancen)
- Swing Highs / Swing Lows (Marktstruktur)
"""
import numpy as np
import pandas as pd


def calculate_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Berechnet die True Range und den rollierenden Durchschnitt."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def calculate_ema(series: pd.Series, span: int) -> pd.Series:
    """Berechnet den exponentiellen Durchschnitt."""
    return series.ewm(span=span, adjust=False).mean()


def detect_fvgs(df: pd.DataFrame, min_points: float = 3.0) -> pd.DataFrame:
    """
    Erkennt Fair Value Gaps (3-Kerzen-Imbalance):
    - Bullish FVG: Tief von Kerze i liegt ueber Hoch von Kerze i-2.
    - Bearish FVG: Hoch von Kerze i liegt unter Tief von Kerze i-2.
    Gueltig ab dem Schluss von Kerze i (kein Blick in die Zukunft).
    """
    out = pd.DataFrame(index=df.index)
    
    h2 = df["high"].shift(2)
    l2 = df["low"].shift(2)
    
    # Bullische FVG
    bull_gap = df["low"] - h2
    bull_valid = (bull_gap >= min_points) & (df["close"].shift(1) > df["open"].shift(1))
    out["fvg_bull"] = bull_valid
    out["fvg_bull_bottom"] = h2.where(bull_valid)
    out["fvg_bull_top"] = df["low"].where(bull_valid)
    
    # Baerische FVG
    bear_gap = l2 - df["high"]
    bear_valid = (bear_gap >= min_points) & (df["close"].shift(1) < df["open"].shift(1))
    out["fvg_bear"] = bear_valid
    out["fvg_bear_top"] = l2.where(bear_valid)
    out["fvg_bear_bottom"] = df["high"].where(bear_valid)
    
    return out


def detect_displacement(df: pd.DataFrame, atr_series: pd.Series, 
                        mult: float = 1.2, max_wick_ratio: float = 0.35) -> pd.DataFrame:
    """
    Erkennt institutionelles Displacement:
    - Kerzenkoerper ist mindestens mult * ATR der Vorkerzen gross.
    - Kaum Docht in Ausbruchsrichtung (zeigt entschiedene Orderflow-Dynamik).
    """
    out = pd.DataFrame(index=df.index)
    body = (df["close"] - df["open"]).abs()
    threshold = mult * atr_series.shift(1)
    
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    
    # Bullisches Displacement
    bull_cond = (
        (df["close"] > df["open"]) &
        (body >= threshold) &
        (upper_wick <= max_wick_ratio * body)
    )
    
    # Baerisches Displacement
    bear_cond = (
        (df["close"] < df["open"]) &
        (body >= threshold) &
        (lower_wick <= max_wick_ratio * body)
    )
    
    out["disp_bull"] = bull_cond
    out["disp_bear"] = bear_cond
    return out


def detect_swings(df: pd.DataFrame, n: int = 2) -> pd.DataFrame:
    """
    Swing-Hoch / Swing-Tief (Fraktale):
    n Kerzen links und rechts niedriger (Hoch) bzw. hoeher (Tief).
    """
    h, l = df["high"], df["low"]
    left_h = pd.concat([h.shift(k) for k in range(1, n + 1)], axis=1).max(axis=1)
    right_h = pd.concat([h.shift(-k) for k in range(1, n + 1)], axis=1).max(axis=1)
    left_l = pd.concat([l.shift(k) for k in range(1, n + 1)], axis=1).min(axis=1)
    right_l = pd.concat([l.shift(-k) for k in range(1, n + 1)], axis=1).min(axis=1)
    
    out = pd.DataFrame(index=df.index)
    out["swing_high"] = h.where((h > left_h) & (h > right_h))
    out["swing_low"] = l.where((l < left_l) & (l < right_l))
    # 'bekannt' erst n Kerzen spaeter verfuegbar
    out["swing_high_known"] = out["swing_high"].shift(n)
    out["swing_low_known"] = out["swing_low"].shift(n)
    return out
