"""
MT5 Connector Modul fuer den NASDAQ ICT Continuation Bot.

Stellt bereit:
- MT5-Verbindungsmanagement (connect / disconnect)
- Dynamische Lotsize-Berechnung mit Spread/Slippage-Guard
- Order-Platzierung (Market & Limit) mit Retry-Logik
- Account-Info Abfrage (Balance, Equity, Drawdown-Check)
- Hilfsfunktionen fuer Symbol-Info

WICHTIG: Dieses Modul laueft AUSSCHLIESSLICH auf Windows mit installiertem
         MetaTrader5 Python Package. Auf macOS nur zur Entwicklung/Review nutzbar.

Installation (Windows VPS):
    pip install MetaTrader5

FundingPips MT5 Server-Name: Pruefen unter MT5 -> Datei -> Demo-Konto eroeffnen
Typische Server-Namen: "FundingPips-Server", "FundingPips-Demo", "FundingPips-Live"
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

# MT5 nur importieren wenn vorhanden (Mac-Entwicklungsumgebung ueberspringen)
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("[WARNUNG] MetaTrader5 Package nicht gefunden.")
    print("          Auf Windows VPS installieren: pip install MetaTrader5")
    print("          Dieses Modul laeuft nur auf Windows mit MT5 Terminal.")

# Pfad-Setup damit config.py gefunden wird
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import MT5_CONFIG, LIVE_PROTECTION_CONFIG, FUNDINGPIPS_CONFIG

logger = logging.getLogger("MT5Connector")


# ==============================================================================
# VERBINDUNGSMANAGEMENT
# ==============================================================================

def connect(login: Optional[int] = None,
            password: Optional[str] = None,
            server: Optional[str] = None) -> bool:
    """
    Stellt Verbindung zu MetaTrader5 her.

    Zugangsdaten aus MT5_CONFIG oder als Parameter; Umgebungsvariablen
    haben Vorrang (MT5_LOGIN, MT5_PASSWORD, MT5_SERVER).

    Returns:
        True wenn Verbindung erfolgreich, False bei Fehler.
    """
    if not MT5_AVAILABLE:
        logger.error("MetaTrader5 nicht installiert. Bitte auf Windows VPS ausfuehren.")
        return False

    # Zugangsdaten: Env-Variablen > Parameter > config.py
    _login    = int(os.environ.get("MT5_LOGIN",    login    or MT5_CONFIG["LOGIN"]    or 0))
    _password = str(os.environ.get("MT5_PASSWORD", password or MT5_CONFIG["PASSWORD"] or ""))
    _server   = str(os.environ.get("MT5_SERVER",   server   or MT5_CONFIG["SERVER"]   or ""))

    if not _login or not _password or not _server:
        logger.error(
            "MT5 Zugangsdaten fehlen! Setze MT5_LOGIN, MT5_PASSWORD, MT5_SERVER "
            "als Umgebungsvariablen oder trage sie in config.py -> MT5_CONFIG ein."
        )
        return False

    # MT5 initialisieren (optional: Pfad zum Terminal)
    mt5_path = MT5_CONFIG.get("MT5_PATH") or None
    if mt5_path and os.path.exists(mt5_path):
        init_ok = mt5.initialize(path=mt5_path)
    else:
        init_ok = mt5.initialize()

    if not init_ok:
        err = mt5.last_error()
        logger.error(f"MT5 initialize() fehlgeschlagen: {err}")
        return False

    # Login
    if not mt5.login(_login, password=_password, server=_server):
        err = mt5.last_error()
        logger.error(f"MT5 login() fehlgeschlagen fuer Account {_login}: {err}")
        mt5.shutdown()
        return False

    info = mt5.account_info()
    logger.info(
        f"MT5 Verbunden | Account: {info.login} | Server: {info.server} | "
        f"Balance: {info.balance:.2f} | Equity: {info.equity:.2f}"
    )
    return True


def disconnect():
    """Trennt die MT5-Verbindung sauber."""
    if MT5_AVAILABLE:
        mt5.shutdown()
        logger.info("MT5 Verbindung getrennt.")


# ==============================================================================
# ACCOUNT & SYMBOL INFO
# ==============================================================================

def get_account_info() -> Optional[Dict[str, Any]]:
    """Gibt aktuelle Account-Daten als Dict zurueck."""
    if not MT5_AVAILABLE:
        return None
    info = mt5.account_info()
    if info is None:
        logger.error(f"account_info() fehlgeschlagen: {mt5.last_error()}")
        return None
    return {
        "login":        info.login,
        "balance":      info.balance,
        "equity":       info.equity,
        "profit":       info.profit,
        "margin":       info.margin,
        "free_margin":  info.margin_free,
        "currency":     info.currency,
        "leverage":     info.leverage,
        "server":       info.server,
    }


def get_symbol_info(symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Gibt Symbol-Informationen zurueck (tick_size, tick_value, volume_step etc.).
    Wichtig fuer die Lotsize-Berechnung.
    """
    if not MT5_AVAILABLE:
        return None
    sym = symbol or MT5_CONFIG["SYMBOL"]

    # Symbol sichtbar schalten (falls nicht in Marktwatch)
    if not mt5.symbol_select(sym, True):
        logger.error(f"Symbol '{sym}' konnte nicht ausgewaehlt werden: {mt5.last_error()}")
        return None

    info = mt5.symbol_info(sym)
    if info is None:
        logger.error(f"symbol_info('{sym}') fehlgeschlagen: {mt5.last_error()}")
        return None

    return {
        "symbol":           info.name,
        "digits":           info.digits,
        "point":            info.point,          # 1 Punkt in Preis-Einheiten
        "tick_size":        info.trade_tick_size, # Mindest-Preisaenderung
        "tick_value":       info.trade_tick_value, # Wert von 1 Tick in Konto-Waehrung pro Lot
        "contract_size":    info.trade_contract_size,
        "volume_min":       info.volume_min,
        "volume_max":       info.volume_max,
        "volume_step":      info.volume_step,
        "spread":           info.spread,         # Spread in Punkten
        "ask":              info.ask,
        "bid":              info.bid,
    }


