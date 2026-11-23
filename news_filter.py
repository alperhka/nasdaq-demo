"""
ForexFactory News-Filter fuer den NASDAQ ICT Continuation Bot.
=============================================================
Holt automatisch den tagesaktuellen USD High-Impact Kalender von
ForexFactory (iCal-Feed) und stellt Sperrfunktionen bereit.

Verwendung in live_trader.py:
    from news_filter import NewsFilter
    news = NewsFilter()
    news.refresh()                  # einmal pro Tag aufrufen
    news.is_entry_blocked(dt_ny)    # True = kein neuer Entry
    news.should_close(dt_ny)        # True = Positionen schliessen

iCal-Feed: https://www.forexfactory.com/calendar.ics?week=thisweek
  - Kein API-Key noetig
  - Offizielle Quelle, stabil
  - Enthaelt alle Events der aktuellen Woche mit Impact-Level

Cache:  cache/news_YYYY-MM-DD.json  (1x pro Tag gecached, kein Re-Fetch)
Fallback: Manuelle Hardcode-Liste wichtiger Termine (wenn kein Netz)
"""

import os
import sys
import json
import logging
import re
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict
from zoneinfo import ZoneInfo

logger = logging.getLogger("NewsFilter")

# Pfad-Setup
BASE_DIR = Path(__file__).parent.resolve()
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# New York Timezone
NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = timezone.utc

# ==============================================================================
# BEKANNTE HIGH-IMPACT USD EVENTS (Fallback wenn ForexFactory nicht erreichbar)
# Diese Zeiten gelten fuer den typischen Veroeffentlichungszeitpunkt (NY Zeit)
# ==============================================================================

# Wiederkehrende News-Muster (Wochentag -> typische Event-Zeiten in NY)
# 0=Montag, 1=Dienstag, ..., 4=Freitag
RECURRING_PATTERNS: Dict[int, List[str]] = {
    # Freitag: NFP (erster Freitag im Monat) - immer 08:30
    # Hier nur generische Fallbacks - echte Daten kommen von ForexFactory
}

# Feste "immer gefaehrliche" Uhrzeiten (Fallback wenn FF nicht erreichbar)
# Diese Zeiten sind hochfrequente USD-Risiko-Fenster
FALLBACK_HIGH_RISK_WINDOWS_NY = [
    "08:30",   # NFP, CPI, PPI, Retail Sales, Jobless Claims
    "10:00",   # ISM, JOLTS, Consumer Confidence
    "14:00",   # FOMC Statement, Treasury Auctions
    "14:30",   # Fed Chair Pressekonferenz (nach FOMC)
]

# Keywords die auf High-Impact USD Events hinweisen (im iCal SUMMARY Feld)
HIGH_IMPACT_KEYWORDS = [
    "Non-Farm", "NFP", "Nonfarm",
    "CPI", "Consumer Price",
    "PPI", "Producer Price",
    "FOMC", "Federal Reserve", "Fed Chair", "Powell",
    "GDP", "Gross Domestic",
    "Retail Sales",
    "ISM", "PMI Manufacturing", "PMI Services",
    "JOLTS", "Job Opening",
    "Unemployment", "Jobless Claims",
    "Interest Rate", "Rate Decision",
    "PCE", "Personal Consumption",
    "Durable Goods",
    "Trade Balance",
    "Housing Starts", "Building Permits",
    "Consumer Confidence", "Consumer Sentiment",
    "ADP Employment",
]


# ==============================================================================
# ICAL PARSER (ohne externe Abhaengigkeiten)
# ==============================================================================

def _parse_ical_datetime(dt_str: str) -> Optional[datetime]:
    """
    Parst ein iCal DTSTART Datumsfeld in ein UTC-datetime Objekt.
    Formate: 20240115T133000Z  |  20240115T083000  |  20240115
    """
    dt_str = dt_str.strip()

    # Format: 20240115T133000Z (UTC)
    if dt_str.endswith("Z") and "T" in dt_str:
        try:
            return datetime.strptime(dt_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC_TZ)
        except ValueError:
            pass

    # Format: 20240115T133000 (assume UTC fuer FF)
    if "T" in dt_str and len(dt_str) >= 15:
        try:
            dt = datetime.strptime(dt_str[:15], "%Y%m%dT%H%M%S")
            return dt.replace(tzinfo=UTC_TZ)
        except ValueError:
            pass

    # Format: 20240115 (Ganztages-Event)
    if len(dt_str) == 8:
        try:
            d = datetime.strptime(dt_str, "%Y%m%d")
            return d.replace(hour=0, minute=0, tzinfo=UTC_TZ)
        except ValueError:
            pass

    return None


