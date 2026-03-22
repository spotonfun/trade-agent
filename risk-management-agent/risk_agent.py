import json
import sqlite3
import yfinance as yf
import numpy as np
import ollama
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field, asdict
from typing import Literal

MODEL = "llama3.2"

# ──────────────────────────────────────────────
# 1. Konfiguracja ryzyka – JEDYNE ŹRÓDŁO PRAWDY
#    Zmień tu, a nie w prompcie LLM
# ──────────────────────────────────────────────

@dataclass
class KonfiguracjaRyzyka:
    # Limity pozycji
    max_pozycja_procent: float = 8.0       # max % portfela na 1 spółkę
    max_sektor_procent: float = 30.0       # max % na 1 sektor
    max_asset_class_procent: float = 40.0  # max % na kryptowaluty / akcje US itp.
    min_rr_ratio: float = 1.5              # min Risk:Reward (zysk/ryzyko)

    # Stop-lossy
    max_strata_pozycja_procent: float = 7.0   # max strata na pojedynczej pozycji
    trailing_stop_procent: float = 5.0        # trailing stop od szczytu

    # Portfel globalny
    max_drawdown_portfela: float = 15.0    # % – emergency stop wszystkiego
    max_dzienny_strata: float = 5.0        # % – stop handlu na dziś
    min_cash_procent: float = 10.0         # min gotówki w portfelu zawsze

    # Korelacja
    max_korelacja_nowej: float = 0.75      # max korelacja z istniejącą pozycją

    # Volatility regime
    wysoka_vola_vix: float = 30.0          # VIX > X → obcinaj pozycje o 50%
    bardzo_wysoka_vola_vix: float = 40.0   # VIX > X → tylko HOLD, brak nowych


@dataclass
class StanPortfela:
    kapital_total: float                           # całkowita wartość
    cash: float                                    # wolna gotówka
    pozycje: dict[str, dict] = field(default_factory=dict)
    # Format pozycji: {"AAPL": {"wartosc": 5000, "cena_wejscia": 150,
    #                            "ilosc": 33, "sektor": "Technology",
    #                            "szczyt_ceny": 165, "timestamp_wejscia": "..."}}

    @property
    def zainwestowany_procent(self) -> float:
        if self.kapital_total == 0:
            return 0.0
        return (1 - self.cash / self.kapital_total) * 100

    @property
    def cash_procent(self) -> float:
        if self.kapital_total == 0:
            return 100.0
        return self.cash / self.kapital_total * 100

    def wartosc_sektora(self, sektor: str) -> float:
        return sum(
            p["wartosc"] for p in self.pozycje.values()
            if p.get("sektor") == sektor
        )

    def procent_sektora(self, sektor: str) -> float:
        if self.kapital_total == 0:
            return 0.0
        return self.wartosc_sektora(sektor) / self.kapital_total * 100


# ──────────────────────────────────────────────
# 2. Metryki ryzyka
# ──────────────────────────────────────────────

def pobierz_vix() -> float | None:
    """VIX = Fear Index. Wysoki VIX → wysoka niepewność rynku."""
    try:
        vix = yf.Ticker("^VIX")
        return float(vix.info.get("regularMarketPrice") or
                     vix.fast_info.get("lastPrice", 0))
    except Exception:
        return None


def oblicz_var(
    ticker: str,
    wielkosc_pozycji: float,
    okres_dni: int = 252,
    poziom_ufnosci: float = 0.95,
) -> float | None:
    """
    Historyczny VaR (Value at Risk) dla pozycji.
    Zwraca maksymalną dzienną stratę w złotówkach/dolarach
    z zadanym poziomem ufności.
    """
    try:
        df = yf.download(ticker, period="1y", interval="1d",
                         auto_adjust=True, progress=False)
        if len(df) < 30:
            return None
        zwroty = df["Close"].pct_change().dropna().values
        percentyl = np.percentile(zwroty, (1 - poziom_ufnosci) * 100)
        return abs(percentyl * wielkosc_pozycji)
    except Exception:
        return None


def oblicz_korelacje(
    nowy_ticker: str,
    istniejace_tickery: list[str],
    okres_dni: int = 90,
) -> dict[str, float]:
    """
    Liczy korelację nowego tickera z każdą istniejącą pozycją.
    Wysoka korelacja = dodajemy podobne ryzyko, które już mamy.
    """
    if not istniejace_tickery:
        return {}

    wszystkie = [nowy_ticker] + istniejace_tickery
    try:
        dane = yf.download(
            wszystkie, period="3mo", interval="1d",
            auto_adjust=True, progress=False
        )["Close"]

        if isinstance(dane, dict):  # jeden ticker
            return {}

        zwroty = dane.pct_change().dropna()
        korelacje = zwroty.corr()

        wynik = {}
        for t in istniejace_tickery:
            if t in korelacje.columns and nowy_ticker in korelacje.index:
                wynik[t] = round(float(korelacje.loc[nowy_ticker, t]), 3)
        return wynik

    except Exception:
        return {}


