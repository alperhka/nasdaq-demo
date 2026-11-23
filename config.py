"""
Globale Konfiguration fuer den Nasdaq ICT-Continuation Bot.
Optimiert fuer Prop-Firm-Evaluierungen (10k - 50k Konten) auf 1-Minuten-Basis.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "USATECH_1min.csv")
RESULTS_DIR = os.path.join(BASE_DIR, "results")
CHARTS_DIR = os.path.join(BASE_DIR, "charts")

# Zeitzonen
DATA_TZ = "UTC"
MARKET_TZ = "America/New_York"

# Session-Zeiten (New York Zeit)
# Multi-Session Architektur: AM Open (09:30 - 11:30) & PM Silver Bullet (13:30 - 15:15)
SESSIONS = {
    "AM": {"START": "09:30", "DEADLINE": "11:30", "NAME": "NY AM Session"},
    "PM": {"START": "13:30", "DEADLINE": "15:15", "NAME": "NY PM Silver Bullet"}
}
SESSION_START = "09:30"   # Glocke an der Wall Street
ENTRY_DEADLINE = "11:30"  # Vormittags-Einstiegsfenster
SESSION_END = "16:00"    # Spaetester Ausstieg fuer Tagespositionen
MAX_TRADES_PER_DAY = 2    # Max. 1 Trade pro Session (verhindert Overtrading & schuetzt FundingPips Daily DD)

# Strategie-Parameter
CONFIG = {
    # Zeiteinheiten
    "SETUP_TIMEFRAME": "1min",     # 1-Minuten-Einstiege fuer maximale Praezision & Frequenz
    "BIAS_TIMEFRAME": "15min",     # 15-Minuten-Trend fuer uebergeordneten Momentum-Filter
    
    # 15M Bias Filter
    "BIAS_EMA_FAST": 20,
    "BIAS_EMA_SLOW": 50,
    "USE_BIAS_FILTER": True,       # True = nur mit Trend traden
    
    # Displacement (1-Minuten-Kerzen)
    "DISP_ATR_LEN": 14,
    "DISP_ATR_MULT": 1.2,          # Kerzenkoerper >= 1.2 * ATR
    "DISP_MAX_WICK_RATIO": 0.40,   # Docht in Trendrichtung max. 40% des Koerpers
    
    # Fair Value Gap (1-Minuten-FVG)
    "FVG_MIN_POINTS": 2.0,         # Mindestgroesse der FVG auf 1M in Indexpunkten
    
    # Einstieg & Ausstieg
    "RR_TARGET": 2.0,              # Fester Take-Profit: 1:2.0 CRV
    "MIN_STOP_POINTS": 5.0,        # Mindestabstand zum Stop (Spread-Schutz)
    "MAX_STOP_POINTS": 25.0,       # Maximaler Stop auf 1M (schuetzt vor ueberdehnten Kerzen)
    "STOP_BUFFER_POINTS": 1.0,     # Puffer hinter der FVG
    
    # Kosten & Friktionen (Reale CFD-/Futures-Bedingungen)
    "SPREAD_POINTS": 1.5,          # Broker-Spread
    "SLIPPAGE_POINTS": 0.5,        # Slippage bei Stop-Loss-Ausfuehrung
    
    # Prop-Firm-Einstellungen (z. B. 50k Account)
    "PROP_ACCOUNT_SIZE": 50000.0,  # Kontogroesse (z. B. 50.000 $)
    "PROP_TARGET_USD": 3000.0,     # Gewinnziel (z. B. +3.000 $ = 6 %)
    "PROP_MAX_TRAILING_DD": 2000.0,# Max. Trailing Drawdown (-2.000 $ = 4 %)
    "PROP_RISK_PER_TRADE": 350.0,  # Risiko pro Trade (0.7 % = ca. 350 $)
}

# FundingPips 2-Step Standard ($10,000 Account) & Bi-Weekly Payout Engine
FUNDINGPIPS_CONFIG = {
    # Account & Fee-Regeln
    "ACCOUNT_SIZE": 10000.0,           # 10.000 $ Account
    "CHALLENGE_FEE_USD": 80.0,         # 80 $ Challenge-Gebuehr
    "FEE_REFUND_AT_PAYOUT": 4,         # Beim 4. Payout auf demselben Master wird Fee erstattet (+80 $)
    "STATIC_MAX_LOSS": 1000.0,         # 10 % statischer Max Loss (fester Boden bei $9.000)
    "DAILY_LOSS_LIMIT": 500.0,         # 5 % Tagesverlust-Grenze (Start-Balance/Equity)

    # 1. Evaluation Phase 1 & 2
    "EVAL_PHASE_1_TARGET": 800.0,      # Phase 1: +800 $ (8 %)
    "EVAL_PHASE_1_MIN_DAYS": 3,        # Min. 3 Handelstage
    "EVAL_PHASE_2_TARGET": 500.0,      # Phase 2: +500 $ (5 %)
    "EVAL_PHASE_2_MIN_DAYS": 3,        # Min. 3 Handelstage
    "EVAL_RISK_PER_TRADE": 100.0,      # 100 $ Risiko in Evaluation
    "EVAL_RR_TARGET": 2.0,             # 1:2.0 CRV (+200 $ Win, -$103 Loss)

    # 2. Master Account Payout-Regeln
    "PROFIT_SPLIT": 0.80,              # 80 % Trader Netto-Cashflow, 20 % FundingPips
    "PAYOUT_CYCLE_DAYS": 14,           # Bi-Weekly (alle 14 Kalendertage moeglich)
    "MIN_PAYOUT_GROSS": 100.0,         # Mindestens 100 $ brutto (1 %) fuer Payout
    
    # Master Phase 1 (Kosten absichern)
    "MASTER_P1_RISK": 100.0,           # 100 $ Risiko
    "MASTER_P1_RR": 1.25,              # 1:1.25 CRV -> +125 $ brutto (nach 80% Split = exakt 100 $ netto)
    "MASTER_P1_PAYOUTS_NEEDED": 2,     # Nach 2 erfolgreichen Payouts -> Switch auf Phase 2

    # Master Phase 2 (Asymmetric Moonshot Runner)
    "MASTER_P2_RISK": 80.0,            # 80 $ fixes Risiko (0.8 %)
    "MASTER_P2_RR": 6.25,              # 1:6.25 CRV -> +500 $ brutto (nach 80% Split = 400 $ netto)
}

# Technische Schutzfilter fuer Live-MT5-Bot
LIVE_PROTECTION_CONFIG = {
    "NEWS_BLOCK_MINUTES_BEFORE": 15,   # Neue Entries 15 Min. vor roten USD-News sperren
    "NEWS_CLOSE_MINUTES_BEFORE": 5,    # Offene Trades 5 Min. vor roten USD-News schliessen
    "HARD_SESSION_CLOSE_UTC": "20:30", # Harter Close um 20:30 UTC (vor FundingPips Auto-Liquidation)
    "MAX_SLIPPAGE_SPREAD_BUFFER": 10.0 # Lotsize-Puffer damit Verlust bei $80-Stop max. $85-$90 betraegt
}

# Abwaertskompatibilitaet fuer Altaufrufe
PAYOUT_HARVEST_CONFIG = FUNDINGPIPS_CONFIG

# MT5 Live-Trading Konfiguration (fuer Windows VPS mit MetaTrader5)
MT5_CONFIG = {
    # Broker-Zugangsdaten (NIEMALS in Versionskontrolle einchecken!)
    # Stattdessen als Umgebungsvariablen setzen oder in .env Datei speichern
    "LOGIN":    None,         # int:  MT5 Account-Nummer (z.B. 12345678)
    "PASSWORD": None,         # str:  MT5 Passwort
    "SERVER":   None,         # str:  FundingPips MT5 Server (z.B. "FundingPips-Server")

    # Symbol-Konfiguration (NAS100 / US100 / USTEC - je nach Broker)
    "SYMBOL":           "USTEC",    # Exakter MT5 Symbol-Name (auf FundingPips verifizieren!)
    "MAGIC_NUMBER":     202402,     # Eindeutige Magic Number fuer diesen Bot (verhindert Konflikte)
    "COMMENT_PREFIX":   "ICT_BOT",  # Order-Kommentar-Praefix fuer MT5

    # Risiko-Parameter Live-Trading
    "RISK_USD_PHASE1":  100.0,      # $ Risiko pro Trade in Master Phase 1
    "RISK_USD_PHASE2":  80.0,       # $ Risiko pro Trade in Master Phase 2 (Moonshot)
    "RISK_USD_EVAL":    100.0,      # $ Risiko pro Trade in Evaluation Phase 1 & 2

    # Lotsize-Berechnung
    "MIN_LOT":          0.01,       # Minimale Lotgroesse
    "MAX_LOT":          5.0,        # Maximale Lotgroesse (Sicherheitsbegrenzung)
    "LOT_STEP":         0.01,       # Lot-Schrittweite (Standard bei den meisten Brokern)

    # Verbindungs-Timeouts
    "CONNECTION_TIMEOUT_SEC":   30,     # Max. Wartezeit fuer MT5-Verbindung
    "ORDER_RETRY_COUNT":        3,      # Anzahl Wiederholungsversuche bei Order-Fehler
    "ORDER_RETRY_DELAY_SEC":    2.0,    # Wartezeit zwischen Wiederholungsversuchen

    # Pfad zum MT5 Terminal (nur auf Windows relevant)
    # Leer lassen wenn MT5 schon laeuft; ansonsten Pfad zur terminal64.exe angeben
    "MT5_PATH":         r"C:\Program Files\MetaTrader 5\terminal64.exe",

    # Finnhub API Key fuer Economic Calendar (kostenlos auf https://finnhub.io)
    # Leer lassen = funktioniert ohne Key (limitierter Zugang)
    "FINNHUB_API_KEY":  "",
}

# NYSE Boersenfeiertage (kein Handel)
HOLIDAYS = {
    "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20",
    "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
    "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29",
    "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
}