def _is_high_impact_usd(summary: str, description: str = "") -> bool:
    """
    Prueft ob ein iCal-Event ein High-Impact USD Event ist.
    ForexFactory markiert Impact im SUMMARY oder Description.
    """
    text = (summary + " " + description).upper()

    # ForexFactory iCal: "USD" muss im Event stehen
    if "USD" not in text and "US " not in text and "UNITED STATES" not in text:
        # Zusaetzlich: bekannte reine US-Events ohne explizites "USD"
        is_known_us = any(kw.upper() in text for kw in HIGH_IMPACT_KEYWORDS[:5])
        if not is_known_us:
            return False

    # Impact-Level Check (ForexFactory iCal kodiert Impact manchmal in Description)
    # "High" oder Warnsymbol (kann variieren je nach FF-Format)
    if "LOW" in text and "HIGH" not in text:
        return False

    # Keyword-Match
    return any(kw.lower() in text.lower() for kw in HIGH_IMPACT_KEYWORDS)


def parse_forexfactory_ical(ical_text: str, target_date: date) -> List[datetime]:
    """
    Parst den ForexFactory iCal-Feed und gibt alle High-Impact USD Events
    fuer target_date als NY-Zeit Datetimes zurueck.

    Args:
        ical_text:   Rohtext des iCal-Feeds
        target_date: Das Datum fuer das Events gesucht werden (z.B. date.today())

    Returns:
        Sortierte Liste von datetime-Objekten (NY-Zeit) der Events
    """
    events = []
    current_event: Dict[str, str] = {}
    in_event = False

    for line in ical_text.splitlines():
        line = line.strip()

        if line == "BEGIN:VEVENT":
            in_event = True
            current_event = {}

        elif line == "END:VEVENT" and in_event:
            in_event = False

            summary     = current_event.get("SUMMARY", "")
            description = current_event.get("DESCRIPTION", "")
            dtstart_raw = current_event.get("DTSTART", "")

            if not dtstart_raw or not summary:
                continue

            # Datetime parsen
            dt_utc = _parse_ical_datetime(dtstart_raw)
            if dt_utc is None:
                continue

            # Auf Zieldatum pruefen (in NY-Zeit)
            dt_ny = dt_utc.astimezone(NY_TZ)
            if dt_ny.date() != target_date:
                continue

            # High Impact USD Filter
            if not _is_high_impact_usd(summary, description):
                continue

            events.append(dt_ny)
            logger.info(
                f"[NEWS] Event gefunden: {dt_ny.strftime('%H:%M')} NY | {summary}"
            )

        elif in_event:
            # Multi-line iCal Felder (Folding) zusammenfuehren
            if line.startswith(" ") and current_event:
                last_key = list(current_event.keys())[-1] if current_event else None
                if last_key:
                    current_event[last_key] += line.strip()
            else:
                # Key:Value parsen (Sonderfall: DTSTART;TZID=... -> nur Key behalten)
                if ":" in line:
                    key, _, value = line.partition(":")
                    # Parametrisierte Keys (z.B. DTSTART;TZID=America/New_York) -> Key kuerzen
                    key = key.split(";")[0]
                    current_event[key] = value

    events.sort()
    return events


# ==============================================================================
# HAUPTKLASSE
# ==============================================================================

