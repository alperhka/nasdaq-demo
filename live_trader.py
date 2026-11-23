"""
NASDAQ ICT Continuation Bot - Live Trader
==========================================
Vollautomatischer MT5-Live-Bot fuer FundingPips 10k 2-Step Standard Account.

Architektur:
  - 2 Sessions pro Tag: AM Open (09:30-11:30 NY) & PM Silver Bullet (13:30-15:15 NY)
  - Pro Session max. 1 Trade (Daily Max = 2 Trades)
  - Phasen-Tracking: Evaluation P1 -> Evaluation P2 -> Master (P1 safe -> P2 Moonshot)
  - Daily Loss Brake: Nach $500 Verlust am Tag -> kein weiterer Trade
  - Hard Close: 20:30 UTC (alle offenen Positionen zwangsgeschlossen)
  - News-Filter: Placeholder (TODO: ForexFactory-Scraper aus news_filter.py)

WICHTIG: Dieses Skript laueft AUSSCHLIESSLICH auf Windows mit MetaTrader5.
         Auf macOS: Code lesen / konfigurieren, dann auf Windows VPS deployen.

Starten:
    python live_trader.py

Umgebungsvariablen (EMPFOHLEN statt config.py editieren):
    MT5_LOGIN=12345678
    MT5_PASSWORD=DeinPasswort
    MT5_SERVER=FundingPips-Server
    BOT_PHASE=EVAL_P1    # EVAL_P1 | EVAL_P2 | MASTER_P1 | MASTER_P2

Oder .env Datei anlegen (wird automatisch geladen):
    echo "MT5_LOGIN=12345678" >> .env
    echo "MT5_PASSWORD=..." >> .env

Log-Datei: logs/live_trader_YYYY-MM-DD.log
State-Datei: state/bot_state.json (wird gespeichert und beim Neustart geladen)
"""

import os
import sys
import json
import time
import signal
import logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo  # Python 3.9+

# Pfad-Setup
BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))

# .env Datei laden falls vorhanden (manuell, ohne python-dotenv Abhaengigkeit)
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from config import (
    FUNDINGPIPS_CONFIG as FP,
    LIVE_PROTECTION_CONFIG as LPC,
    MT5_CONFIG,
    SESSIONS,
    MAX_TRADES_PER_DAY,
    MARKET_TZ,
    CONFIG,
)
import mt5_connector as connector
from indicators import calculate_atr, calculate_ema, detect_displacement, detect_fvgs
from strategy import determine_bias_15m, find_setup_1m, TradeSetup

# Strategie-Imports
import pandas as pd
import numpy as np

# ======================================================
# LOGGING SETUP
# ======================================================

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)

def setup_logging() -> logging.Logger:
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_file  = LOG_DIR / f"live_trader_{today_str}.log"

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler    = logging.FileHandler(log_file, encoding="utf-8")
    console_handler = logging.StreamHandler(sys.stdout)
    for h in (file_handler, console_handler):
        h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    return logging.getLogger("LiveTrader")


logger = setup_logging()


# ======================================================
# PHASEN-DEFINITIONEN
# ======================================================

PHASE_EVAL_P1   = "EVAL_P1"    # Evaluation Phase 1 (8% Ziel)
PHASE_EVAL_P2   = "EVAL_P2"    # Evaluation Phase 2 (5% Ziel)
PHASE_MASTER_P1 = "MASTER_P1"  # Master: sicherer Payout-Modus (1:1.25)
PHASE_MASTER_P2 = "MASTER_P2"  # Master: Moonshot-Modus (1:6.25)

