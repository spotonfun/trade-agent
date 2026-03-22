import yfinance as yf
import requests
import json
import math
from datetime import datetime
from dataclasses import dataclass, asdict

# ──────────────────────────────────────────────
# 1. Struktury danych
# ──────────────────────────────────────────────

@dataclass
class DaneFundamentalne:
    ticker: str
    nazwa: str
    sektor: str
    branza: str
    waluta: str

    # Wycena
    pe_ratio: float | None
    forward_pe: float | None
    pb_ratio: float | None
    ps_ratio: float | None
    peg_ratio: float | None
    ev_ebitda: float | None

    # Rentowność
    marza_netto: float | None        # net margin
    marza_operacyjna: float | None
    roe: float | None                # return on equity
    roa: float | None
    ebitda_margin: float | None

    # Kondycja finansowa
    debt_equity: float | None
    current_ratio: float | None
    quick_ratio: float | None
    fcf: float | None                # free cash flow (ostatni rok)
    fcf_yield: float | None

    # Wzrost
    revenue_growth_yoy: float | None
    earnings_growth_yoy: float | None
    eps_ttm: float | None
    eps_forward: float | None

    # Dywidenda
    dividend_yield: float | None
    payout_ratio: float | None

    # Wycena rynkowa
    cena_aktualna: float | None
    market_cap: float | None
    shares_outstanding: float | None

    # DCF uproszczony
    dcf_intrinsic_value: float | None
    margin_of_safety: float | None   # (intrinsic - current) / intrinsic


# ──────────────────────────────────────────────
# 2. Pobieranie danych przez yfinance
# ──────────────────────────────────────────────

def _safe(val, default=None):
    """Zwraca None zamiast NaN/None z yfinance."""
    if val is None:
        return default
    try:
        if math.isnan(float(val)):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default

def oblicz_dcf(fcf: float | None, wzrost_5y: float,
               wzrost_terminal: float = 0.03,
               wacc: float = 0.10,
               shares: float | None = None) -> float | None:
    """
    Uproszczony 2-stage DCF.
    Etap 1: 5 lat z podaną stopą wzrostu.
    Etap 2: wzrost terminalny na wieczność.
    """
    if fcf is None or shares is None or shares == 0 or fcf <= 0:
        return None

    wzrost_5y = max(-0.5, min(wzrost_5y, 1.0))  # ograniczenie do rozsądnych wartości

    pv = 0.0
    fcf_t = fcf
    for t in range(1, 6):
        fcf_t *= (1 + wzrost_5y)
        pv += fcf_t / (1 + wacc) ** t

    # Wartość terminalna (Gordon Growth Model)
    terminal_fcf = fcf_t * (1 + wzrost_terminal)
    terminal_value = terminal_fcf / (wacc - wzrost_terminal)
    pv += terminal_value / (1 + wacc) ** 5

    return pv / shares  # wartość na akcję

def pobierz_dane(ticker: str) -> DaneFundamentalne:
    spółka = yf.Ticker(ticker)
    info = spółka.info

    cena = _safe(info.get("currentPrice") or info.get("regularMarketPrice"))
    shares = _safe(info.get("sharesOutstanding"))
    fcf = _safe(info.get("freeCashflow"))

    # Szacuj stopę wzrostu FCF z historii (fallback: earnings growth)
    wzrost = _safe(info.get("earningsGrowth"), 0.05)
    if wzrost is not None and wzrost > 1:
        wzrost = wzrost / 100  # niektóre wersje yfinance dają w %

    dcf_val = oblicz_dcf(fcf, wzrost, shares=shares)

    margin_of_safety = None
    if dcf_val and cena and dcf_val > 0:
        margin_of_safety = (dcf_val - cena) / dcf_val

    # FCF yield = FCF / market cap
    mktcap = _safe(info.get("marketCap"))
    fcf_yield = (fcf / mktcap) if (fcf and mktcap and mktcap > 0) else None

    return DaneFundamentalne(
        ticker=ticker,
        nazwa=info.get("longName", ticker),
        sektor=info.get("sector", "Nieznany"),
        branza=info.get("industry", "Nieznana"),
        waluta=info.get("currency", "USD"),

        pe_ratio=_safe(info.get("trailingPE")),
        forward_pe=_safe(info.get("forwardPE")),
        pb_ratio=_safe(info.get("priceToBook")),
        ps_ratio=_safe(info.get("priceToSalesTrailing12Months")),
        peg_ratio=_safe(info.get("pegRatio")),
        ev_ebitda=_safe(info.get("enterpriseToEbitda")),

        marza_netto=_safe(info.get("profitMargins")),
        marza_operacyjna=_safe(info.get("operatingMargins")),
        roe=_safe(info.get("returnOnEquity")),
        roa=_safe(info.get("returnOnAssets")),
        ebitda_margin=_safe(info.get("ebitdaMargins")),

        debt_equity=_safe(info.get("debtToEquity")),
        current_ratio=_safe(info.get("currentRatio")),
        quick_ratio=_safe(info.get("quickRatio")),
        fcf=fcf,
        fcf_yield=fcf_yield,

        revenue_growth_yoy=_safe(info.get("revenueGrowth")),
        earnings_growth_yoy=_safe(info.get("earningsGrowth")),
        eps_ttm=_safe(info.get("trailingEps")),
        eps_forward=_safe(info.get("forwardEps")),

        dividend_yield=_safe(info.get("dividendYield")),
        payout_ratio=_safe(info.get("payoutRatio")),

        cena_aktualna=cena,
        market_cap=mktcap,
        shares_outstanding=shares,

        dcf_intrinsic_value=dcf_val,
        margin_of_safety=margin_of_safety,
    )