def oblicz_drawdown_portfela(
    historia_wartosci: list[float],
) -> float:
    """Max drawdown od szczytu do dolka w historii portfela (w %)."""
    if len(historia_wartosci) < 2:
        return 0.0
    szczyt = historia_wartosci[0]
    max_dd = 0.0
    for v in historia_wartosci:
        if v > szczyt:
            szczyt = v
        dd = (szczyt - v) / szczyt * 100
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def oblicz_rr_ratio(
    cena: float,
    stop_loss: float | None,
    take_profit: float | None,
    kierunek: str = "BUY",
) -> float | None:
    """Risk:Reward ratio. Chcemy >= 1.5 (zarabiamy 1.5x więcej niż ryzykujemy)."""
    if not stop_loss or not take_profit or cena <= 0:
        return None
    if kierunek == "BUY":
        ryzyko = cena - stop_loss
        zysk = take_profit - cena
    else:
        ryzyko = stop_loss - cena
        zysk = cena - take_profit

    if ryzyko <= 0:
        return None
    return round(zysk / ryzyko, 2)


# ──────────────────────────────────────────────
# 3. Sprawdzanie limitów (twarde reguły – nie LLM)
# ──────────────────────────────────────────────

@dataclass
class WynikSprawdzenia:
    przeszlo: bool
    akcja: Literal["PASS", "MODIFY", "BLOCK", "EMERGENCY_STOP"]
    zmodyfikowana_wielkosc: float | None  # None = bez zmian
    flagi: list[str] = field(default_factory=list)
    powod_blokady: str = ""