PHASE_CONFIGS = {
    PHASE_EVAL_P1: {
        "risk_usd":    FP["EVAL_RISK_PER_TRADE"],    # $100
        "rr_target":   FP["EVAL_RR_TARGET"],          # 2.0
        "description": "Evaluation Phase 1 (Ziel: +$800 / 8%)",
    },
    PHASE_EVAL_P2: {
        "risk_usd":    FP["EVAL_RISK_PER_TRADE"],    # $100
        "rr_target":   FP["EVAL_RR_TARGET"],          # 2.0
        "description": "Evaluation Phase 2 (Ziel: +$500 / 5%)",
    },
    PHASE_MASTER_P1: {
        "risk_usd":    FP["MASTER_P1_RISK"],          # $100
        "rr_target":   FP["MASTER_P1_RR"],            # 1.25
        "description": "Master Phase 1 - Cost Recovery (Ziel: 2x Payouts a $100 netto)",
    },
    PHASE_MASTER_P2: {
        "risk_usd":    FP["MASTER_P2_RISK"],          # $80
        "rr_target":   FP["MASTER_P2_RR"],            # 6.25
        "description": "Master Phase 2 - MOONSHOT (1:6.25 | $80 -> $500 target)",
    },
}


# ======================================================
# ZUSTANDSVERWALTUNG (State Persistence)
# ======================================================

STATE_FILE = STATE_DIR / "bot_state.json"

DEFAULT_STATE = {
    # Phase & Account
    "phase":                    PHASE_EVAL_P1,
    "account_start_balance":    FP["ACCOUNT_SIZE"],  # 10000.0
    "phase_start_balance":      FP["ACCOUNT_SIZE"],
    "phase_start_date":         date.today().isoformat(),
    "phase_trading_days":       0,
    "last_trading_day":         None,

    # Master-Account Tracking
    "master_payout_count":      0,
    "master_total_net_payout":  0.0,
    "last_payout_date":         None,
    "fee_refund_received":      False,

    # Daily Tracking
    "today_date":               date.today().isoformat(),
    "today_start_balance":      FP["ACCOUNT_SIZE"],
    "today_trades":             0,
    "today_am_traded":          False,
    "today_pm_traded":          False,
    "today_pnl_usd":            0.0,
    "daily_loss_brake_active":  False,

    # Laufende Trade-Statistik
    "total_trades":             0,
    "total_wins":               0,
    "total_losses":             0,
    "total_pnl_usd":            0.0,

    # Trade-History (letzte 50)
    "trade_history":            [],
}


class BotState:
    """Verwaltet den persistenten Bot-Zustand (Datei-basiert)."""

    def __init__(self):
        self._state = self._load()

    def _load(self) -> Dict[str, Any]:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r") as f:
                    loaded = json.load(f)
                # Fehlende Keys mit Defaults auffuellen (fuer Updates)
                for k, v in DEFAULT_STATE.items():
                    if k not in loaded:
                        loaded[k] = v
                logger.info(f"Bot-State geladen aus {STATE_FILE}")
                return loaded
            except Exception as e:
                logger.warning(f"State-Datei konnte nicht geladen werden: {e} -> Neustart mit Defaults")
        return dict(DEFAULT_STATE)

    def save(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self._state, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"State konnte nicht gespeichert werden: {e}")

    def __getitem__(self, key):
        return self._state[key]

    def __setitem__(self, key, value):
        self._state[key] = value
        self.save()

    def get(self, key, default=None):
        return self._state.get(key, default)

    def reset_daily(self, current_balance: float):
        """Taglicher Reset (wird beim ersten Check eines neuen Tages aufgerufen)."""
        today = date.today().isoformat()
        self._state["today_date"]              = today
        self._state["today_start_balance"]     = current_balance
        self._state["today_trades"]            = 0
        self._state["today_am_traded"]         = False
        self._state["today_pm_traded"]         = False
        self._state["today_pnl_usd"]           = 0.0
        self._state["daily_loss_brake_active"] = False
        self.save()
        logger.info(f"Daily Reset | Balance: ${current_balance:.2f} | Datum: {today}")

    def print_summary(self):
        logger.info("=" * 60)
        logger.info("BOT STATUS ZUSAMMENFASSUNG")
        logger.info("=" * 60)
        logger.info(f"Phase:              {self['phase']} ({PHASE_CONFIGS[self['phase']]['description']})")
        logger.info(f"Phase Startbalance: ${self['phase_start_balance']:.2f}")
        logger.info(f"Handelstage Phase:  {self['phase_trading_days']}")
        logger.info(f"Heute Trades:       {self['today_trades']} | PnL: ${self['today_pnl_usd']:.2f}")
        logger.info(f"Daily Brake aktiv:  {self['daily_loss_brake_active']}")
        logger.info(f"Gesamt Trades:      {self['total_trades']} | Wins: {self['total_wins']} | Losses: {self['total_losses']}")
        logger.info(f"Master Payouts:     {self['master_payout_count']} | Netto: ${self['master_total_net_payout']:.2f}")
        logger.info("=" * 60)