# ──────────────────────────────────────────────
# 3. Benchmarki sektorowe (uproszczone)
# ──────────────────────────────────────────────

BENCHMARKI = {
    "Technology":         {"pe": 28, "pb": 6,  "roe": 0.18, "debt_eq": 80,  "marza": 0.18},
    "Healthcare":         {"pe": 22, "pb": 4,  "roe": 0.14, "debt_eq": 60,  "marza": 0.10},
    "Financial Services": {"pe": 14, "pb": 1.4,"roe": 0.11, "debt_eq": 200, "marza": 0.20},
    "Consumer Cyclical":  {"pe": 20, "pb": 4,  "roe": 0.15, "debt_eq": 100, "marza": 0.07},
    "Energy":             {"pe": 12, "pb": 1.8,"roe": 0.12, "debt_eq": 70,  "marza": 0.08},
    "Industrials":        {"pe": 18, "pb": 3,  "roe": 0.13, "debt_eq": 90,  "marza": 0.08},
    "Communication":      {"pe": 22, "pb": 4,  "roe": 0.14, "debt_eq": 80,  "marza": 0.12},
    "default":            {"pe": 18, "pb": 3,  "roe": 0.12, "debt_eq": 100, "marza": 0.10},
}

def benchmark(sektor: str) -> dict:
    return BENCHMARKI.get(sektor, BENCHMARKI["default"])


# ──────────────────────────────────────────────
# 4. Budowanie promptu
# ──────────────────────────────────────────────

def _fmt(val, suffix="", precision=2, scale=1.0):
    if val is None:
        return "brak danych"
    return f"{val * scale:.{precision}f}{suffix}"

