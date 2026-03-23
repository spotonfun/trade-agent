import feedparser
import requests
import json
import praw
import yfinance as yf
import ollama
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Literal

MODEL = "llama3.2"

# ──────────────────────────────────────────────
# 1. Struktury danych
# ──────────────────────────────────────────────

@dataclass
class Artykul:
    tytul: str
    tresc: str          # pierwsze ~500 znaków
    zrodlo: str
    url: str
    timestamp: datetime
    waga: float         # 1.0 = news finansowy, 0.6 = Reddit, 0.8 = SEC


@dataclass
class SygnalSentymentu:
    artykul: Artykul
    sentyment: Literal["bardzo_pozytywny", "pozytywny", "neutralny",
                        "negatywny", "bardzo_negatywny"]
    score: float        # -1.0 … +1.0
    istotnosc: float    # 0.0 … 1.0 (czy naprawdę dotyczy spółki)
    kategoria: str      # "wyniki", "przejęcie", "regulacje", "produkt", "ogólny"
    kluczowy_fakt: str  # jedno zdanie z artykułu


# ──────────────────────────────────────────────
# 2. Źródła danych
# ──────────────────────────────────────────────

# --- 2a. RSS / newsy przez yfinance ---

def pobierz_newsy_yfinance(ticker: str, max_artykulow: int = 15) -> list[Artykul]:
    """yfinance zwraca listę newsów powiązanych z tickerem."""
    spółka = yf.Ticker(ticker)
    wyniki = []

    for item in (spółka.news or [])[:max_artykulow]:
        content = item.get("content", {})
        tytul = content.get("title", "")
        tresc = content.get("summary", "") or tytul

        # timestamp – yfinance zwraca różne formaty
        ts_raw = content.get("pubDate") or content.get("displayTime")
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except Exception:
            ts = datetime.now(timezone.utc)

        wyniki.append(Artykul(
            tytul=tytul,
            tresc=tresc[:500],
            zrodlo="yfinance/news",
            url=content.get("canonicalUrl", {}).get("url", ""),
            timestamp=ts,
            waga=1.0,
        ))

    return wyniki


# --- 2b. Kanały RSS finansowe ---

RSS_FEEDS = {
    "Reuters Business": "https://feeds.reuters.com/reuters/businessNews",
    "MarketWatch":      "https://feeds.marketwatch.com/marketwatch/topstories/",
    "Seeking Alpha":    "https://seekingalpha.com/feed.xml",
    "Yahoo Finance":    "https://finance.yahoo.com/rss/topstories",
}

def pobierz_rss(ticker: str, feeds: dict = RSS_FEEDS,
                okno_godzin: int = 48) -> list[Artykul]:
    """Pobiera RSS i filtruje artykuły wspominające ticker lub nazwę spółki."""
    spółka = yf.Ticker(ticker)
    nazwa = (spółka.info.get("shortName") or ticker).lower()
    ticker_lc = ticker.lower().replace("-usd", "").replace(".wa", "")

    granica = datetime.now(timezone.utc) - timedelta(hours=okno_godzin)
    wyniki = []

    for nazwa_zrodla, url in feeds.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:30]:
                tekst = (entry.get("title", "") + " " +
                         entry.get("summary", "")).lower()

                if ticker_lc not in tekst and nazwa[:6] not in tekst:
                    continue  # artykuł nie dotyczy spółki

                # Parsuj datę
                ts_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                if ts_struct:
                    ts = datetime(*ts_struct[:6], tzinfo=timezone.utc)
                else:
                    ts = datetime.now(timezone.utc)

                if ts < granica:
                    continue

                wyniki.append(Artykul(
                    tytul=entry.get("title", ""),
                    tresc=(entry.get("summary", "") or "")[:500],
                    zrodlo=nazwa_zrodla,
                    url=entry.get("link", ""),
                    timestamp=ts,
                    waga=1.0,
                ))
        except Exception as e:
            print(f"  RSS {nazwa_zrodla}: {e}")

    return wyniki


# --- 2c. Reddit (wymaga darmowego konta aplikacji na reddit.com/prefs/apps) ---