# ======================================================
# INDIKATOR-BERECHNUNG & DATEN-STREAMING (1M & 15M)
# ======================================================

def prepare_1m_indicators(df1: pd.DataFrame) -> pd.DataFrame:
    """
    Berechnet die exakten Indikatoren wie im Backtester:
    ATR, Displacement (mit ATR-Shift und Docht-Pruefung) und 3-Kerzen-FVGs.
    """
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
    return df1


def get_live_bars(symbol: str, n_bars: int = 400) -> Optional[pd.DataFrame]:
    """
    Holt die letzten n_bars 1-Minuten-Kerzen aus MT5 und berechnet
    die identischen Indikatoren wie im Backtest.
    """
    try:
        import MetaTrader5 as mt5
        bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, n_bars)
        if bars is None or len(bars) == 0:
            logger.error(f"Keine 1M-Bars erhalten fuer {symbol}: {mt5.last_error()}")
            return None

        df = pd.DataFrame(bars)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        df = df.rename(columns={"tick_volume": "volume"})
        df = df[["open", "high", "low", "close", "volume"]]
        df.index = df.index.tz_convert(MARKET_TZ)
        return prepare_1m_indicators(df)

    except ImportError:
        logger.error("MT5 nicht verfuegbar fuer get_live_bars()")
        return None
    except Exception as e:
        logger.error(f"Fehler beim Laden der Live-1M-Bars: {e}")
        return None


def get_live_bars_15m(symbol: str, n_bars: int = 100) -> Optional[pd.DataFrame]:
    """
    Holt 15-Minuten-Kerzen aus MT5 fuer die EMA20 / EMA50 Trendbestimmung.
    """
    try:
        import MetaTrader5 as mt5
        bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, n_bars)
        if bars is None or len(bars) == 0:
            logger.error(f"Keine 15M-Bars erhalten fuer {symbol}: {mt5.last_error()}")
            return None

        df = pd.DataFrame(bars)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        df = df.rename(columns={"tick_volume": "volume"})
        df = df[["open", "high", "low", "close"]]
        df.index = df.index.tz_convert(MARKET_TZ)
        df["ema20"] = calculate_ema(df["close"], CONFIG["BIAS_EMA_FAST"])
        df["ema50"] = calculate_ema(df["close"], CONFIG["BIAS_EMA_SLOW"])
        return df

    except ImportError:
        logger.error("MT5 nicht verfuegbar fuer get_live_bars_15m()")
        return None
    except Exception as e:
        logger.error(f"Fehler beim Laden der Live-15M-Bars: {e}")
        return None


# ======================================================
# NEWS-FILTER (Placeholder - TODO: ForexFactory Scraper)
# ======================================================

# NewsFilter: Import aus news_filter.py (jetzt vollstaendig implementiert)
try:
    from news_filter import NewsFilter as _FFNewsFilter, manual_override

    class NewsFilter:
        """
        Wrapper um news_filter.NewsFilter (ForexFactory iCal).
        Kompatibel mit dem live_trader.py Interface (is_blocked / should_close_positions).
        """
        def __init__(self):
            self._filter = _FFNewsFilter(
                block_minutes_before=LPC["NEWS_BLOCK_MINUTES_BEFORE"],
                close_minutes_before=LPC["NEWS_CLOSE_MINUTES_BEFORE"]
            )

        def refresh(self):
            self._filter.refresh()
            # Manuelle Overrides aus global manual_override hinzufuegen
            extra = manual_override.get_today_events()
            existing = {dt.strftime("%H:%M") for dt in self._filter.get_todays_events()}
            for dt in extra:
                if dt.strftime("%H:%M") not in existing:
                    self._filter._news_times_ny.append(dt)
                    logger.info(f"[NEWS] Manueller Override hinzugefuegt: {dt.strftime('%H:%M')} NY")
            if extra:
                self._filter._news_times_ny.sort()

        def is_blocked(self, dt_ny: datetime, block_minutes_before: int = 15) -> bool:
            return self._filter.is_entry_blocked(dt_ny)

        def should_close_positions(self, dt_ny: datetime, close_minutes_before: int = 5) -> bool:
            return self._filter.should_close(dt_ny)

        def get_next_event_info(self, dt_ny: datetime) -> str:
            mins = self._filter.minutes_to_next_event(dt_ny)
            if mins is None:
                return "Kein weiteres Event heute"
            nxt = self._filter.next_event(dt_ny)
            return f"Naechstes Event: {nxt.strftime('%H:%M')} NY (in {mins:.0f} Min.)"

