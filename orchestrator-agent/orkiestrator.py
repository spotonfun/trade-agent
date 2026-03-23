# ═══════════════════════════════════════════════════════════════
# orkiestrator.py – główny orkiestrator systemu multi-agentowego
# ═══════════════════════════════════════════════════════════════

import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Literal

import ollama
import psycopg2
from psycopg2.extras import RealDictCursor
import yfinance as yf

# Dodaj ścieżkę do shared/ (wspólny moduł dry_run)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.dry_run import dry_run_guard, sprawdz_tryb, DRY_RUN

# Importy własnych agentów
import agent_techniczny
import agent_fundamentalny
import agent_sentymentu

MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")


# ───────────────────────────────────────────────
# 1. Struktury danych
# ───────────────────────────────────────────────

@dataclass
class WejscieAgenta:
    nazwa: str
    signal: str           # BUY / SELL / HOLD
    confidence: float
    waga: float           # waga w agregacji
    dane_surowe: dict     # pełny JSON z agenta


@dataclass
class DecyzjaKoncowa:
    ticker: str
    timestamp: str
    action: Literal["BUY", "SELL", "HOLD", "SKIP"]
    confidence: float
    consensus: Literal["strong", "moderate", "weak", "conflict"]
    pozycja_procent: float
    stop_loss: float | None
    take_profit: float | None
    horyzont: str
    reasoning: str
    devil_advocate: str
    sygnaly_wejsciowe: dict
    ryzyko_flagi: list[str] = field(default_factory=list)


# ───────────────────────────────────────────────
# 2. Uruchamianie agentów równolegle
# ───────────────────────────────────────────────

def uruchom_agentow(
    ticker: str,
    reddit_id: str = "",
    reddit_secret: str = "",
) -> list[WejscieAgenta]:
    """
    Uruchamia wszystkich trzech agentów równolegle.
    Jeśli jeden zawiedzie, reszta działa dalej.
    """

    def run_techniczny():
        wynik = agent_techniczny.analizuj(ticker)
        return WejscieAgenta(
            nazwa="techniczny",
            signal=wynik["signal"],
            confidence=wynik["confidence"],
            waga=0.35,
            dane_surowe=wynik,
        )

    def run_fundamentalny():
        wynik = agent_fundamentalny.analizuj(ticker)
        return WejscieAgenta(
            nazwa="fundamentalny",
            signal=wynik["signal"],
            confidence=wynik["confidence"],
            waga=0.45,
            dane_surowe=wynik,
        )

    def run_sentyment():
        wynik = agent_sentymentu.analizuj(ticker, reddit_id, reddit_secret)
        # Normalizuj signal sentymentu do BUY/SELL/HOLD
        mapa = {
            "BULLISH": "BUY",
            "SLIGHTLY_BULLISH": "BUY",
            "NEUTRAL": "HOLD",
            "SLIGHTLY_BEARISH": "SELL",
            "BEARISH": "SELL",
        }
        wynik["signal"] = mapa.get(wynik.get("signal", "NEUTRAL"), "HOLD")
        return WejscieAgenta(
            nazwa="sentyment",
            signal=wynik["signal"],
            confidence=wynik["confidence"],
            waga=0.20,
            dane_surowe=wynik,
        )

    zadania = {
        "techniczny":    run_techniczny,
        "fundamentalny": run_fundamentalny,
        "sentyment":     run_sentyment,
    }

    wyniki = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fn): nazwa for nazwa, fn in zadania.items()}
        for future in as_completed(futures):
            nazwa = futures[future]
            try:
                wyniki.append(future.result())
                print(f"  [{nazwa}] gotowy")
            except Exception as e:
                print(f"  [{nazwa}] BŁĄD: {e}")

    return wyniki


# ───────────────────────────────────────────────
# 3. Agregacja sygnałów
# ───────────────────────────────────────────────

def _normalizuj_signal(signal: str) -> float:
    """BUY=+1, HOLD=0, SELL=-1, WATCH=+0.3"""
    s = signal.upper()
    if s in ("BUY", "STRONG_BUY"):
        return 1.0
    if s in ("SELL", "STRONG_SELL"):
        return -1.0
    if s == "WATCH":
        return 0.3
    return 0.0