def pobierz_reddit(ticker: str,
                   client_id: str = "",
                   client_secret: str = "",
                   max_postow: int = 20) -> list[Artykul]:
    """
    Pobiera posty z r/stocks, r/investing, r/wallstreetbets.
    Jeśli brak credentials – zwraca pustą listę (agent działa bez Reddit).
    """
    if not client_id or not client_secret:
        return []

    try:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent="investment-agent/1.0",
        )

        subreddity = ["stocks", "investing", "SecurityAnalysis"]
        wyniki = []
        ticker_lc = ticker.lower().replace("-usd", "").replace(".wa", "")

        for sub in subreddity:
            for post in reddit.subreddit(sub).search(
                ticker_lc, sort="new", time_filter="week", limit=max_postow
            ):
                tresc = (post.selftext or "")[:400] or post.title
                wyniki.append(Artykul(
                    tytul=post.title,
                    tresc=tresc,
                    zrodlo=f"Reddit/r/{sub}",
                    url=f"https://reddit.com{post.permalink}",
                    timestamp=datetime.fromtimestamp(
                        post.created_utc, tz=timezone.utc
                    ),
                    waga=0.6,   # Reddit ma niższe zaufanie niż media finansowe
                ))

        return wyniki

    except Exception as e:
        print(f"  Reddit error: {e}")
        return []


# --- 2d. SEC EDGAR – najważniejsze formularze (8-K, 10-Q, 10-K) ---

def pobierz_sec(ticker: str, max_zdarzen: int = 5) -> list[Artykul]:
    """Pobiera nagłówki ostatnich zgłoszeń do SEC przez EDGAR RSS."""
    try:
        spółka = yf.Ticker(ticker)
        cik = spółka.info.get("circulatingSupply")  # yfinance nie daje CIK bezpośrednio

        # Alternatywnie: wyszukaj CIK przez EDGAR API
        r = requests.get(
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
            f"&dateRange=custom&startdt={(datetime.now()-timedelta(days=30)).strftime('%Y-%m-%d')}"
            f"&forms=8-K,10-Q",
            headers={"User-Agent": "investment-agent contact@example.com"},
            timeout=10,
        )
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])[:max_zdarzen]

        wyniki = []
        for hit in hits:
            src = hit.get("_source", {})
            wyniki.append(Artykul(
                tytul=src.get("file_date", "") + " " + src.get("form_type", "") + ": " + src.get("display_names", ""),
                tresc=src.get("entity_name", "") + " złożył " + src.get("form_type", ""),
                zrodlo="SEC EDGAR",
                url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type="
                    + src.get("form_type", "") + "&dateb=&owner=include&count=10",
                timestamp=datetime.now(timezone.utc),
                waga=1.2,   # komunikaty regulacyjne mają wyższą wagę
            ))

        return wyniki

    except Exception as e:
        print(f"  SEC EDGAR: {e}")
        return []


# ──────────────────────────────────────────────
# 3. Analiza sentymentu per artykuł (LLM)
# ──────────────────────────────────────────────

PROMPT_PER_ARTYKUL = """Przeanalizuj poniższy artykuł finansowy dotyczący spółki {ticker}.

TYTUŁ: {tytul}
TREŚĆ: {tresc}
ŹRÓDŁO: {zrodlo}

Odpowiedz WYŁĄCZNIE w JSON (bez markdown):
{{
  "sentyment": "bardzo_pozytywny" | "pozytywny" | "neutralny" | "negatywny" | "bardzo_negatywny",
  "score": -1.0 do 1.0,
  "istotnosc": 0.0 do 1.0,
  "kategoria": "wyniki_finansowe" | "przejecie_fuzja" | "produkt_innowacja" | "regulacje_prawo" | "makro" | "insider" | "ogolny",
  "kluczowy_fakt": "jedno zdanie opisujące najważniejszy fakt"
}}"""