def sprawdz_twarde_limity(
    ticker: str,
    kierunek: str,           # BUY / SELL
    wielkosc_procent: float, # % portfela
    stop_loss: float | None,
    take_profit: float | None,
    cena: float,
    sektor: str,
    portfel: StanPortfela,
    cfg: KonfiguracjaRyzyka,
) -> WynikSprawdzenia:
    """
    Sprawdza wszystkie twarde reguły PO KOLEI.
    Kolejność ważna – najpierw emergency, potem modyfikacje.
    """
    flagi: list[str] = []
    wielkosc_po_modyfikacji = wielkosc_procent

    # --- EMERGENCY STOP (natychmiastowy block) ---

    vix = pobierz_vix()
    if vix and vix > cfg.bardzo_wysoka_vola_vix:
        return WynikSprawdzenia(
            przeszlo=False, akcja="EMERGENCY_STOP",
            zmodyfikowana_wielkosc=None,
            flagi=[f"VIX={vix:.1f} > {cfg.bardzo_wysoka_vola_vix} – rynek paniki"],
            powod_blokady=f"VIX={vix:.1f}: ekstremalna zmienność, brak nowych pozycji",
        )

    # Drawdown portfela
    # (w prawdziwym systemie pobieraj historię z bazy)
    historia = [portfel.kapital_total]  # uproszczenie – zastąp prawdziwą historią
    dd = oblicz_drawdown_portfela(historia)
    if dd >= cfg.max_drawdown_portfela:
        return WynikSprawdzenia(
            przeszlo=False, akcja="EMERGENCY_STOP",
            zmodyfikowana_wielkosc=None,
            flagi=[f"Drawdown {dd:.1f}% >= limit {cfg.max_drawdown_portfela}%"],
            powod_blokady="Max drawdown osiągnięty – handel zatrzymany",
        )

    # --- BLOCK (reguła nie do obejścia) ---

    if portfel.cash_procent - wielkosc_procent < cfg.min_cash_procent:
        return WynikSprawdzenia(
            przeszlo=False, akcja="BLOCK",
            zmodyfikowana_wielkosc=None,
            flagi=["Za mało gotówki po transakcji"],
            powod_blokady=f"Minimalna rezerwa gotówki {cfg.min_cash_procent}% musi zostać",
        )

    rr = oblicz_rr_ratio(cena, stop_loss, take_profit, kierunek)
    if rr is not None and rr < cfg.min_rr_ratio:
        return WynikSprawdzenia(
            przeszlo=False, akcja="BLOCK",
            zmodyfikowana_wielkosc=None,
            flagi=[f"R:R={rr:.2f} < minimum {cfg.min_rr_ratio}"],
            powod_blokady=f"Zbyt słaby stosunek zysku do ryzyka: {rr:.2f}",
        )

    # Stop-loss zbyt daleko
    if stop_loss:
        strata_procent = abs(cena - stop_loss) / cena * 100
        if strata_procent > cfg.max_strata_pozycja_procent:
            return WynikSprawdzenia(
                przeszlo=False, akcja="BLOCK",
                zmodyfikowana_wielkosc=None,
                flagi=[f"Stop-loss za daleko: {strata_procent:.1f}% > max {cfg.max_strata_pozycja_procent}%"],
                powod_blokady="Stop-loss jest za szeroki – ryzyko zbyt duże",
            )

    # --- MODIFY (przepuszczamy, ale zmniejszamy) ---

    # Limit pojedynczej pozycji
    if wielkosc_po_modyfikacji > cfg.max_pozycja_procent:
        flagi.append(
            f"Obcięto z {wielkosc_po_modyfikacji:.1f}% "
            f"do max {cfg.max_pozycja_procent}%"
        )
        wielkosc_po_modyfikacji = cfg.max_pozycja_procent

    # Limit sektora
    sektor_obecny = portfel.procent_sektora(sektor)
    sektor_po = sektor_obecny + wielkosc_po_modyfikacji
    if sektor_po > cfg.max_sektor_procent:
        dostepne = max(0.0, cfg.max_sektor_procent - sektor_obecny)
        flagi.append(
            f"Sektor {sektor}: {sektor_obecny:.1f}% → limit {cfg.max_sektor_procent}%. "
            f"Obcinam do {dostepne:.1f}%"
        )
        wielkosc_po_modyfikacji = dostepne
        if wielkosc_po_modyfikacji < 1.0:
            return WynikSprawdzenia(
                przeszlo=False, akcja="BLOCK",
                zmodyfikowana_wielkosc=None,
                flagi=flagi,
                powod_blokady=f"Sektor {sektor} już na limicie",
            )

    # Wysoka zmienność (VIX) – obetnij pozycję o 50%
    if vix and vix > cfg.wysoka_vola_vix:
        wielkosc_po_modyfikacji *= 0.5
        flagi.append(f"VIX={vix:.1f} > {cfg.wysoka_vola_vix}: pozycja zmniejszona o 50%")

    # Korelacja z istniejącymi pozycjami
    istniejace = [t for t in portfel.pozycje if t != ticker]
    if istniejace:
        korelacje = oblicz_korelacje(ticker, istniejace)
        for t, kor in korelacje.items():
            if abs(kor) > cfg.max_korelacja_nowej:
                wielkosc_po_modyfikacji *= 0.6
                flagi.append(
                    f"Wysoka korelacja z {t} ({kor:.2f}): "
                    f"pozycja zmniejszona o 40%"
                )
                break  # jedna modyfikacja wystarczy

    # VaR check
    wielkosc_usd = wielkosc_po_modyfikacji / 100 * portfel.kapital_total
    var_95 = oblicz_var(ticker, wielkosc_usd)
    if var_95 and var_95 > portfel.kapital_total * 0.02:  # max 2% portfela dziennie
        flagi.append(f"VaR95={var_95:.0f} USD > 2% portfela – uwaga!")

    zmodyfikowano = abs(wielkosc_po_modyfikacji - wielkosc_procent) > 0.1
    akcja = "MODIFY" if zmodyfikowano else "PASS"

    return WynikSprawdzenia(
        przeszlo=True,
        akcja=akcja,
        zmodyfikowana_wielkosc=round(wielkosc_po_modyfikacji, 2),
        flagi=flagi,
    )


# ──────────────────────────────────────────────
# 4. LLM – komentarz jakościowy (nie blokuje!)
# ──────────────────────────────────────────────