def agreguj_sygnaly(agenci: list[WejscieAgenta]) -> dict:
    """
    Ważona agregacja + detekcja siły konsensusu.
    Zwraca słownik gotowy do wklejenia w prompt LLM.
    """
    if not agenci:
        return {}

    suma_wag  = sum(a.waga for a in agenci)
    score     = sum(
        _normalizuj_signal(a.signal) * a.confidence * a.waga
        for a in agenci
    ) / suma_wag

    sygnaly  = [_normalizuj_signal(a.signal) for a in agenci]
    rozstep  = max(sygnaly) - min(sygnaly)

    if rozstep == 0:
        consensus = "strong"
    elif rozstep <= 0.5:
        consensus = "moderate"
    elif rozstep <= 1.0:
        consensus = "weak"
    else:
        consensus = "conflict"

    avg_conf = sum(a.confidence * a.waga for a in agenci) / suma_wag

    # Proponowana wielkość pozycji (uproszczony Kelly)
    if consensus == "conflict" or avg_conf < 0.55:
        pozycja = 0.0
    else:
        pozycja = min(10.0, round(avg_conf * abs(score) * 15, 1))

    return {
        "score_agregowany":           round(score, 3),
        "consensus":                  consensus,
        "rozstep_sygnalow":           round(rozstep, 2),
        "avg_confidence":             round(avg_conf, 3),
        "proponowana_pozycja_procent": pozycja,
        "sygnaly": {
            a.nazwa: {
                "signal":     a.signal,
                "confidence": a.confidence,
                "waga":       a.waga,
            }
            for a in agenci
        },
    }


# ───────────────────────────────────────────────
# 4. Bramka ryzyka
# ───────────────────────────────────────────────

@dataclass
class KonfiguracjaRyzyka:
    max_pozycja_procent: float  = 8.0
    min_confidence: float       = 0.55
    blokuj_przy_konflikcie: bool = True
    max_drawdown_portfela: float = 15.0
    biezacy_drawdown: float      = 0.0


def sprawdz_ryzyko(
    agregacja: dict,
    cfg: KonfiguracjaRyzyka,
) -> tuple[bool, list[str]]:
    """
    Zwraca (czy_przepuscic, lista_flag).
    False = orkiestrator zwraca SKIP bez wywoływania LLM.
    """
    flagi: list[str] = []

    if cfg.biezacy_drawdown >= cfg.max_drawdown_portfela:
        flagi.append(
            f"STOP: drawdown {cfg.biezacy_drawdown:.1f}% "
            f">= limit {cfg.max_drawdown_portfela:.1f}%"
        )
        return False, flagi

    if agregacja.get("consensus") == "conflict" and cfg.blokuj_przy_konflikcie:
        flagi.append("CONFLICT: agenci mają sprzeczne sygnały")
        return False, flagi

    if agregacja.get("avg_confidence", 0) < cfg.min_confidence:
        flagi.append(
            f"LOW_CONF: pewność {agregacja['avg_confidence']:.2f} "
            f"< min {cfg.min_confidence}"
        )
        return False, flagi

    # Obetnij pozycję do limitu (nie blokuj, tylko zmniejsz)
    if agregacja.get("proponowana_pozycja_procent", 0) > cfg.max_pozycja_procent:
        flagi.append(
            f"SIZE_CAP: obcinam pozycję do {cfg.max_pozycja_procent}%"
        )
        agregacja["proponowana_pozycja_procent"] = cfg.max_pozycja_procent

    return True, flagi


# ───────────────────────────────────────────────
# 5. Deliberacja LLM
# ───────────────────────────────────────────────