except ImportError:
    logger.warning("[NEWS] news_filter.py nicht gefunden! Fallback auf leeren Filter.")

    class NewsFilter:  # type: ignore
        """Minimaler Fallback wenn news_filter.py fehlt."""
        def refresh(self): pass
        def is_blocked(self, dt_ny, block_minutes_before=15): return False
        def should_close_positions(self, dt_ny, close_minutes_before=5): return False


# ======================================================
# PHASEN-LOGIK & ZUSTANDSUEBERGAENGE
# ======================================================

class PhaseManager:
    """Verwaltet Phasen-Uebergaenge und Payout-Tracking."""

    def __init__(self, state: BotState):
        self.state = state

    def get_phase_config(self) -> Dict[str, Any]:
        return PHASE_CONFIGS[self.state["phase"]]

    def check_and_advance_phase(self, current_balance: float, current_equity: float):
        """Prueft ob Phasen-Ziele erreicht sind und fuehrt Uebergaenge durch."""
        phase        = self.state["phase"]
        start_bal    = self.state["phase_start_balance"]
        profit       = current_balance - start_bal
        trading_days = self.state["phase_trading_days"]

        logger.info(
            f"[PHASE] Check: {phase} | Profit: ${profit:.2f} | "
            f"Handelstage: {trading_days} | Balance: ${current_balance:.2f}"
        )

        if phase == PHASE_EVAL_P1:
            if profit >= FP["EVAL_PHASE_1_TARGET"] and trading_days >= FP["EVAL_PHASE_1_MIN_DAYS"]:
                logger.info(f"[PHASE] EVALUATION PHASE 1 BESTANDEN! Profit: ${profit:.2f}")
                self._transition_to(PHASE_EVAL_P2, current_balance)

        elif phase == PHASE_EVAL_P2:
            if profit >= FP["EVAL_PHASE_2_TARGET"] and trading_days >= FP["EVAL_PHASE_2_MIN_DAYS"]:
                logger.info(f"[PHASE] EVALUATION PHASE 2 BESTANDEN! Profit: ${profit:.2f}")
                master_start = FP["ACCOUNT_SIZE"]
                self._transition_to(PHASE_MASTER_P1, master_start)
                logger.info(f"[PHASE] MASTER ACCOUNT AKTIV! Startbalance: ${master_start:.2f}")

        elif phase == PHASE_MASTER_P1:
            if self.state["master_payout_count"] >= FP["MASTER_P1_PAYOUTS_NEEDED"]:
                logger.info(
                    f"[PHASE] MASTER PHASE 1 ABGESCHLOSSEN! "
                    f"{self.state['master_payout_count']} Payouts erhalten. Wechsel zu MOONSHOT."
                )
                self._transition_to(PHASE_MASTER_P2, current_balance)

        # MASTER_P2: laueft weiter bis Account verloren

    def check_payout_opportunity(self, current_balance: float) -> bool:
        """Prueft ob Bi-Weekly Payout moeglich ist."""
        if self.state["phase"] not in (PHASE_MASTER_P1, PHASE_MASTER_P2):
            return False

        # Bi-Weekly Check
        last_payout = self.state.get("last_payout_date")
        if last_payout:
            last_dt    = date.fromisoformat(last_payout)
            days_since = (date.today() - last_dt).days
            if days_since < FP["PAYOUT_CYCLE_DAYS"]:
                return False
        else:
            master_start = date.fromisoformat(self.state["phase_start_date"])
            if (date.today() - master_start).days < FP["PAYOUT_CYCLE_DAYS"]:
                return False

        # Mindestgewinn pruefen
        profit_gross = current_balance - FP["ACCOUNT_SIZE"]
        if profit_gross < FP["MIN_PAYOUT_GROSS"]:
            return False

        net_payout = profit_gross * FP["PROFIT_SPLIT"]
        fee_refund = 0.0

        payout_num = self.state["master_payout_count"] + 1
        if payout_num == FP["FEE_REFUND_AT_PAYOUT"] and not self.state["fee_refund_received"]:
            fee_refund = FP["CHALLENGE_FEE_USD"]

        logger.info(
            f"[PAYOUT] PAYOUT-FENSTER OFFEN!\n"
            f"         Brutto:      ${profit_gross:.2f}\n"
            f"         Netto (80%): ${net_payout:.2f}\n"
            f"         Fee-Refund:  ${fee_refund:.2f}\n"
            f"         GESAMT:      ${net_payout + fee_refund:.2f}\n"
            f"         -> Jetzt bei FundingPips Payout beantragen!"
        )
        return True

    def record_payout(self, gross_amount: float):
        """Registriert einen erfolgten Payout (manuell aufrufen nach FundingPips-Bestaetigung)."""
        net        = gross_amount * FP["PROFIT_SPLIT"]
        fee_refund = 0.0
        payout_num = self.state["master_payout_count"] + 1

        if payout_num == FP["FEE_REFUND_AT_PAYOUT"] and not self.state["fee_refund_received"]:
            fee_refund = FP["CHALLENGE_FEE_USD"]
            self.state["fee_refund_received"] = True

        self.state["master_payout_count"]     = payout_num
        self.state["master_total_net_payout"] = self.state["master_total_net_payout"] + net + fee_refund
        self.state["last_payout_date"]        = date.today().isoformat()

        logger.info(
            f"[PAYOUT] #{payout_num} registriert | "
            f"Netto: ${net:.2f} | Fee-Refund: ${fee_refund:.2f} | "
            f"Gesamt bis heute: ${self.state['master_total_net_payout']:.2f}"
        )

    def _transition_to(self, new_phase: str, new_start_balance: float):
        logger.info(f"[PHASE] Uebergang: {self.state['phase']} -> {new_phase}")
        self.state["phase"]               = new_phase
        self.state["phase_start_balance"] = new_start_balance
        self.state["phase_start_date"]    = date.today().isoformat()
        self.state["phase_trading_days"]  = 0
        self.state["last_trading_day"]    = None