class NewsFilter:
    """
    Automatischer ForexFactory News-Filter.

    Wichtige USD High-Impact Events werden morgens gecacht und dann
    sekundengenau fuer Entry-Block und Position-Close verwendet.

    Typischer Workflow:
        news = NewsFilter()
        news.refresh()                           # 1x pro Tag (morgens)
        blocked = news.is_entry_blocked(now_ny)  # jede Minute pruefen
        close   = news.should_close(now_ny)      # jede Minute pruefen
    """

    def __init__(self,
                 block_minutes_before: int = 15,
                 close_minutes_before: int = 5):
        self.block_minutes = block_minutes_before
        self.close_minutes = close_minutes_before

        self._news_times_ny: List[datetime] = []
        self._last_fetch_date: Optional[date] = None
        self._fetch_failed: bool = False

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------

    def refresh(self, force: bool = False) -> bool:
        """
        Aktualisiert den News-Kalender fuer heute.
        Wird nur einmal pro Tag ausgefuehrt (gecached).

        Args:
            force: True = Cache ignorieren und neu fetchen

        Returns:
            True wenn Kalender erfolgreich geladen wurde.
        """
        today = date.today()

        # Bereits gecacht fuer heute?
        if not force and self._last_fetch_date == today:
            return not self._fetch_failed

        # 1. Cache-Datei pruefen
        cached = self._load_cache(today)
        if cached is not None and not force:
            self._news_times_ny = cached
            self._last_fetch_date = today
            self._fetch_failed = False
            self._log_todays_events()
            return True

        # 2. ForexFactory iCal fetchen
        logger.info("[NEWS] Lade ForexFactory Kalender...")
        events = self._fetch_from_forexfactory(today)

        if events is not None:
            self._news_times_ny = events
            self._fetch_failed = False
            self._save_cache(today, events)
            logger.info(f"[NEWS] {len(events)} High-Impact USD Events heute geladen.")
        else:
            # 3. Fallback: Manuell oder aus letztem Cache
            logger.warning(
                "[NEWS] ForexFactory nicht erreichbar! Nutze Fallback-Risikozeiten. "
                "Bitte manuell pruefen: https://www.forexfactory.com/calendar"
            )
            self._news_times_ny = self._get_fallback_times(today)
            self._fetch_failed = True

        self._last_fetch_date = today
        self._log_todays_events()
        return not self._fetch_failed

    def is_entry_blocked(self, dt_ny: datetime) -> bool:
        """
        True wenn neuer Entry gesperrt ist (innerhalb der Sperrzone vor News).

        Args:
            dt_ny: Aktuelle Zeit in NY-Timezone
        """
        for news_time in self._news_times_ny:
            diff_min = (news_time - dt_ny).total_seconds() / 60
            if 0 <= diff_min <= self.block_minutes:
                logger.warning(
                    f"[NEWS] ENTRY GEBLOCKT: {diff_min:.0f} Min. vor "
                    f"{news_time.strftime('%H:%M')} Event"
                )
                return True
        return False

    def should_close(self, dt_ny: datetime) -> bool:
        """
        True wenn offene Positionen sofort geschlossen werden sollen
        (innerhalb der Close-Zone kurz vor News).

        Args:
            dt_ny: Aktuelle Zeit in NY-Timezone
        """
        for news_time in self._news_times_ny:
            diff_min = (news_time - dt_ny).total_seconds() / 60
            if 0 <= diff_min <= self.close_minutes:
                logger.warning(
                    f"[NEWS] POSITIONEN SCHLIESSEN: {diff_min:.1f} Min. vor "
                    f"{news_time.strftime('%H:%M')} Event"
                )
                return True
        return False

    def get_todays_events(self) -> List[datetime]:
        """Gibt alle heutigen High-Impact Events zurueck (NY-Zeit)."""
        return list(self._news_times_ny)

    def next_event(self, dt_ny: datetime) -> Optional[datetime]:
        """Gibt das naechste bevorstehende Event zurueck (oder None)."""
        future = [t for t in self._news_times_ny if t > dt_ny]
        return min(future) if future else None

    def minutes_to_next_event(self, dt_ny: datetime) -> Optional[float]:
        """Gibt Minuten bis zum naechsten Event zurueck (oder None)."""
        nxt = self.next_event(dt_ny)
        if nxt is None:
            return None
        return (nxt - dt_ny).total_seconds() / 60

    # ------------------------------------------------------------------
    # FETCH & PARSE
    # ------------------------------------------------------------------

    def _fetch_from_forexfactory(self, target_date: date) -> Optional[List[datetime]]:
        """
        Holt News-Events aus mehreren Quellen (Fallback-Kette):
        1. ForexFactory iCal (direkt)
        2. ForexFactory iCal (via corsproxy - umgeht 403)
        3. TradingEconomics / Finnhub freier Kalender-Endpoint
        """
        # Quelle 1: ForexFactory direkt
        events = self._fetch_forexfactory_direct(target_date)
        if events is not None:
            logger.info("[NEWS] Quelle: ForexFactory direkt")
            return events

        # Quelle 2: ForexFactory via CORS-Proxy (umgeht 403 auf einigen VPS)
        events = self._fetch_forexfactory_via_proxy(target_date)
        if events is not None:
            logger.info("[NEWS] Quelle: ForexFactory via Proxy")
            return events

        # Quelle 3: Finnhub Economic Calendar (kostenloser API-Key noetig)
        events = self._fetch_finnhub(target_date)
        if events is not None:
            logger.info("[NEWS] Quelle: Finnhub Calendar")
            return events

        logger.warning("[NEWS] Alle News-Quellen fehlgeschlagen -> Fallback aktiv")
        return None

    def _fetch_forexfactory_direct(self, target_date: date) -> Optional[List[datetime]]:
        """ForexFactory iCal direkt fetchen."""
        try:
            import requests
            # Versuche sowohl thisweek als auch nextweek (falls Wochenwechsel)
            for week_param in ["thisweek", "nextweek"]:
                url = f"https://www.forexfactory.com/calendar.ics?week={week_param}"
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept":          "text/calendar,text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection":      "keep-alive",
                    "Referer":         "https://www.forexfactory.com/",
                    "Cache-Control":   "no-cache",
                }
                resp = requests.get(url, headers=headers, timeout=15)
                if resp.status_code == 200 and "BEGIN:VCALENDAR" in resp.text:
                    events = parse_forexfactory_ical(resp.text, target_date)
                    # Nur zurueckgeben wenn tatsaechlich Events fuer target_date gefunden
                    if events is not None:  # Leere Liste = kein News-Tag, auch OK
                        return events
                else:
                    logger.debug(f"[NEWS] FF direkt HTTP {resp.status_code} ({week_param})")
        except Exception as e:
            logger.debug(f"[NEWS] FF direkt Fehler: {e}")
        return None

    def _fetch_forexfactory_via_proxy(self, target_date: date) -> Optional[List[datetime]]:
        """
        ForexFactory iCal via oeffentlichem CORS-Proxy fetchen.
        Funktioniert oft wenn direkte Verbindung mit 403 geblockt wird.
        """
        try:
            import requests
            # Offentlicher Proxy - nur als Fallback, nicht auf sensible Daten anwenden
            proxies_to_try = [
                "https://corsproxy.io/?https://www.forexfactory.com/calendar.ics?week=thisweek",
                "https://api.allorigins.win/raw?url=https://www.forexfactory.com/calendar.ics?week=thisweek",
            ]
            for proxy_url in proxies_to_try:
                try:
                    resp = requests.get(proxy_url, timeout=10)
                    if resp.status_code == 200 and "BEGIN:VCALENDAR" in resp.text:
                        return parse_forexfactory_ical(resp.text, target_date)
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"[NEWS] Proxy Fetch Fehler: {e}")
        return None

    def _fetch_finnhub(self, target_date: date) -> Optional[List[datetime]]:
        """
        Finnhub Economic Calendar API (kostenlos, kein Key noetig fuer basic).
        Doku: https://finnhub.io/docs/api/economic-calendar

        Fuer besseren Zugang: kostenlosen API-Key auf finnhub.io holen und
        in config.py -> MT5_CONFIG["FINNHUB_API_KEY"] eintragen.
        """
        try:
            import requests
            from config import MT5_CONFIG

            api_key = MT5_CONFIG.get("FINNHUB_API_KEY", "")
            date_str = target_date.isoformat()

            url = "https://finnhub.io/api/v1/calendar/economic"
            params = {"from": date_str, "to": date_str}
            if api_key:
                params["token"] = api_key

            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                logger.debug(f"[NEWS] Finnhub HTTP {resp.status_code}")
                return None

            data = resp.json()
            events_raw = data.get("economicCalendar", [])

            result = []
            for ev in events_raw:
                # Filter: USD + High Impact
                if ev.get("country", "").upper() != "US":
                    continue
                impact = ev.get("impact", "").lower()
                if impact not in ("high", "3"):
                    continue

                time_str = ev.get("time", "")  # Format: "08:30:00" oder leer
                if not time_str:
                    continue

                try:
                    h, m = int(time_str[:2]), int(time_str[3:5])
                    dt = datetime(
                        target_date.year, target_date.month, target_date.day,
                        h, m, 0, tzinfo=NY_TZ
                    )
                    result.append(dt)
                    logger.info(f"[NEWS] Finnhub Event: {dt.strftime('%H:%M')} NY | {ev.get('event', '')}")
                except (ValueError, IndexError):
                    continue

            result.sort()
            return result

        except Exception as e:
            logger.debug(f"[NEWS] Finnhub Fehler: {e}")
        return None

    # ------------------------------------------------------------------
    # CACHE
    # ------------------------------------------------------------------

    def _cache_path(self, d: date) -> Path:
        return CACHE_DIR / f"news_{d.isoformat()}.json"

    def _save_cache(self, d: date, events: List[datetime]):
        try:
            data = [dt.isoformat() for dt in events]
            with open(self._cache_path(d), "w") as f:
                json.dump({"date": d.isoformat(), "events": data}, f, indent=2)
        except Exception as e:
            logger.warning(f"[NEWS] Cache-Speichern fehlgeschlagen: {e}")

    def _load_cache(self, d: date) -> Optional[List[datetime]]:
        path = self._cache_path(d)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("date") != d.isoformat():
                return None
            events = []
            for s in data.get("events", []):
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=NY_TZ)
                events.append(dt)
            logger.info(f"[NEWS] Cache geladen: {len(events)} Events fuer {d}")
            return events
        except Exception as e:
            logger.warning(f"[NEWS] Cache-Laden fehlgeschlagen: {e}")
            return None

    # ------------------------------------------------------------------
    # FALLBACK
    # ------------------------------------------------------------------

    def _get_fallback_times(self, target_date: date) -> List[datetime]:
        """
        Fallback wenn ForexFactory nicht erreichbar:
        Gibt generische Risikozeiten als Sperrzonen zurueck.

        ACHTUNG: Diese sind NICHT tagesspezifisch! Immer manuell pruefen!
        https://www.forexfactory.com/calendar
        """
        result = []
        weekday = target_date.weekday()  # 0=Mo, 4=Fr

        # Erster Freitag im Monat = NFP (08:30)
        if weekday == 4:
            # Pruefen ob erster Freitag
            if target_date.day <= 7:
                h, m = 8, 30
                dt = datetime(target_date.year, target_date.month, target_date.day,
                              h, m, 0, tzinfo=NY_TZ)
                result.append(dt)
                logger.warning(f"[NEWS] Fallback: Potenzieller NFP-Tag! 08:30 NY geblockt.")

        # Fallback: Generische 08:30 und 10:00 Sperrzonen (konservativ)
        for time_str in ["08:30", "10:00"]:
            h, m = map(int, time_str.split(":"))
            dt = datetime(target_date.year, target_date.month, target_date.day,
                          h, m, 0, tzinfo=NY_TZ)
            if dt not in result:
                result.append(dt)

        result.sort()
        logger.warning(
            f"[NEWS] FALLBACK AKTIV! {len(result)} generische Sperrzeiten gesetzt. "
            f"Manuell pruefen: https://www.forexfactory.com/calendar"
        )
        return result

    # ------------------------------------------------------------------
    # HILFSFUNKTIONEN
    # ------------------------------------------------------------------

    def _log_todays_events(self):
        if not self._news_times_ny:
            logger.info("[NEWS] Heute keine High-Impact USD Events (oder Wochenende).")
            return

        logger.info(f"[NEWS] Heutige Sperrzeiten ({len(self._news_times_ny)} Events):")
        for dt in self._news_times_ny:
            block_start = dt - timedelta(minutes=self.block_minutes)
            close_start = dt - timedelta(minutes=self.close_minutes)
            logger.info(
                f"       {dt.strftime('%H:%M')} NY | "
                f"Entry-Block ab {block_start.strftime('%H:%M')} | "
                f"Close ab {close_start.strftime('%H:%M')}"
            )


