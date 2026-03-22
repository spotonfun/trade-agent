import schedule
import time
from agent_techniczny import analizuj
import json
from pathlib import Path

TICKERY = ["AAPL", "NVDA", "MSFT", "BTC-USD", "ETH-USD", "CDR.WA"]

def zapisz_sygnał(wynik: dict):
    """Zapisuje do pliku JSONL (każda linia = jeden sygnał)."""
    with open("sygnaly.jsonl", "a") as f:
        f.write(json.dumps(wynik, ensure_ascii=False) + "\n")

def uruchom_analizę():
    print("\n--- Cykl analizy ---")
    for ticker in TICKERY:
        try:
            wynik = analizuj(ticker)
            zapisz_sygnał(wynik)
            if wynik["signal"] != "HOLD" and wynik["confidence"] > 0.7:
                print(f"*** SYGNAŁ: {wynik['signal']} {ticker} (pewność {wynik['confidence']:.0%}) ***")
        except Exception as e:
            print(f"Błąd {ticker}: {e}")

# Uruchom od razu + co godzinę
uruchom_analizę()
schedule.every().hour.do(uruchom_analizę)

while True:
    schedule.run_pending()
    time.sleep(60)