PROMPT_DELIBERACJA = """Jesteś głównym analitykiem inwestycyjnym. Trzy niezależne agenty AI zbadały spółkę {ticker} i wydały poniższe opinie. Twoim zadaniem jest SYNTEZA i ostateczna decyzja.

=== WYNIKI AGENTÓW ===

AGENT TECHNICZNY (waga 35%):
Signal: {tech_signal} | Pewność: {tech_conf:.0%}
Stop-loss: {tech_stop} | Take-profit: {tech_tp}
Kluczowe sygnały: {tech_sygnaly}
Uzasadnienie: {tech_reason}

AGENT FUNDAMENTALNY (waga 45%):
Signal: {fund_signal} | Pewność: {fund_conf:.0%}
Wycena: {fund_wycena} | Margin of Safety: {fund_mos}
Jakość spółki: {fund_jakosc}
Uzasadnienie: {fund_reason}

AGENT SENTYMENTU (waga 20%):
Signal: {sent_signal} | Pewność: {sent_conf:.0%}
Score sentymentu: {sent_score}
Narracja rynkowa: {sent_narracja}
Catalyst events: {sent_catalyst}

=== AGREGACJA MATEMATYCZNA ===
Score ważony: {score_agregowany} (zakres -1 do +1)
Consensus: {consensus}
Proponowana wielkość pozycji: {pozycja}% portfela
Cena aktualna: {cena}

=== TWOJE ZADANIE ===
1. Oceń czy agenci mają rację – czy ich argumenty są spójne?
2. Znajdź NAJSILNIEJSZY argument PRZECIWKO dominującej opinii (devil's advocate).
3. Wydaj ostateczną decyzję.

Odpowiedz WYŁĄCZNIE w JSON (bez markdown):
{{
  "action": "BUY" | "SELL" | "HOLD" | "SKIP",
  "confidence": 0.0-1.0,
  "pozycja_procent": 0.0-10.0,
  "stop_loss": <liczba lub null>,
  "take_profit": <liczba lub null>,
  "horyzont": "1-3 dni" | "1-2 tygodnie" | "1-3 miesiące" | "6-12 miesięcy",
  "reasoning": "2-3 zdania dlaczego ta decyzja",
  "devil_advocate": "najsilniejszy argument przeciwny",
  "kluczowy_czynnik": "jeden czynnik który najbardziej wpłynął na decyzję"
}}"""


def deliberuj(
    ticker: str,
    agenci: list[WejscieAgenta],
    agregacja: dict,
    cena: float | None,
) -> dict:
    """Wywołuje LLM do końcowej deliberacji."""

    def get(nazwa: str, klucz: str, default="brak") -> str:
        agent = next((a for a in agenci if a.nazwa == nazwa), None)
        if not agent:
            return default
        val = agent.dane_surowe.get(klucz, default)
        return str(val) if val is not None else default

    def get_lista(nazwa: str, klucz: str) -> str:
        agent = next((a for a in agenci if a.nazwa == nazwa), None)
        if not agent:
            return "brak"
        val = agent.dane_surowe.get(klucz, [])
        return ", ".join(val) if isinstance(val, list) else str(val)

    prompt = PROMPT_DELIBERACJA.format(
        ticker=ticker,

        tech_signal=get("techniczny", "signal"),
        tech_conf=float(get("techniczny", "confidence", "0")),
        tech_stop=get("techniczny", "stop_loss"),
        tech_tp=get("techniczny", "take_profit"),
        tech_sygnaly=get_lista("techniczny", "kluczowe_sygnaly"),
        tech_reason=get("techniczny", "reasoning"),

        fund_signal=get("fundamentalny", "signal"),
        fund_conf=float(get("fundamentalny", "confidence", "0")),
        fund_wycena=get("fundamentalny", "ocena_wyceny"),
        fund_mos=get("fundamentalny", "margin_of_safety"),
        fund_jakosc=get("fundamentalny", "ocena_jakosci"),
        fund_reason=get("fundamentalny", "reasoning"),

        sent_signal=get("sentyment", "signal"),
        sent_conf=float(get("sentyment", "confidence", "0")),
        sent_score=get("sentyment", "score"),
        sent_narracja=get("sentyment", "narracja_rynkowa"),
        sent_catalyst=get_lista("sentyment", "catalyst_events"),

        score_agregowany=agregacja.get("score_agregowany", 0),
        consensus=agregacja.get("consensus", "unknown"),
        pozycja=agregacja.get("proponowana_pozycja_procent", 0),
        cena=f"{cena:.2f}" if cena else "nieznana",
    )

    odpowiedz = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0.15},
    )

    tekst = odpowiedz["message"]["content"].strip()
    if "```" in tekst:
        tekst = tekst.split("```")[1].lstrip("json")

    return json.loads(tekst)


# ───────────────────────────────────────────────
# 6. Baza danych PostgreSQL
# ───────────────────────────────────────────────