def analizuj_artykul(artykul: Artykul, ticker: str) -> SygnalSentymentu | None:
    """Krótki, szybki prompt – jeden artykuł na raz."""
    try:
        prompt = PROMPT_PER_ARTYKUL.format(
            ticker=ticker,
            tytul=artykul.tytul,
            tresc=artykul.tresc,
            zrodlo=artykul.zrodlo,
        )

        odp = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0},
        )

        tekst = odp["message"]["content"].strip()
        if "```" in tekst:
            tekst = tekst.split("```")[1].lstrip("json")

        dane = json.loads(tekst)

        return SygnalSentymentu(
            artykul=artykul,
            sentyment=dane["sentyment"],
            score=float(dane["score"]),
            istotnosc=float(dane["istotnosc"]),
            kategoria=dane.get("kategoria", "ogolny"),
            kluczowy_fakt=dane.get("kluczowy_fakt", ""),
        )

    except Exception as e:
        print(f"    Błąd analizy artykułu: {e}")
        return None


# ──────────────────────────────────────────────
# 4. Agregacja ważona sygnałów
# ──────────────────────────────────────────────

def agreguj_sygnaly(sygnaly: list[SygnalSentymentu],
                    okno_godzin: int = 48) -> dict:
    """
    Liczy ważony score sentymentu.
    Waga = waga_źródła × istotność × aktualność (decay eksponencjalny).
    """
    if not sygnaly:
        return {"score_sredni": 0.0, "liczba": 0, "rozklad": {}}

    teraz = datetime.now(timezone.utc)
    suma_wag = 0.0
    suma_score = 0.0
    rozklad: dict[str, int] = {}

    for s in sygnaly:
        # Aktualność: pełna waga dla <6h, liniowy zanik do 0 po okno_godzin
        wiek_h = (teraz - s.artykul.timestamp).total_seconds() / 3600
        aktualnosc = max(0.0, 1.0 - wiek_h / okno_godzin)

        waga = s.artykul.waga * s.istotnosc * aktualnosc
        suma_wag += waga
        suma_score += s.score * waga
        rozklad[s.sentyment] = rozklad.get(s.sentyment, 0) + 1

    score_sredni = suma_score / suma_wag if suma_wag > 0 else 0.0

    return {
        "score_sredni": round(score_sredni, 3),
        "liczba_sygnalow": len(sygnaly),
        "suma_wag": round(suma_wag, 2),
        "rozklad": rozklad,
        "sygnaly_wysokiej_istotnosci": [
            {
                "tytul": s.artykul.tytul,
                "zrodlo": s.artykul.zrodlo,
                "score": s.score,
                "kategoria": s.kategoria,
                "fakt": s.kluczowy_fakt,
            }
            for s in sorted(sygnaly, key=lambda x: x.istotnosc, reverse=True)[:5]
        ],
    }


# ──────────────────────────────────────────────
# 5. Synteza końcowa (drugi wywołanie LLM)
# ──────────────────────────────────────────────

def synteza_końcowa(ticker: str, agregacja: dict) -> dict:
    """LLM tworzy narrację rynkową na podstawie zebranych sygnałów."""

    sygnaly_txt = "\n".join([
        f"- [{s['zrodlo']}] score {s['score']:+.2f} | {s['kategoria']} | {s['fakt']}"
        for s in agregacja["sygnaly_wysokiej_istotnosci"]
    ])

    rozklad_txt = ", ".join(
        [f"{k}: {v}" for k, v in agregacja["rozklad"].items()]
    )

    prompt = f"""Jesteś analitykiem rynkowym. Na podstawie zebranych sygnałów sentymentu dla {ticker}:

SCORE WAŻONY: {agregacja['score_sredni']:+.3f}  (zakres -1.0 do +1.0)
LICZBA SYGNAŁÓW: {agregacja['liczba_sygnalow']}
ROZKŁAD: {rozklad_txt}

TOP 5 ISTOTNYCH SYGNAŁÓW:
{sygnaly_txt}

Wydaj końcowy raport WYŁĄCZNIE w JSON (bez markdown):
{{
  "signal": "BULLISH" | "SLIGHTLY_BULLISH" | "NEUTRAL" | "SLIGHTLY_BEARISH" | "BEARISH",
  "score": {agregacja['score_sredni']},
  "confidence": 0.0-1.0,
  "narracja_rynkowa": "2-3 zdania: jaka historia opowiada rynek o tej spółce teraz",
  "catalyst_events": ["zdarzenie które może przyspieszyć ruch", "..."],
  "ryzyka_sentymentalne": ["potencjalny negatywny zwrot narracji", "..."],
  "dominujaca_kategoria": "kategoria zdarzenia z największą wagą",
  "horyzont_sygnalu": "1-3 dni" | "1-2 tygodnie" | "1 miesiąc"
}}"""

    odp = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0.1},
    )

    tekst = odp["message"]["content"].strip()
    if "```" in tekst:
        tekst = tekst.split("```")[1].lstrip("json")

    return json.loads(tekst)