def get_current_spread_points(symbol: Optional[str] = None) -> float:
    """Gibt den aktuellen Spread in Punkten (Index-Punkten fuer NAS100) zurueck."""
    if not MT5_AVAILABLE:
        return 1.5  # Fallback-Wert fuer Tests
    info = get_symbol_info(symbol)
    if info is None:
        return 1.5
    return float(info["spread"]) * info["point"] / info["tick_size"]


# ==============================================================================
# DYNAMISCHE LOTSIZE-BERECHNUNG (KERN-FUNKTION)
# ==============================================================================

def calculate_lotsize(risk_usd: float,
                      stop_points: float,
                      symbol: Optional[str] = None,
                      add_spread_buffer: bool = True) -> Tuple[float, Dict[str, Any]]:
    """
    Berechnet die korrekte Lotsize fuer ein gegebenes USD-Risiko und einen Stop-Abstand.

    Formel:
        tick_value_per_lot = tick_value (Wert von 1 Tick pro Lot in $)
        ticks_in_stop      = stop_points / tick_size
        value_per_lot      = ticks_in_stop * tick_value_per_lot
        raw_lots           = risk_usd / value_per_lot

    Dann: Runden auf lot_step, Clamp auf [min_lot, max_lot]

    Args:
        risk_usd:          Gewuenschtes Dollar-Risiko (z.B. 80.0 oder 100.0)
        stop_points:       Stop-Abstand in Index-Punkten (z.B. 10.0 Punkte)
        symbol:            MT5 Symbol-Name (Standard aus MT5_CONFIG["SYMBOL"])
        add_spread_buffer: True = Spread + Slippage-Puffer zum Stop addieren
                           (verhindert, dass tatsaechlicher Verlust > risk_usd)

    Returns:
        (lotsize, debug_info): Berechnete Lotsize + Diagnose-Dict
    """
    sym_info = get_symbol_info(symbol)

    if sym_info is None:
        # Fallback fuer offline Tests (NAS100 typische Werte)
        # 1 Lot NAS100 = $1 pro Punkt, 0.01 Lot = $0.01 pro Punkt
        logger.warning("Symbol-Info nicht verfuegbar, nutze NAS100-Fallback-Werte")
        sym_info = {
            "tick_size":    0.25,       # NAS100 bei vielen Brokern 0.25 Punkte
            "tick_value":   0.25,       # 0.25$ pro Tick pro Lot (bei $1/Punkt-Lot)
            "volume_min":   0.01,
            "volume_max":   5.0,
            "volume_step":  0.01,
            "spread":       1.5,
            "point":        0.01,
        }

    tick_size  = sym_info["tick_size"]
    tick_value = sym_info["tick_value"]
    vol_min    = sym_info["volume_min"]
    vol_max    = min(sym_info["volume_max"], MT5_CONFIG["MAX_LOT"])
    vol_step   = sym_info["volume_step"]
    spread_pts = sym_info.get("spread", 1.5)

    # Spread-Puffer hinzufuegen (tatsaechlicher Verlust = Stop + Spread + Slippage)
    effective_stop = stop_points
    if add_spread_buffer:
        # Puffer = Spread-Punkte + fest konfigurierter Slippage-Puffer
        buffer = spread_pts + LIVE_PROTECTION_CONFIG["MAX_SLIPPAGE_SPREAD_BUFFER"]
        effective_stop = stop_points + buffer
        logger.debug(f"Stop: {stop_points:.2f} Punkte + Buffer {buffer:.2f} = {effective_stop:.2f} Punkte")

    # Kernberechnung
    if tick_size <= 0 or tick_value <= 0:
        logger.error(f"Ungueltige Symbol-Info: tick_size={tick_size}, tick_value={tick_value}")
        return MT5_CONFIG["MIN_LOT"], {}

    ticks_in_stop      = effective_stop / tick_size
    dollar_per_lot     = ticks_in_stop * tick_value
    raw_lots           = risk_usd / dollar_per_lot if dollar_per_lot > 0 else 0.0

    # Auf lot_step runden (Floor = nie mehr riskieren als gewuenscht)
    if vol_step > 0:
        rounded_lots = max(vol_min, (raw_lots // vol_step) * vol_step)
    else:
        rounded_lots = max(vol_min, raw_lots)

    final_lots = min(rounded_lots, vol_max)
    actual_risk = final_lots * dollar_per_lot

    debug = {
        "risk_usd_target":      risk_usd,
        "stop_points_raw":      stop_points,
        "stop_points_effective": effective_stop,
        "tick_size":            tick_size,
        "tick_value":           tick_value,
        "ticks_in_stop":        ticks_in_stop,
        "dollar_per_lot":       dollar_per_lot,
        "raw_lots":             raw_lots,
        "final_lots":           final_lots,
        "actual_risk_usd":      actual_risk,
    }

    logger.debug(
        f"Lotsize: {final_lots:.2f} Lots | "
        f"Stop: {effective_stop:.1f} Pts | "
        f"Risiko: ${actual_risk:.2f} (Ziel: ${risk_usd:.2f})"
    )

    return final_lots, debug


# ==============================================================================
# ORDER-PLATZIERUNG
# ==============================================================================

def place_market_order(direction: int,
                       lots: float,
                       stop_loss: float,
                       take_profit: float,
                       symbol: Optional[str] = None,
                       comment: str = "") -> Optional[Dict[str, Any]]:
    """
    Platziert eine Market-Order (sofortige Ausfuehrung).

    Args:
        direction:   +1 fuer Long (BUY), -1 fuer Short (SELL)
        lots:        Lotgroesse (aus calculate_lotsize())
        stop_loss:   Stop-Loss Preis
        take_profit: Take-Profit Preis
        symbol:      MT5 Symbol (Standard aus Config)
        comment:     Order-Kommentar

    Returns:
        Dict mit Ergebnis-Informationen oder None bei Fehler.
    """
    if not MT5_AVAILABLE:
        logger.warning("MT5 nicht verfuegbar - Order simuliert (Testmodus)")
        return {"ticket": -1, "simulated": True, "lots": lots}

    sym  = symbol or MT5_CONFIG["SYMBOL"]
    info = get_symbol_info(sym)
    if info is None:
        return None

    order_type = mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL
    price      = info["ask"] if direction == 1 else info["bid"]

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       sym,
        "volume":       lots,
        "type":         order_type,
        "price":        price,
        "sl":           round(stop_loss,  info["digits"]),
        "tp":           round(take_profit, info["digits"]),
        "magic":        MT5_CONFIG["MAGIC_NUMBER"],
        "comment":      f"{MT5_CONFIG['COMMENT_PREFIX']}_{comment}"[:27],  # MT5 max 31 Zeichen
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    return _send_order_with_retry(request)


def place_limit_order(direction: int,
                      lots: float,
                      entry_price: float,
                      stop_loss: float,
                      take_profit: float,
                      symbol: Optional[str] = None,
                      comment: str = "",
                      expiry_minutes: int = 30) -> Optional[Dict[str, Any]]:
    """
    Platziert eine Pending Limit-Order (Retracement-Einstieg an der FVG).

    Args:
        direction:       +1 fuer BUY LIMIT (Long), -1 fuer SELL LIMIT (Short)
        entry_price:     Limit-Preis (FVG-Kante)
        expiry_minutes:  Automatischer Verfall der Order (Standard: 30 Min.)
    """
    if not MT5_AVAILABLE:
        logger.warning("MT5 nicht verfuegbar - Limit-Order simuliert (Testmodus)")
        return {"ticket": -1, "simulated": True, "lots": lots, "type": "limit"}

    sym  = symbol or MT5_CONFIG["SYMBOL"]
    info = get_symbol_info(sym)
    if info is None:
        return None

    order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == 1 else mt5.ORDER_TYPE_SELL_LIMIT

    # Order-Ablaufzeit (UTC)
    from datetime import timedelta
    expiry_time = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)

    request = {
        "action":       mt5.TRADE_ACTION_PENDING,
        "symbol":       sym,
        "volume":       lots,
        "type":         order_type,
        "price":        round(entry_price, info["digits"]),
        "sl":           round(stop_loss,   info["digits"]),
        "tp":           round(take_profit,  info["digits"]),
        "magic":        MT5_CONFIG["MAGIC_NUMBER"],
        "comment":      f"{MT5_CONFIG['COMMENT_PREFIX']}_{comment}"[:27],
        "type_time":    mt5.ORDER_TIME_SPECIFIED,
        "expiration":   int(expiry_time.timestamp()),
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    return _send_order_with_retry(request)


def close_all_positions(symbol: Optional[str] = None, reason: str = "auto_close") -> int:
    """
    Schliesst alle offenen Positionen fuer das angegebene Symbol (oder alle).

    Returns:
        Anzahl geschlossener Positionen.
    """
    if not MT5_AVAILABLE:
        logger.warning("MT5 nicht verfuegbar - kein Close moeglich.")
        return 0

    sym       = symbol or MT5_CONFIG["SYMBOL"]
    positions = mt5.positions_get(symbol=sym)
    if not positions:
        return 0

    closed = 0
    for pos in positions:
        if pos.magic != MT5_CONFIG["MAGIC_NUMBER"]:
            continue  # Nur eigene Bot-Positionen schliessen

        sym_info  = get_symbol_info(pos.symbol)
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        close_price = sym_info["bid"] if pos.type == mt5.ORDER_TYPE_BUY else sym_info["ask"]

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     pos.ticket,
            "price":        close_price,
            "magic":        MT5_CONFIG["MAGIC_NUMBER"],
            "comment":      f"CLOSE_{reason}"[:27],
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = _send_order_with_retry(request)
        if result:
            closed += 1
            logger.info(f"Position #{pos.ticket} geschlossen ({reason}): {pos.symbol} {pos.volume} Lots")

    return closed


def cancel_all_pending_orders(symbol: Optional[str] = None) -> int:
    """Loescht alle pending Limit-Orders des Bots."""
    if not MT5_AVAILABLE:
        return 0

    sym    = symbol or MT5_CONFIG["SYMBOL"]
    orders = mt5.orders_get(symbol=sym)
    if not orders:
        return 0

    cancelled = 0
    for order in orders:
        if order.magic != MT5_CONFIG["MAGIC_NUMBER"]:
            continue
        result = mt5.order_send({
            "action":  mt5.TRADE_ACTION_REMOVE,
            "order":   order.ticket,
            "comment": "BOT_CANCEL",
        })
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            cancelled += 1

    return cancelled


def get_open_positions(symbol: Optional[str] = None) -> list:
    """Gibt alle offenen Bot-Positionen fuer das Symbol zurueck."""
    if not MT5_AVAILABLE:
        return []
    sym = symbol or MT5_CONFIG["SYMBOL"]
    positions = mt5.positions_get(symbol=sym)
    if not positions:
        return []
    return [p for p in positions if p.magic == MT5_CONFIG["MAGIC_NUMBER"]]


# ==============================================================================
# INTERNE HILFSFUNKTIONEN
# ==============================================================================

def _send_order_with_retry(request: Dict) -> Optional[Dict[str, Any]]:
    """
    Sendet eine Order mit automatischer Retry-Logik bei temporaeren Fehlern.
    """
    retries = MT5_CONFIG["ORDER_RETRY_COUNT"]
    delay   = MT5_CONFIG["ORDER_RETRY_DELAY_SEC"]

    for attempt in range(retries):
        result = mt5.order_send(request)

        if result is None:
            err = mt5.last_error()
            logger.error(f"order_send() -> None | Versuch {attempt+1}/{retries} | Fehler: {err}")
            if attempt < retries - 1:
                time.sleep(delay)
            continue

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                f"Order ausgefuehrt | Ticket: {result.order} | "
                f"Preis: {result.price:.2f} | Lots: {result.volume:.2f}"
            )
            return {
                "ticket":     result.order,
                "price":      result.price,
                "volume":     result.volume,
                "retcode":    result.retcode,
                "comment":    result.comment,
                "simulated":  False,
            }

        # Preis-Requote: Preis aktualisieren und nochmal versuchen
        if result.retcode in (mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED):
            sym_info = get_symbol_info(request.get("symbol"))
            if sym_info:
                if request.get("type") in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT):
                    request["price"] = sym_info["ask"]
                else:
                    request["price"] = sym_info["bid"]
            logger.warning(f"Requote | Versuch {attempt+1}/{retries} | Neuer Preis: {request['price']:.2f}")
            time.sleep(0.5)
            continue

        # Kritischer Fehler - nicht wiederholen
        logger.error(
            f"Order fehlgeschlagen | Retcode: {result.retcode} | "
            f"Kommentar: {result.comment} | Versuch {attempt+1}/{retries}"
        )
        if result.retcode not in (
            mt5.TRADE_RETCODE_CONNECTION,
            mt5.TRADE_RETCODE_TIMEOUT,
        ):
            break  # Nicht-temporaerer Fehler -> sofort abbrechen

        if attempt < retries - 1:
            time.sleep(delay)

    logger.error("Order nach allen Versuchen fehlgeschlagen.")
    return None