# ======================================================
# TAEGLISCHE SCHUTZCHECKS
# ======================================================

class RiskGuard:
    """Ueberwacht Daily Loss Limit und Max Drawdown in Echtzeit."""

    def __init__(self, state: BotState):
        self.state = state

    def daily_reset_if_needed(self, current_balance: float) -> bool:
        """Prueft ob neuer Tag begonnen hat und fuehrt taeglich Reset durch."""
        today = date.today().isoformat()
        if self.state["today_date"] != today:
            self.state.reset_daily(current_balance)
            return True
        return False

    def is_daily_loss_limit_hit(self, current_balance: float, current_equity: float) -> bool:
        """
        Prueft ob das Daily Loss Limit erreicht ist.
        FundingPips: 5% vom hoeherem Wert aus Tagesstart-Balance oder -Equity = $500.
        """
        start       = self.state["today_start_balance"]
        limit       = FP["DAILY_LOSS_LIMIT"]  # $500
        actual_loss = start - min(current_balance, current_equity)

        if actual_loss >= limit:
            if not self.state["daily_loss_brake_active"]:
                self.state["daily_loss_brake_active"] = True
                logger.warning(
                    f"[RISK] DAILY LOSS LIMIT ERREICHT! "
                    f"Verlust: ${actual_loss:.2f} >= Limit: ${limit:.2f} | "
                    f"Kein weiterer Trade heute!"
                )
            return True
        return False

    def is_max_drawdown_hit(self, current_equity: float) -> bool:
        """Prueft ob statisches Max-Drawdown-Limit ($1,000 / Boden $9,000) unterschritten ist."""
        floor = FP["ACCOUNT_SIZE"] - FP["STATIC_MAX_LOSS"]  # $9,000
        if current_equity <= floor:
            logger.critical(
                f"[RISK] MAX DRAWDOWN UNTERSCHRITTEN! "
                f"Equity: ${current_equity:.2f} <= Boden: ${floor:.2f} | Challenge verloren!"
            )
            return True
        return False

    def max_trades_reached(self, session: str) -> bool:
        """Prueft ob fuer die Session bereits ein Trade ausgefuehrt wurde."""
        if session == "AM" and self.state["today_am_traded"]:
            logger.debug("[RISK] AM-Session bereits getradet.")
            return True
        if session == "PM" and self.state["today_pm_traded"]:
            logger.debug("[RISK] PM-Session bereits getradet.")
            return True
        return False