# ==============================================================================
# MANUELLE EVENT-VERWALTUNG (optional, fuer bekannte Termine vorab)
# ==============================================================================

class ManualNewsOverride:
    """
    Ermoeglicht das manuelle Hinzufuegen von bekannten News-Terminen.
    Nuetzlich fuer FOMC-Termine die Wochen im Voraus bekannt sind.

    Verwendung:
        override = ManualNewsOverride()
        override.add("2024-12-18", "14:00", "FOMC Rate Decision")
        override.add("2024-12-18", "14:30", "Fed Chair Powell Pressekonferenz")

        # In NewsFilter einbinden:
        news.add_manual_events(override.get_today_events())
    """

    def __init__(self):
        self._events: List[Dict] = []

    def add(self, date_str: str, time_ny: str, description: str = ""):
        """
        Fuegt einen manuellen Event hinzu.

        Args:
            date_str:    Datum im Format "YYYY-MM-DD"
            time_ny:     Uhrzeit New York im Format "HH:MM"
            description: Optionale Beschreibung
        """
        d = date.fromisoformat(date_str)
        h, m = map(int, time_ny.split(":"))
        dt = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=NY_TZ)
        self._events.append({"dt": dt, "desc": description})
        logger.info(f"[NEWS] Manueller Event: {date_str} {time_ny} NY - {description}")

    def get_today_events(self) -> List[datetime]:
        today = date.today()
        return [e["dt"] for e in self._events if e["dt"].date() == today]

    def get_all_events(self) -> List[Dict]:
        return list(self._events)