def komentarz_llm(
    ticker: str,
    kierunek: str,
    wynik: WynikSprawdzenia,
    portfel: StanPortfela,
    vix: float | None,
) -> str:
    """
    LLM NIE podejmuje tu decyzji – tylko dodaje narrację.
    Wszystkie twarde blokady są już w wynik.
    """
    pozycje_txt = "\n".join([
        f"  {t}: {p['wartosc']:.0f} USD ({p['wartosc']/portfel.kapital_total*100:.1f}%) – {p.get('sektor','?')}"
        for t, p in portfel.pozycje.items()
    ]) or "  (brak otwartych pozycji)"

    prompt = f"""Jesteś risk managerem. Twarde limity zostały już sprawdzone algorytmicznie.
Napisz krótki komentarz jakościowy do tej decyzji.

TRANSAKCJA: {kierunek} {ticker}
WYNIK KONTROLI: {wynik.akcja}
FLAGI: {'; '.join(wynik.flagi) or 'brak'}
VIX: {vix or 'nieznany'}

PORTFEL (top pozycje):
{pozycje_txt}
Gotówka: {portfel.cash_procent:.1f}%

Napisz 2 zdania: (1) główne ryzyko tej transakcji w kontekście portfela,
(2) co obserwować po wejściu. Bądź konkretny i zwięzły.
Odpowiedz TYLKO tekstem, bez JSON, bez nagłówków."""

    try:
        odp = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.2},
        )
        return odp["message"]["content"].strip()
    except Exception:
        return "Brak komentarza LLM."


# ──────────────────────────────────────────────
# 5. Monitor ciągły – trailing stop i alerty
# ──────────────────────────────────────────────

def sprawdz_trailing_stops(
    portfel: StanPortfela,
    cfg: KonfiguracjaRyzyka,
) -> list[dict]:
    """
    Sprawdza czy jakieś pozycje osiągnęły trailing stop.
    Wywołuj co 15 minut z schedulera.
    Zwraca listę pozycji do zamknięcia.
    """
    do_zamkniecia = []

    for ticker, poz in portfel.pozycje.items():
        try:
            info = yf.Ticker(ticker).fast_info
            cena_aktualna = float(info.get("lastPrice", 0))
        except Exception:
            continue

        if cena_aktualna <= 0:
            continue

        # Zaktualizuj szczyt
        szczyt = poz.get("szczyt_ceny", poz["cena_wejscia"])
        if cena_aktualna > szczyt:
            poz["szczyt_ceny"] = cena_aktualna
            szczyt = cena_aktualna

        # Sprawdź trailing stop
        trailing_poziom = szczyt * (1 - cfg.trailing_stop_procent / 100)
        if cena_aktualna <= trailing_poziom:
            spadek_od_szczytu = (szczyt - cena_aktualna) / szczyt * 100
            do_zamkniecia.append({
                "ticker": ticker,
                "powod": "trailing_stop",
                "cena_wejscia": poz["cena_wejscia"],
                "szczyt_ceny": szczyt,
                "cena_aktualna": cena_aktualna,
                "spadek_od_szczytu_procent": round(spadek_od_szczytu, 2),
                "trailing_poziom": round(trailing_poziom, 2),
            })

    return do_zamkniecia


def sprawdz_rebalancing(
    portfel: StanPortfela,
    cfg: KonfiguracjaRyzyka,
) -> list[str]:
    """Zwraca listę alertów gdy coś wymaga rebalancingu."""
    alerty = []

    for ticker, poz in portfel.pozycje.items():
        udział = poz["wartosc"] / portfel.kapital_total * 100
        if udział > cfg.max_pozycja_procent * 1.2:  # 20% powyżej limitu
            alerty.append(
                f"REBALANCE: {ticker} urósł do {udział:.1f}% "
                f"(limit {cfg.max_pozycja_procent}%)"
            )

    for sektor in set(p.get("sektor", "") for p in portfel.pozycje.values()):
        if not sektor:
            continue
        udział = portfel.procent_sektora(sektor)
        if udział > cfg.max_sektor_procent:
            alerty.append(
                f"REBALANCE SEKTOR: {sektor} na {udział:.1f}% "
                f"(limit {cfg.max_sektor_procent}%)"
            )

    if portfel.cash_procent < cfg.min_cash_procent:
        alerty.append(
            f"NISKA GOTÓWKA: {portfel.cash_procent:.1f}% "
            f"(min {cfg.min_cash_procent}%)"
        )

    return alerty


# ──────────────────────────────────────────────
# 6. Baza danych audytu
# ──────────────────────────────────────────────

def init_db(sciezka: str = "ryzyko_audyt.db") -> sqlite3.Connection:
    con = sqlite3.connect(sciezka)
    con.execute("""
        CREATE TABLE IF NOT EXISTS audyt (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT,
            ticker       TEXT,
            kierunek     TEXT,
            akcja        TEXT,
            wielkosc_in  REAL,
            wielkosc_out REAL,
            flagi        TEXT,
            powod        TEXT,
            komentarz    TEXT,
            vix          REAL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trailing_stops (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT,
            ticker      TEXT,
            szczyt_ceny REAL,
            cena_stop   REAL,
            cena_aktualna REAL,
            wykonano    INTEGER DEFAULT 0
        )
    """)
    con.commit()
    return con