# ======================================================
# SETUP-ERKENNUNG (1:1 mit Backtest strategy.py)
# ======================================================

def find_live_setup(df_1m: pd.DataFrame,
                    session: str,
                    rr_target: float,
                    bias: int = 0) -> Optional[Dict[str, Any]]:
    """
    Sucht nach einem ICT FVG-Retracement Setup in den aktuellen 1M-Bars
    unter exakter Verwendung von strategy.find_setup_1m().
    """
    deadline   = SESSIONS[session]["DEADLINE"]
    sess_start = SESSIONS[session]["START"]

    start_h, start_m = map(int, sess_start.split(":"))
    end_h, end_m     = map(int, deadline.split(":"))

    # Nur Session-Kerzen ab Session-Start bis zur Deadline
    session_df = df_1m[
        ((df_1m.index.hour == start_h) & (df_1m.index.minute >= start_m)) |
        (df_1m.index.hour > start_h)
    ]
    session_df = session_df[
        ((session_df.index.hour == end_h) & (session_df.index.minute <= end_m)) |
        (session_df.index.hour < end_h)
    ]

    if len(session_df) < 5:
        return None

    # Exakte Backtest-Logik aus strategy.py verwenden
    setup_obj, status = find_setup_1m(session_df, bias=bias, deadline_str=deadline)
    if setup_obj is None:
        return None

    # Dynamisches TP fuer Phase 1 / Phase 2 (Moonshot)
    direction = setup_obj.direction
    entry     = setup_obj.entry_price
    sl        = setup_obj.stop_loss
    risk      = setup_obj.risk_points

    if direction == 1:
        tp = entry + rr_target * risk
    else:
        tp = entry - rr_target * risk

    return {
        "direction":   direction,
        "entry":       entry,
        "sl":          sl,
        "tp":          tp,
        "risk_points": risk,
        "setup_time":  setup_obj.setup_time,
        "fvg_top":     setup_obj.fvg_top,
        "fvg_bot":     setup_obj.fvg_bottom,
        "status":      status,
    }


# ======================================================
# HAUPT-BOT
# ======================================================