# Konfiguracja połączenia PostgreSQL
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB = os.getenv("POSTGRES_DB", "tradeagent")
POSTGRES_USER = os.getenv("POSTGRES_USER", "tradeagent")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "tradeagent")


def get_db_connection():
    """Tworzy połączenie z PostgreSQL."""
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )


def init_db():
    """Inicjalizuje połączenie z PostgreSQL (tabele są tworzone przez init-db.sql)."""
    con = get_db_connection()
    return con


def zapisz_decyzje(
    con,
    decyzja: DecyzjaKoncowa,
    cena: float | None,
) -> int:
    """Zapisuje decyzję do bazy PostgreSQL. Zwraca id wiersza."""
    tryb = "DRY_RUN" if DRY_RUN else os.getenv("IBKR_TRADING_MODE", "paper").upper()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO decyzje
        (ticker, timestamp, action, confidence, consensus,
         pozycja_procent, stop_loss, take_profit, cena_wejscia,
         reasoning, devil_advocate, dane_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        decyzja.ticker,
        decyzja.timestamp,
        decyzja.action,
        decyzja.confidence,
        decyzja.consensus,
        decyzja.pozycja_procent,
        decyzja.stop_loss,
        decyzja.take_profit,
        cena,
        decyzja.reasoning,
        decyzja.devil_advocate,
        json.dumps(asdict(decyzja), ensure_ascii=False),
    ))
    decyzja_id = cur.fetchone()[0]
    con.commit()
    cur.close()
    return decyzja_id