# Globale Override-Instanz (optional befuellen)
manual_override = ManualNewsOverride()

# Hier bekannte FOMC/wichtige Termine vorab eintragen:
# manual_override.add("2024-12-18", "14:00", "FOMC Rate Decision")
# manual_override.add("2024-12-18", "14:30", "Fed Powell Pressekonferenz")


# ==============================================================================
# QUICK-TEST (python news_filter.py direkt aufrufen)
# ==============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )

    print("=" * 60)
    print("NEWS FILTER - Test")
    print("=" * 60)

    news = NewsFilter(block_minutes_before=15, close_minutes_before=5)

    print("\n1. Lade ForexFactory Kalender...")
    success = news.refresh()

    if success:
        print(f"\n   Erfolgreich geladen!")
    else:
        print(f"\n   WARNUNG: Fallback-Zeiten aktiv (kein Netz oder ForexFactory down)")

    events = news.get_todays_events()
    print(f"\n2. Heutige High-Impact USD Events: {len(events)}")
    for e in events:
        print(f"   {e.strftime('%H:%M')} NY")

    print("\n3. Sperrzeit-Simulation:")
    now_ny = datetime.now(NY_TZ)
    print(f"   Aktuelle Zeit: {now_ny.strftime('%H:%M')} NY")
    print(f"   Entry geblockt: {news.is_entry_blocked(now_ny)}")
    print(f"   Positionen schliessen: {news.should_close(now_ny)}")

    nxt = news.next_event(now_ny)
    if nxt:
        mins = news.minutes_to_next_event(now_ny)
        print(f"   Naechster Event: {nxt.strftime('%H:%M')} NY (in {mins:.0f} Min.)")
    else:
        print("   Naechster Event: Keiner mehr heute")

    print("\n4. Manueller Override Test:")
    override = ManualNewsOverride()
    override.add(date.today().isoformat(), "14:00", "Test FOMC")
    today_manual = override.get_today_events()
    print(f"   {len(today_manual)} manuelle Events fuer heute")

    print("\nTest abgeschlossen.")