def buduj_prompt(d: DaneFundamentalne) -> str:
    bm = benchmark(d.sektor)

    def vs(val, ref, im_mniej_tym_lepiej=False):
        """Dodaje znacznik vs. benchmark."""
        if val is None or ref is None:
            return ""
        if im_mniej_tym_lepiej:
            return " [LEPIEJ niż sektor]" if val < ref else " [GORZEJ niż sektor]"
        return " [LEPIEJ niż sektor]" if val > ref else " [GORZEJ niż sektor]"

    dcf_info = "brak danych"
    if d.dcf_intrinsic_value and d.cena_aktualna:
        dcf_info = (
            f"Wartość wewnętrzna DCF: {d.dcf_intrinsic_value:.2f} {d.waluta} | "
            f"Cena rynkowa: {d.cena_aktualna:.2f} | "
            f"Margin of safety: {d.margin_of_safety*100:.1f}% "
            f"({'NIEDOWARTOŚCIOWANA' if d.margin_of_safety > 0.15 else 'PRZEWARTOŚCIOWANA' if d.margin_of_safety < -0.10 else 'w okolicach wartości godziwej'})"
        )

    return f"""Jesteś analitykiem fundamentalnym. Oceń atrakcyjność inwestycyjną poniższej spółki.

SPÓŁKA: {d.nazwa} ({d.ticker})
SEKTOR: {d.sektor} | BRANŻA: {d.branza}
WALUTA: {d.waluta}

=== WYCENA (vs. benchmark sektora) ===
P/E (TTM):        {_fmt(d.pe_ratio)}    [benchmark: {bm['pe']}]{vs(bm['pe'], d.pe_ratio, True) if d.pe_ratio else ''}
Forward P/E:      {_fmt(d.forward_pe)}
P/B:              {_fmt(d.pb_ratio)}    [benchmark: {bm['pb']}]
P/S:              {_fmt(d.ps_ratio)}
PEG:              {_fmt(d.peg_ratio)}   [<1 = potencjalnie tanie]
EV/EBITDA:        {_fmt(d.ev_ebitda)}

=== WYCENA DCF ===
{dcf_info}

=== RENTOWNOŚĆ ===
Marża netto:      {_fmt(d.marza_netto, '%', scale=100)}   [benchmark: {bm['marza']*100:.0f}%]{vs(d.marza_netto, bm['marza'])}
Marża operacyjna: {_fmt(d.marza_operacyjna, '%', scale=100)}
ROE:              {_fmt(d.roe, '%', scale=100)}   [benchmark: {bm['roe']*100:.0f}%]{vs(d.roe, bm['roe'])}
ROA:              {_fmt(d.roa, '%', scale=100)}
EBITDA margin:    {_fmt(d.ebitda_margin, '%', scale=100)}

=== WZROST ===
Wzrost przychodów (YoY): {_fmt(d.revenue_growth_yoy, '%', scale=100)}
Wzrost zysków (YoY):     {_fmt(d.earnings_growth_yoy, '%', scale=100)}
EPS (TTM):               {_fmt(d.eps_ttm)}
EPS (forward):           {_fmt(d.eps_forward)}

=== KONDYCJA FINANSOWA ===
Debt/Equity:    {_fmt(d.debt_equity)}    [benchmark: {bm['debt_eq']}]{vs(bm['debt_eq'], d.debt_equity, True) if d.debt_equity else ''}
Current ratio:  {_fmt(d.current_ratio)}  [>1.5 = zdrowe]
Quick ratio:    {_fmt(d.quick_ratio)}    [>1.0 = zdrowe]
FCF (roczny):   {_fmt(d.fcf, ' ' + d.waluta, precision=0) if d.fcf else 'brak'}
FCF yield:      {_fmt(d.fcf_yield, '%', scale=100)}

=== DYWIDENDA ===
Stopa dywidendy:  {_fmt(d.dividend_yield, '%', scale=100)}
Payout ratio:     {_fmt(d.payout_ratio, '%', scale=100)}

Wydaj ocenę WYŁĄCZNIE w formacie JSON (bez markdown, bez komentarzy):
{{
  "signal": "BUY" | "SELL" | "HOLD" | "WATCH",
  "confidence": 0.0-1.0,
  "ocena_wyceny": "niedowartościowana" | "uczciwa" | "przewartościowana",
  "ocena_jakosci": "wysoka" | "średnia" | "niska",
  "horyzont": "krótkoterminowy" | "średnioterminowy" | "długoterminowy",
  "mocne_strony": ["punkt 1", "punkt 2"],
  "slabe_strony": ["punkt 1", "punkt 2"],
  "czynniki_ryzyka": ["ryzyko 1", "ryzyko 2"],
  "kluczowe_metryki": {{
    "najbardziej_atrakcyjne": "nazwa wskaźnika i wartość",
    "najbardziej_niepokojące": "nazwa wskaźnika i wartość"
  }},
  "reasoning": "ocena w 3 zdaniach: czy warto kupić, dlaczego i na jakim horyzoncie"
}}"""


# ──────────────────────────────────────────────
# 5. Główna funkcja agenta
# ──────────────────────────────────────────────

import ollama

MODEL = "llama3.2"

def analizuj(ticker: str) -> dict:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Analiza fundamentalna: {ticker}")

    dane = pobierz_dane(ticker)
    prompt = buduj_prompt(dane)

    odpowiedź = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0.1}
    )

    tekst = odpowiedź["message"]["content"].strip()
    if "```" in tekst:
        tekst = tekst.split("```")[1]
        if tekst.startswith("json"):
            tekst = tekst[4:]

    wynik = json.loads(tekst)
    wynik["ticker"] = ticker
    wynik["timestamp"] = datetime.now().isoformat()
    wynik["cena"] = dane.cena_aktualna
    wynik["dcf_intrinsic"] = dane.dcf_intrinsic_value
    wynik["margin_of_safety"] = dane.margin_of_safety
    wynik["dane_surowe"] = asdict(dane)

    return wynik


if __name__ == "__main__":
    for ticker in ["AAPL", "MSFT", "NVDA"]:
        try:
            w = analizuj(ticker)
            print(f"\n{'='*55}")
            print(f"  {w['ticker']} | {w['signal']} | pewność: {w['confidence']:.0%}")
            print(f"  Wycena: {w['ocena_wyceny']} | Jakość: {w['ocena_jakosci']}")
            if w.get("dcf_intrinsic"):
                mos = w.get("margin_of_safety", 0) * 100
                print(f"  DCF: {w['dcf_intrinsic']:.2f} | MoS: {mos:.1f}%")
            print(f"  {w['reasoning']}")
        except Exception as e:
            print(f"Błąd {ticker}: {e}")