def zapisz_zlecenie_dry_run(
    con,
    ticker: str,
    akcja: str,
    ilosc: int,
    cena: float,
    decyzja_id: int,
):
    """Zapisuje symulowane zlecenie (DRY_RUN) do bazy PostgreSQL."""
    cur = con.cursor()
    cur.execute("""
        INSERT INTO zlecenia
        (timestamp, ticker, akcja, ilosc, typ, status,
         wypelniona_cena, wartosc_usd, blad, decyzja_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        datetime.now().isoformat(),
        ticker, akcja, ilosc,
        "LMT", "DRY_RUN",
        cena, round(cena * ilosc, 2),
        "", decyzja_id,
    ))
    con.commit()
    cur.close()


# ───────────────────────────────────────────────
# 7. Wykonanie decyzji (broker lub dry-run)
# ───────────────────────────────────────────────

async def wykonaj_decyzje_orkiestratora(
    decyzja: DecyzjaKoncowa,
    kapital_portfela: float,
    con,
    decyzja_id: int,
) -> dict:
    """
    Łącznik między orkiestratorem a brokerem.

    DRY_RUN=true  → loguje co BYŁOBY wysłane, zero transakcji
    DRY_RUN=false → przekazuje do broker_ibkr.py
    """
    if decyzja.action not in ("BUY", "SELL"):
        return {"status": "SKIP", "powod": decyzja.action}

    if decyzja.confidence < 0.65:
        return {
            "status": "SKIP",
            "powod": f"confidence {decyzja.confidence:.0%} < 65%",
        }

    # Pobierz aktualną cenę
    try:
        cena = float(yf.Ticker(decyzja.ticker).fast_info.get("lastPrice", 0))
    except Exception:
        cena = 0.0

    if cena <= 0:
        return {"status": "ERROR", "powod": "nie można pobrać ceny"}

    wartosc  = kapital_portfela * decyzja.pozycja_procent / 100
    ilosc    = max(1, int(wartosc / cena))

    if DRY_RUN:
        # ── Tryb testowy ─────────────────────────
        print(f"\n{'─'*50}")
        print(f"[DRY RUN] CO BYŁOBY WYSŁANE DO BROKERA:")
        print(f"  Ticker:      {decyzja.ticker}")
        print(f"  Akcja:       {decyzja.action}")
        print(f"  Ilość:       {ilosc} szt.")
        print(f"  Cena ~:      {cena:.2f} USD")
        print(f"  Wartość ~:   {cena * ilosc:.2f} USD")
        print(f"  Stop-loss:   {decyzja.stop_loss}")
        print(f"  Take-profit: {decyzja.take_profit}")
        print(f"  Horyzont:    {decyzja.horyzont}")
        print(f"{'─'*50}")

        zapisz_zlecenie_dry_run(
            con, decyzja.ticker, decyzja.action,
            ilosc, cena, decyzja_id,
        )

        return {
            "status":    "DRY_RUN",
            "ticker":    decyzja.ticker,
            "akcja":     decyzja.action,
            "ilosc":     ilosc,
            "cena":      cena,
            "wartosc":   round(cena * ilosc, 2),
        }

    else:
        # ── Tryb paper/live → broker ──────────────
        try:
            from broker_ibkr import wykonaj_przez_ibkr
            wynik = await wykonaj_przez_ibkr({
                "ticker":      decyzja.ticker,
                "akcja":       decyzja.action,
                "ilosc":       ilosc,
                "cena":        cena,
                "stop_loss":   decyzja.stop_loss,
                "take_profit": decyzja.take_profit,
                "decyzja_id":  decyzja_id,
                "reasoning":   decyzja.reasoning,
            })
            return {"status": "SENT", "wynik_brokera": wynik}

        except ImportError:
            print("[BŁĄD] Brak modułu broker_ibkr")
            return {"status": "ERROR", "powod": "brak modułu brokera"}
        except Exception as e:
            print(f"[BŁĄD] Broker: {e}")
            return {"status": "ERROR", "powod": str(e)}


# ───────────────────────────────────────────────
# 8. Główna funkcja analizuj()
# ───────────────────────────────────────────────

def analizuj(
    ticker: str,
    cfg: KonfiguracjaRyzyka = KonfiguracjaRyzyka(),
    reddit_id: str = "",
    reddit_secret: str = "",
    kapital: float = 10_000.0,
) -> DecyzjaKoncowa:

    print(f"\n{'═'*50}")
    print(f"ORKIESTRATOR: {ticker}  [{datetime.now().strftime('%H:%M:%S')}]")

    # 1. Uruchom agentów równolegle
    agenci = uruchom_agentow(ticker, reddit_id, reddit_secret)

    if not agenci:
        raise RuntimeError("Żaden agent nie zwrócił wyników")

    # 2. Agregacja
    agregacja = agreguj_sygnaly(agenci)
    print(
        f"  Score: {agregacja['score_agregowany']:+.3f} | "
        f"Consensus: {agregacja['consensus']} | "
        f"Conf: {agregacja['avg_confidence']:.0%}"
    )

    # 3. Bramka ryzyka
    cena = agenci[0].dane_surowe.get("cena") if agenci else None
    try:
        cena = float(cena) if cena else None
    except (TypeError, ValueError):
        cena = None

    przepuscic, flagi = sprawdz_ryzyko(agregacja, cfg)

    if not przepuscic:
        print(f"  RYZYKO BLOKUJE: {flagi}")
        decyzja = DecyzjaKoncowa(
            ticker=ticker,
            timestamp=datetime.now().isoformat(),
            action="SKIP",
            confidence=0.0,
            consensus=agregacja.get("consensus", "conflict"),
            pozycja_procent=0.0,
            stop_loss=None,
            take_profit=None,
            horyzont="n/a",
            reasoning="Zablokowane przez bramkę ryzyka.",
            devil_advocate="",
            sygnaly_wejsciowe=agregacja,
            ryzyko_flagi=flagi,
        )
    else:
        # 4. Deliberacja LLM
        print("  LLM deliberuje...")
        wynik_llm = deliberuj(ticker, agenci, agregacja, cena)

        decyzja = DecyzjaKoncowa(
            ticker=ticker,
            timestamp=datetime.now().isoformat(),
            action=wynik_llm["action"],
            confidence=wynik_llm["confidence"],
            consensus=agregacja["consensus"],
            pozycja_procent=wynik_llm.get("pozycja_procent", 0),
            stop_loss=wynik_llm.get("stop_loss"),
            take_profit=wynik_llm.get("take_profit"),
            horyzont=wynik_llm.get("horyzont", ""),
            reasoning=wynik_llm["reasoning"],
            devil_advocate=wynik_llm["devil_advocate"],
            sygnaly_wejsciowe=agregacja,
            ryzyko_flagi=flagi,
        )

    # 5. Zapis do bazy
    con = init_db()
    decyzja_id = zapisz_decyzje(con, decyzja, cena)

    print(
        f"  DECYZJA: {decyzja.action} | "
        f"Pewność: {decyzja.confidence:.0%} | "
        f"Pozycja: {decyzja.pozycja_procent}%"
    )
    if decyzja.reasoning:
        print(f"  Powód: {decyzja.reasoning}")
    if decyzja.devil_advocate:
        print(f"  Kontra: {decyzja.devil_advocate}")

    # 6. Wykonaj (broker lub dry-run)
    if decyzja.action in ("BUY", "SELL"):
        asyncio.run(
            wykonaj_decyzje_orkiestratora(decyzja, kapital, con, decyzja_id)
        )

    con.close()
    return decyzja


# ───────────────────────────────────────────────
# 9. Monitorowanie wyników (opcjonalne)
# ───────────────────────────────────────────────

def zamknij_pozycje(decyzja_id: int, cena_wyjscia: float):
    """
    Wywołaj ręcznie po zamknięciu pozycji.
    Uzupełnia tabelę wyniki o rzeczywisty zwrot.
    """
    con = init_db()
    cur = con.cursor()
    cur.execute(
        "SELECT cena_wejscia, action FROM decyzje WHERE id=%s",
        (decyzja_id,)
    )
    row = cur.fetchone()

    if not row:
        print(f"Brak decyzji o id={decyzja_id}")
        cur.close()
        con.close()
        return

    cena_wejscia, action = row
    if not cena_wejscia:
        print("Brak ceny wejścia – nie można policzyć zwrotu")
        cur.close()
        con.close()
        return

    zwrot = (cena_wyjscia - cena_wejscia) / cena_wejscia
    if action == "SELL":
        zwrot = -zwrot

    cur.execute("""
        INSERT INTO wyniki
        (decyzja_id, cena_wyjscia, zwrot_procent, czy_trafiona, timestamp_zamkniecia)
        VALUES (%s, %s, %s, %s, %s)
    """, (
        decyzja_id,
        cena_wyjscia,
        round(zwrot * 100, 4),
        True if zwrot > 0 else False,
        datetime.now().isoformat(),
    ))
    con.commit()
    cur.close()
    con.close()

    print(
        f"Pozycja {decyzja_id} zamknięta: "
        f"zwrot {zwrot:.2%} | "
        f"{'TRAFIONA' if zwrot > 0 else 'CHYBIONA'}"
    )


# ───────────────────────────────────────────────
# 10. Punkt wejścia – scheduler
# ───────────────────────────────────────────────

if __name__ == "__main__":
    import schedule
    import time

    # Pokaż tryb na starcie (DRY_RUN / PAPER / LIVE)
    sprawdz_tryb()

    # Konfiguracja z zmiennych środowiskowych
    TICKERY  = os.getenv("TICKERY", "AAPL,NVDA,MSFT").split(",")
    KAPITAL  = float(os.getenv("KAPITAL_PORTFELA", "10000"))
    CYKL_H   = int(os.getenv("CYKL_GODZINY", "4"))

    CFG = KonfiguracjaRyzyka(
        max_pozycja_procent  = float(os.getenv("MAX_POZYCJA_PROCENT", "8")),
        max_drawdown_portfela= float(os.getenv("MAX_DRAWDOWN_PROCENT", "15")),
        min_confidence       = 0.55,
        blokuj_przy_konflikcie = True,
    )

    REDDIT_ID     = os.getenv("REDDIT_CLIENT_ID", "")
    REDDIT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")

    def cykl():
        print(f"\n{'▶'*3} Cykl analizy: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        for ticker in TICKERY:
            ticker = ticker.strip()
            if not ticker:
                continue
            try:
                analizuj(
                    ticker=ticker,
                    cfg=CFG,
                    reddit_id=REDDIT_ID,
                    reddit_secret=REDDIT_SECRET,
                    kapital=KAPITAL,
                )
            except Exception as e:
                print(f"[BŁĄD] {ticker}: {e}")

    # Uruchom od razu, potem co CYKL_H godzin
    cykl()
    schedule.every(CYKL_H).hours.do(cykl)

    print(f"\nNastępny cykl za {CYKL_H}h. Ctrl+C aby zatrzymać.\n")

    while True:
        # Kill switch – plik tworzony przez start.sh stop
        if os.path.exists("/tmp/KILL_SWITCH"):
            print("\n[KILL SWITCH] Zatrzymuję orkiestratora.")
            break
        schedule.run_pending()
        time.sleep(30)