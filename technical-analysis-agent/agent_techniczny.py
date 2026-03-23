import yfinance as yf
import pandas as pd
import pandas_ta as ta
import ollama
import json
from datetime import datetime

MODEL = "llama3.2"  # lub qwen2.5:7b

def pobierz_dane(ticker: str, okres: str = "3mo", interwał: str = "1d") -> pd.DataFrame:
    """Pobiera OHLCV i liczy wskaźniki."""
    df = yf.download(ticker, period=okres, interval=interwał, auto_adjust=True)
    df.columns = [c.lower() for c in df.columns]

    # Wskaźniki – pandas-ta liczy wszystko in-place
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.bbands(length=20, std=2, append=True)
    df.ta.ema(length=20, append=True)
    df.ta.ema(length=50, append=True)

    return df.dropna()

def buduj_prompt(ticker: str, df: pd.DataFrame) -> str:
    """Zamienia ostatnie N świec + wskaźniki na prompt tekstowy."""
    ostatni = df.iloc[-1]
    poprzedni = df.iloc[-2]

    # Kierunek wolumenu
    vol_trend = "rosnący" if ostatni["volume"] > df["volume"].rolling(10).mean().iloc[-1] else "malejący"

    # Wykryj crossover MACD
    macd_cross = ""
    if poprzedni["MACDh_12_26_9"] < 0 and ostatni["MACDh_12_26_9"] > 0:
        macd_cross = "UWAGA: właśnie nastąpił bullish MACD crossover!"
    elif poprzedni["MACDh_12_26_9"] > 0 and ostatni["MACDh_12_26_9"] < 0:
        macd_cross = "UWAGA: właśnie nastąpił bearish MACD crossover!"

    prompt = f"""Jesteś ekspertem analizy technicznej. Przeanalizuj poniższe dane i wydaj rekomendację.

TICKER: {ticker}
DATA: {df.index[-1].date()}

=== CENA ===
Zamknięcie:  {ostatni['close']:.2f}
Otwarcie:    {ostatni['open']:.2f}
Wysokie:     {ostatni['high']:.2f}
Niskie:      {ostatni['low']:.2f}
Zmiana:      {((ostatni['close']/poprzedni['close'])-1)*100:.2f}%

=== WSKAŹNIKI TECHNICZNE ===
RSI (14):          {ostatni['RSI_14']:.1f}  {'[WYKUPIONY >70]' if ostatni['RSI_14'] > 70 else '[WYPRZEDANY <30]' if ostatni['RSI_14'] < 30 else '[neutralny]'}
MACD:              {ostatni['MACD_12_26_9']:.4f}
MACD sygnał:       {ostatni['MACDs_12_26_9']:.4f}
MACD histogram:    {ostatni['MACDh_12_26_9']:.4f}
{macd_cross}

Bollinger górna:   {ostatni['BBU_20_2.0']:.2f}
Bollinger środek:  {ostatni['BBM_20_2.0']:.2f}
Bollinger dolna:   {ostatni['BBL_20_2.0']:.2f}
% w paśmie BB:     {ostatni['BBP_20_2.0']:.2f}

EMA 20:            {ostatni['EMA_20']:.2f}
EMA 50:            {ostatni['EMA_50']:.2f}
Trend EMA:         {'EMA20 > EMA50 (uptrend)' if ostatni['EMA_20'] > ostatni['EMA_50'] else 'EMA20 < EMA50 (downtrend)'}

Wolumen (vs avg):  {vol_trend}

=== OSTATNIE 5 ŚWIEC (close) ===
{', '.join([f"{df.index[i].date()}: {df['close'].iloc[i]:.2f}" for i in range(-5, 0)])}

Odpowiedz WYŁĄCZNIE w formacie JSON (bez markdown, bez komentarzy):
{{
  "signal": "BUY" | "SELL" | "HOLD",
  "confidence": 0.0-1.0,
  "stop_loss": <cena stop-loss>,
  "take_profit": <cena take-profit>,
  "timeframe": "krótkoterminowy" | "średnioterminowy" | "długoterminowy",
  "kluczowe_sygnaly": ["sygnał 1", "sygnał 2", "sygnał 3"],
  "ryzyka": ["ryzyko 1", "ryzyko 2"],
  "reasoning": "krótkie uzasadnienie max 3 zdania"
}}"""

    return prompt

def analizuj(ticker: str) -> dict:
    """Główna funkcja agenta."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Analizuję {ticker}...")

    df = pobierz_dane(ticker)
    prompt = buduj_prompt(ticker, df)

    # Wywołanie lokalnego LLM przez Ollama
    odpowiedź = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0.1}  # Niska temp = deterministyczne decyzje
    )

    tekst = odpowiedź["message"]["content"].strip()

    # Wyczyść gdyby LLM dodał markdown
    if tekst.startswith("```"):
        tekst = tekst.split("```")[1]
        if tekst.startswith("json"):
            tekst = tekst[4:]

    wynik = json.loads(tekst)
    wynik["ticker"] = ticker
    wynik["timestamp"] = datetime.now().isoformat()
    wynik["cena"] = float(df["close"].iloc[-1])

    return wynik

# Przykład użycia
if __name__ == "__main__":
    tickery = ["AAPL", "NVDA", "BTC-USD"]

    for t in tickery:
        try:
            wynik = analizuj(t)
            print(f"\n{'='*50}")
            print(f"  {wynik['ticker']} | {wynik['signal']} | pewność: {wynik['confidence']:.0%}")
            print(f"  Stop-loss: {wynik['stop_loss']} | Take-profit: {wynik['take_profit']}")
            print(f"  {wynik['reasoning']}")
            print(f"  Sygnały: {', '.join(wynik['kluczowe_sygnaly'])}")
        except Exception as e:
            print(f"Błąd dla {t}: {e}")