class LiveTrader:
    """Haupt-Bot-Klasse. Steuert den kompletten Trading-Zyklus."""

    def __init__(self):
        self.state       = BotState()
        self.phase_mgr   = PhaseManager(self.state)
        self.risk_guard  = RiskGuard(self.state)
        self.news_filter = NewsFilter()
        self._running    = True
        self._symbol     = MT5_CONFIG["SYMBOL"]

        signal.signal(signal.SIGINT,  self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        logger.info(f"[BOT] Shutdown-Signal ({signum}). Beende sauber...")
        self._running = False

    def run(self):
        """Hauptloop des Bots."""
        logger.info("=" * 60)
        logger.info(" NASDAQ ICT CONTINUATION BOT - START")
        logger.info(f" Symbol:  {self._symbol}")
        logger.info(f" Phase:   {self.state['phase']}")
        logger.info("=" * 60)

        self.state.print_summary()

        if not connector.connect():
            logger.critical("[BOT] MT5 Verbindung fehlgeschlagen! Bot beendet.")
            return

        logger.info("[BOT] MT5 Verbunden. Bot laeuft...")

        try:
            self._main_loop()
        except Exception as e:
            logger.critical(f"[BOT] Kritischer Fehler: {e}", exc_info=True)
        finally:
            connector.disconnect()
            logger.info("[BOT] Bot beendet.")

    def _main_loop(self):
        """Tick-Loop (laeuft jede Minute)."""
        while self._running:
            try:
                now_ny  = datetime.now(ZoneInfo(MARKET_TZ))
                now_utc = datetime.now(timezone.utc)

                acct = connector.get_account_info()
                if acct is None:
                    logger.warning("[BOT] Account-Info nicht abrufbar, warte...")
                    time.sleep(30)
                    continue

                balance = acct["balance"]
                equity  = acct["equity"]

                # Taeglich Reset
                self.risk_guard.daily_reset_if_needed(balance)

                # News-Kalender refreshen
                self.news_filter.refresh()

                # Hard Session Close Check (20:30 UTC)
                self._check_hard_close(now_utc)

                # News Close Check
                if self.news_filter.should_close_positions(now_ny, LPC["NEWS_CLOSE_MINUTES_BEFORE"]):
                    closed = connector.close_all_positions(self._symbol, reason="news_close")
                    if closed:
                        logger.info(f"[NEWS] {closed} Positionen vor News geschlossen.")

                # Schutzchecks
                if self.risk_guard.is_max_drawdown_hit(equity):
                    logger.critical("[BOT] Max Drawdown hit - Bot stoppt! Account verloren.")
                    break

                if self.risk_guard.is_daily_loss_limit_hit(balance, equity):
                    time.sleep(60)
                    continue

                # Session-Check & Trading
                session = self._get_active_session(now_ny)
                if session and not self.risk_guard.max_trades_reached(session):
                    if not self.news_filter.is_blocked(now_ny, LPC["NEWS_BLOCK_MINUTES_BEFORE"]):
                        self._process_session(session, balance, equity)

                # End-of-Day Check um 15:30 NY
                if now_ny.hour == 15 and now_ny.minute == 30:
                    self._end_of_day_check(balance, equity)

                # Payout-Opportunity pruefen
                self.phase_mgr.check_payout_opportunity(balance)

                time.sleep(60)

            except Exception as e:
                logger.error(f"[BOT] Fehler im Loop: {e}", exc_info=True)
                time.sleep(30)

    def _get_active_session(self, now_ny: datetime) -> Optional[str]:
        """Gibt 'AM' oder 'PM' zurueck wenn Session aktiv ist, sonst None."""
        time_str = now_ny.strftime("%H:%M")
        for sess_name, sess_cfg in SESSIONS.items():
            if sess_cfg["START"] <= time_str <= sess_cfg["DEADLINE"]:
                return sess_name
        return None

    def _process_session(self, session: str, balance: float, equity: float):
        """Fuehrt die Setup-Suche und Order-Platzierung fuer eine Session durch."""
        phase_cfg = self.phase_mgr.get_phase_config()
        rr_target = phase_cfg["rr_target"]
        risk_usd  = phase_cfg["risk_usd"]

        logger.info(
            f"[{session}] Session aktiv | Phase: {self.state['phase']} | "
            f"Risiko: ${risk_usd:.0f} | CRV: 1:{rr_target}"
        )

        # 1. 15M Bars abrufen und uebergeordneten Momentum-Bias ermitteln
        df_15m = get_live_bars_15m(self._symbol, n_bars=100)
        session_start_str = SESSIONS[session]["START"]
        now_ny = datetime.now(ZoneInfo(MARKET_TZ))
        t_session = pd.Timestamp(f"{now_ny.strftime('%Y-%m-%d')} {session_start_str}", tz=MARKET_TZ)

        if df_15m is not None:
            bias, bias_desc = determine_bias_15m(df_15m, t_session)
        else:
            bias, bias_desc = 0, "fallback_keine_15m_daten"

        logger.info(f"[{session}] 15M-Bias vor Session-Start: {bias} ({bias_desc})")

        # 2. 1M Bars holen und exakt vorbereiten
        df_1m = get_live_bars(self._symbol, n_bars=300)
        if df_1m is None:
            logger.warning(f"[{session}] Keine Live-Bars verfuegbar")
            return

        # 3. Setup mit Backtest-Logik suchen
        setup = find_live_setup(df_1m, session, rr_target, bias=bias)
        if setup is None:
            logger.debug(f"[{session}] Kein Setup gefunden")
            return

        direction = setup["direction"]
        entry     = setup["entry"]
        sl        = setup["sl"]
        tp        = setup["tp"]
        risk_pts  = setup["risk_points"]
        dir_str   = "LONG" if direction == 1 else "SHORT"

        logger.info(
            f"[{session}] 🎯 Setup identifiziert: {dir_str} (Bias: {bias}) | "
            f"Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f} | "
            f"Risk: {risk_pts:.1f} Pkte"
        )

        # Lotsize berechnen
        lots, lot_debug = connector.calculate_lotsize(
            risk_usd=risk_usd,
            stop_points=risk_pts,
            add_spread_buffer=True
        )

        logger.info(
            f"[{session}] Lotsize: {lots:.2f} Lots | "
            f"Tatsaechliches Risiko: ${lot_debug.get('actual_risk_usd', 0):.2f}"
        )

        if lots < MT5_CONFIG["MIN_LOT"]:
            logger.warning(f"[{session}] Lotsize zu klein ({lots}) - kein Trade")
            return

        # Limit-Order an der FVG-Kante platzieren
        comment = f"{session}_{self.state['phase'][:6]}"
        result  = connector.place_limit_order(
            direction=direction,
            lots=lots,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            symbol=self._symbol,
            comment=comment,
            expiry_minutes=30,
        )

        if result:
            ticket = result.get("ticket", -1)
            logger.info(
                f"[{session}] ORDER PLATZIERT | Ticket: #{ticket} | "
                f"{dir_str} {lots:.2f} Lots @ {entry:.2f} | "
                f"SL: {sl:.2f} | TP: {tp:.2f}"
            )
            # Session als getradet markieren
            if session == "AM":
                self.state["today_am_traded"] = True
            else:
                self.state["today_pm_traded"] = True
            self.state["today_trades"] = self.state["today_trades"] + 1
        else:
            logger.error(f"[{session}] Order-Platzierung fehlgeschlagen")

    def _check_hard_close(self, now_utc: datetime):
        """Hard Session Close um 20:30 UTC (vor FundingPips Auto-Liquidation)."""
        close_h, close_m = map(int, LPC["HARD_SESSION_CLOSE_UTC"].split(":"))

        if now_utc.hour == close_h and now_utc.minute == close_m:
            positions = connector.get_open_positions(self._symbol)
            if positions:
                logger.warning(f"[CLOSE] HARD SESSION CLOSE! {len(positions)} Positionen schliessen...")
                connector.close_all_positions(self._symbol, reason="hard_session_close")
                connector.cancel_all_pending_orders(self._symbol)

    def _end_of_day_check(self, balance: float, equity: float):
        """Ende-des-Tages Checks (Handelstage zaehlen, Phasen pruefen)."""
        today = date.today().isoformat()
        if self.state.get("last_trading_day") == today:
            return  # Bereits heute gecheckt

        if self.state["today_trades"] > 0:
            self.state["phase_trading_days"] = self.state["phase_trading_days"] + 1
            self.state["last_trading_day"]   = today
            logger.info(
                f"[EOD] Handelstag #{self.state['phase_trading_days']} in Phase "
                f"{self.state['phase']} abgeschlossen."
            )

        self.phase_mgr.check_and_advance_phase(balance, equity)
        self.state.print_summary()


# ======================================================
# ENTRY POINT
# ======================================================

if __name__ == "__main__":
    bot = LiveTrader()
    bot.run()
