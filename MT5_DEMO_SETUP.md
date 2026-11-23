# MT5-Demobot

`live_trader.py` ist ein Demo-only Adapter für die bestehende 1-Minuten-Strategie. Er benötigt Windows, ein installiertes und geöffnetes MetaTrader-5-Terminal sowie ein dort angemeldetes Demo-Konto. Das Python-Paket kann auf diesem Mac nicht verwendet werden.

## Windows-Start

1. Projektordner auf eine Windows-VM oder einen Windows-VPS kopieren.
2. Python installieren und im Projektordner PowerShell öffnen.
3. Abhängigkeiten in einer virtuellen Umgebung installieren:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install MetaTrader5 pandas numpy matplotlib
   ```

4. MT5 starten, mit dem **Demo-Konto** verbinden und den genauen US100-Namen im Market Watch prüfen (`US100`, `NAS100`, `USTEC` o. Ä.). Algo-Trading im Terminal aktivieren.
5. Demobot starten, zum Beispiel:

   ```powershell
   python live_trader.py --demo --symbol US100 --risk-usd 80 --max-spread 3.0
   ```

   `--max-spread` vergleicht den Ask-Bid-Abstand direkt in Preis-/Indexpunkten (nicht in MT5-Ticks). Der Standardwert ist 3.0. Symbolnamen und Quotierung unterscheiden sich zwischen Servern; vor dem ersten Lauf im Demo-Konto prüfen.

6. Mit `Ctrl+C` beenden. Keine MT5-Passwörter in Kommandozeilen, Dateien oder Quellcode speichern; die Anmeldung erfolgt im MT5-Terminal.

## Schutzverhalten

- `--demo` ist Pflicht; zusätzlich prüft das Programm den Kontotyp direkt über MT5. Live-Konten werden abgelehnt.
- Eine Limitorder wird nur abgelegt, wenn der Preis nach dem erkannten Retracement noch auf der gültigen Seite des Einstiegskurses liegt. Andernfalls wird das verspätete Signal verworfen.
- Die Positionsgröße wird über `order_calc_profit` und die Lot-Minimum-/Schrittwerte des konkreten Symbols abgerundet. Lässt sich das gewünschte Risiko nicht einhalten, wird kein Trade eröffnet.
- Der Bot prüft den aktuellen Spread, maximal zwei Signale pro NY-Handelstag, ein lokales Tagesverlust-Limit und den statischen Gesamtverlust-Floor. Beim Tages-/Gesamtverlust-Limit werden offene Bot-Orders gelöscht und Bot-Positionen geschlossen.
- Nicht gefüllte Limitorders laufen am Ende ihrer Session ab. Offene Bot-Positionen werden um 16:00 Uhr New-York-Zeit geschlossen; das ist vor dem veröffentlichten FundingPips-Auto-Close-Fenster und entspricht dem Tagesende des vorhandenen Backtests.
- Der lokale Zustand wird je MT5-Konto und Symbol unter `results/mt5_demo_state_*.json` gespeichert. Bei beschädigtem State startet der Bot sicherheitshalber nicht.

## Grenzen und nächster Schritt

- Das bestehende `find_setup_1m` liefert ein Signal erst, nachdem das FVG-Retracement bereits in einer abgeschlossenen Minute erkannt wurde. Der Adapter kann deshalb später als die Backtest-Limitannahme sein und verwirft gegebenenfalls Setups. Vor Echtgeldbetrieb muss die Strategie in einen kausalen, zustandsbehafteten Scanner umgebaut und die Signal-/Order-Zeitstempel-Abweichung geprüft werden.
- Der Tagesverlust-Baseline-Wert wird beim ersten Bot-Start des NY-Tages lokal gespeichert. Er ist nicht die offizielle FundingPips-Servermessung und ersetzt sie nicht.
- Der **USD-News-Filter bleibt als nächster eigener Schritt vorgemerkt**. Für FundingPips soll er später den offiziellen Kalender im Dashboard berücksichtigen; bis dahin nicht als regelkonforme Live-Lösung betrachten.
- Dieser Adapter wurde hier nicht mit MT5 verbunden: Die aktuelle Entwicklungsumgebung ist macOS und hat weder Windows-MT5-Terminal noch das MetaTrader5-Python-Paket. Zuerst ausschließlich auf einem Demo-Konto erproben.