# ──────────────────────────────────────────────
# 6. Główna funkcja agenta
# ──────────────────────────────────────────────

def analizuj(
    ticker: str,
    reddit_client_id: str = "",
    reddit_client_secret: str = "",
) -> dict:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Analiza sentymentu: {ticker}")

    # Zbierz artykuły ze wszystkich źródeł
    artykuly: list[Artykul] = []
    artykuly += pobierz_newsy_yfinance(ticker)
    artykuly += pobierz_rss(ticker)
    artykuly += pobierz_sec(ticker)
    artykuly += pobierz_reddit(ticker, reddit_client_id, reddit_client_secret)

    # Deduplikacja po tytule (pierwsze 60 znaków)
    seen: set[str] = set()
    unikalne = []
    for a in artykuly:
        klucz = a.tytul[:60].lower()
        if klucz not in seen:
            seen.add(klucz)
            unikalne.append(a)

    print(f"  Zebrano {len(unikalne)} unikalnych artykułów")

    # Analizuj każdy artykuł (równolegle dla szybkości)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        sygnaly_raw = list(ex.map(
            lambda a: analizuj_artykul(a, ticker), unikalne
        ))

    sygnaly = [s for s in sygnaly_raw if s is not None and s.istotnosc > 0.2]
    print(f"  Istotnych sygnałów: {len(sygnaly)}")

    if not sygnaly:
        return {
            "ticker": ticker,
            "signal": "NEUTRAL",
            "score": 0.0,
            "confidence": 0.1,
            "narracja_rynkowa": "Brak istotnych sygnałów sentymentu.",
            "catalyst_events": [],
            "ryzyka_sentymentalne": [],
            "timestamp": datetime.now().isoformat(),
        }

    # Agreguj i syntetyzuj
    agregacja = agreguj_sygnaly(sygnaly)
    raport = synteza_końcowej(ticker, agregacja)

    raport["ticker"] = ticker
    raport["timestamp"] = datetime.now().isoformat()
    raport["meta"] = {
        "artykuly_zebrane": len(unikalne),
        "sygnaly_istotne": len(sygnaly),
        "score_sredni": agregacja["score_sredni"],
    }

    return raport


# ──────────────────────────────────────────────
# 7. Uruchomienie
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import os

    REDDIT_ID     = os.getenv("REDDIT_CLIENT_ID", "")
    REDDIT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")

    for ticker in ["AAPL", "NVDA"]:
        try:
            w = analizuj(ticker, REDDIT_ID, REDDIT_SECRET)
            print(f"\n{'='*55}")
            print(f"  {w['ticker']} | {w['signal']} | score: {w['score']:+.3f}")
            print(f"  {w['narracja_rynkowa']}")
            print(f"  Catalyst: {', '.join(w.get('catalyst_events', []))}")
        except Exception as e:
            print(f"Błąd {ticker}: {e}")
# ```

# ---

# ## Konfiguracja Reddit API (5 minut)

# Reddit wymaga własnych credentials, ale są całkowicie darmowe:

# 1. Zaloguj się na reddit.com i wejdź na `reddit.com/prefs/apps`
# 2. Kliknij "create another app" → wybierz "script"
# 3. Nazwa: `investment-agent`, redirect URI: `http://localhost:8080`
# 4. Skopiuj `client_id` (pod nazwą) i `client_secret`

# Ustaw w `.env`:
# ```
# REDDIT_CLIENT_ID=twoj_id
# REDDIT_CLIENT_SECRET=twoj_secret