# ==============================================================================
# QUICK-TEST (python mt5_connector.py direkt aufrufen)
# ==============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    print("=" * 60)
    print("MT5 Connector - Verbindungstest")
    print("=" * 60)

    if not MT5_AVAILABLE:
        print("\n[FEHLER] MetaTrader5 nicht installiert!")
        print("Auf Windows VPS ausfuehren: pip install MetaTrader5")
        print("\nFuer Entwicklung: Lotsize-Berechnung offline testen:")
        print("-" * 40)

        # Offline Lotsize-Test (kein MT5 noetig)
        lots_p1, info_p1 = calculate_lotsize(
            risk_usd=100.0, stop_points=10.0, add_spread_buffer=True
        )
        lots_p2, info_p2 = calculate_lotsize(
            risk_usd=80.0,  stop_points=10.0, add_spread_buffer=True
        )
        print(f"Phase 1 Lotsize (100$ Risiko, 10 Pkt Stop): {lots_p1:.2f} Lots")
        print(f"  -> Tatsaechliches Risiko: ${info_p1['actual_risk_usd']:.2f}")
        print(f"Phase 2 Lotsize (80$ Risiko, 10 Pkt Stop):  {lots_p2:.2f} Lots")
        print(f"  -> Tatsaechliches Risiko: ${info_p2['actual_risk_usd']:.2f}")
        sys.exit(0)

    # Verbindungstest (nur auf Windows mit MT5)
    if connect():
        info = get_account_info()
        print(f"\nAccount: {info}")
        sym = get_symbol_info()
        print(f"\nSymbol-Info {MT5_CONFIG['SYMBOL']}: {sym}")

        lots, dbg = calculate_lotsize(risk_usd=80.0, stop_points=10.0)
        print(f"\nLotsize-Test (80$ Risiko, 10 Pkt Stop): {lots:.2f} Lots")
        print(f"Debug: {dbg}")
        disconnect()
    else:
        print("Verbindung fehlgeschlagen - Zugangsdaten in config.py oder .env pruefen.")