def zapisz_audyt(con: sqlite3.Connection, ticker: str, kierunek: str,
                 wielkosc_in: float, wynik: WynikSprawdzenia,
                 komentarz: str, vix: float | None):
    con.execute("""
        INSERT INTO audyt
        (timestamp, ticker, kierunek, akcja, wielkosc_in, wielkosc_out,
         flagi, powod, komentarz, vix)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(), ticker, kierunek,
        wynik.akcja, wielkosc_in,
        wynik.zmodyfikowana_wielkosc or wielkosc_in,
        "; ".join(wynik.flagi), wynik.powod_blokady,
        komentarz, vix,
    ))
    con.commit()


# ──────────────────────────────────────────────
# 7. Główna funkcja agenta
# ──────────────────────────────────────────────

def ocen_transakcje(
    ticker: str,
    kierunek: str,
    wielkosc_procent: float,
    stop_loss: float | None,
    take_profit: float | None,
    cena: float,
    sektor: str,
    portfel: StanPortfela,
    cfg: KonfiguracjaRyzyka = KonfiguracjaRyzyka(),
    db_sciezka: str = "ryzyko_audyt.db",
) -> dict:
    """
    Główna funkcja – wywołaj ją z orkiestratora zamiast bezpośrednio handlować.
    Zwraca słownik z decyzją gotowy do przekazania dalej.
    """
    print(f"  [RYZYKO] Sprawdzam: {kierunek} {ticker} "
          f"({wielkosc_procent:.1f}% portfela)")

    vix = pobierz_vix()
    if vix:
        print(f"  [RYZYKO] VIX = {vix:.1f}")

    # Twarde limity (kod – nie LLM)
    wynik = sprawdz_twarde_limity(
        ticker, kierunek, wielkosc_procent,
        stop_loss, take_profit, cena,
        sektor, portfel, cfg,
    )

    # Komentarz jakościowy (LLM – tylko narracja)
    komentarz = komentarz_llm(ticker, kierunek, wynik, portfel, vix)

    # Zapis audytu
    con = init_db(db_sciezka)
    zapisz_audyt(con, ticker, kierunek, wielkosc_procent,
                 wynik, komentarz, vix)
    con.close()

    print(f"  [RYZYKO] Wynik: {wynik.akcja} | "
          + (f"Nowa wielkość: {wynik.zmodyfikowana_wielkosc}%" if wynik.zmodyfikowana_wielkosc else "")
          + (f"Blokada: {wynik.powod_blokady}" if wynik.powod_blokady else ""))

    return {
        "akcja": wynik.akcja,
        "przeszlo": wynik.przeszlo,
        "wielkosc_zatwierdzona": wynik.zmodyfikowana_wielkosc,
        "flagi": wynik.flagi,
        "powod_blokady": wynik.powod_blokady,
        "komentarz_ryzyka": komentarz,
        "vix": vix,
    }


# ──────────────────────────────────────────────
# 8. Uruchomienie monitora ciągłego
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import schedule, time

    # Przykładowy stan portfela
    portfel = StanPortfela(
        kapital_total=100_000,
        cash=30_000,
        pozycje={
            "AAPL": {"wartosc": 8000, "cena_wejscia": 175, "ilosc": 45,
                     "sektor": "Technology", "szczyt_ceny": 182},
            "NVDA": {"wartosc": 7000, "cena_wejscia": 480, "ilosc": 14,
                     "sektor": "Technology", "szczyt_ceny": 510},
            "JPM":  {"wartosc": 5000, "cena_wejscia": 195, "ilosc": 25,
                     "sektor": "Financial Services", "szczyt_ceny": 198},
        },
    )

    cfg = KonfiguracjaRyzyka()

    def monitor():
        print(f"\n[{datetime.now().strftime('%H:%M')}] Monitor ryzyka...")

        # Trailing stops
        do_zamkniecia = sprawdz_trailing_stops(portfel, cfg)
        for poz in do_zamkniecia:
            print(f"  TRAILING STOP: zamknij {poz['ticker']} "
                  f"(spadek {poz['spadek_od_szczytu_procent']:.1f}% od szczytu)")

        # Rebalancing
        alerty = sprawdz_rebalancing(portfel, cfg)
        for alert in alerty:
            print(f"  {alert}")

    # Uruchom od razu i co 15 minut
    monitor()
    schedule.every(15).minutes.do(monitor)
    while True:
        schedule.run_pending()
        time.sleep(30)