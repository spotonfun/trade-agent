import asyncio
import sqlite3
import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Literal

from ib_insync import IB, Stock, Crypto, Order, MarketOrder, \
    LimitOrder, StopOrder, BracketOrder, Contract, util

# ──────────────────────────────────────────────
# 1. Konfiguracja
# ──────────────────────────────────────────────

@dataclass
class KonfiguracjaBrokera:
    host: str = "localhost"
    port: int = 4002              # 4002 = paper, 4001 = live
    client_id: int = 1
    tryb: Literal["paper", "live"] = "paper"

    # Limity bezpieczeństwa (ostatnia linia obrony – niezależnie od agenta ryzyka)
    max_zlecenie_usd: float = 10_000.0   # max wartość jednego zlecenia
    max_dzienny_usd: float = 25_000.0    # max dzienny obrót
    dozwolone_tickery: set = field(default_factory=lambda: {
        "AAPL", "NVDA", "MSFT", "GOOGL", "AMZN",
        "JPM", "BRK-B", "SPY", "QQQ",            # duże, płynne spółki
        "BTC-USD", "ETH-USD",                     # krypto przez IBKR
    })
    # Pusty set = brak whitelist (wszystkie dozwolone) – NIE rób tego na live!

    def __post_init__(self):
        if self.tryb == "live" and self.port == 4002:
            raise ValueError("Live trading wymaga portu 4001, nie 4002!")


# ──────────────────────────────────────────────
# 2. Zlecenie do wykonania
# ──────────────────────────────────────────────

@dataclass
class ZlecenieBrokera:
    ticker: str
    akcja: Literal["BUY", "SELL"]
    ilosc: int                          # liczba akcji/jednostek
    typ_zlecenia: Literal["MKT", "LMT", "BRACKET"] = "LMT"
    limit_cena: float | None = None     # dla LMT i BRACKET
    stop_loss: float | None = None      # dla BRACKET
    take_profit: float | None = None    # dla BRACKET
    waluta: str = "USD"
    exchange: str = "SMART"             # SMART = IBKR automatycznie wybiera giełdę

    # Meta – skąd pochodzi zlecenie
    decyzja_id: int | None = None
    reasoning: str = ""


@dataclass
class WynikZlecenia:
    sukces: bool
    order_id: int | None
    status: str
    wypelniona_cena: float | None
    wypelniona_ilosc: int | None
    timestamp: str
    blad: str = ""
    dane_surowe: dict = field(default_factory=dict)


# ──────────────────────────────────────────────
# 3. Klient IBKR
# ──────────────────────────────────────────────

class KlientIBKR:
    def __init__(self, cfg: KonfiguracjaBrokera):
        self.cfg = cfg
        self.ib = IB()
        self._dzienny_obrot: float = 0.0
        self._con: sqlite3.Connection = _init_db_broker()

    # --- Połączenie ---

    async def polacz(self):
        await self.ib.connectAsync(
            self.cfg.host,
            self.cfg.port,
            clientId=self.cfg.client_id,
        )
        print(f"[BROKER] Połączono z IBKR "
              f"({'PAPER' if self.cfg.tryb == 'paper' else '*** LIVE ***'})")

        # Załaduj dzisiejszy obrót z bazy
        self._dzienny_obrot = _pobierz_dzienny_obrot(self._con)

    async def rozlacz(self):
        self.ib.disconnect()
        print("[BROKER] Rozłączono")

    # --- Kontrakt ---

    def _buduj_kontrakt(self, zlecenie: ZlecenieBrokera) -> Contract:
        ticker = zlecenie.ticker.replace("-USD", "")
        if zlecenie.ticker.endswith("-USD"):
            # Krypto przez IBKR
            from ib_insync import Crypto as IBCrypto
            return IBCrypto(ticker, "PAXOS", "USD")
        else:
            return Stock(ticker, zlecenie.exchange, zlecenie.waluta)

    # --- Sprawdzenia bezpieczeństwa (ostatnia bramka) ---

    def _sprawdz_whitelist(self, ticker: str) -> tuple[bool, str]:
        if not self.cfg.dozwolone_tickery:
            return True, ""
        if ticker not in self.cfg.dozwolone_tickery:
            return False, f"{ticker} nie jest na whitelist dozwolonych tickerów"
        return True, ""

    def _sprawdz_dzienny_limit(self, wartosc_usd: float) -> tuple[bool, str]:
        if self._dzienny_obrot + wartosc_usd > self.cfg.max_dzienny_usd:
            return False, (
                f"Dzienny limit {self.cfg.max_dzienny_usd:.0f} USD przekroczony "
                f"(obecny: {self._dzienny_obrot:.0f}, nowe: {wartosc_usd:.0f})"
            )
        return True, ""

    def _sprawdz_max_zlecenie(self, wartosc_usd: float) -> tuple[bool, str]:
        if wartosc_usd > self.cfg.max_zlecenie_usd:
            return False, (
                f"Wartość zlecenia {wartosc_usd:.0f} USD > "
                f"max {self.cfg.max_zlecenie_usd:.0f} USD"
            )
        return True, ""

    # --- Pobieranie ceny rynkowej ---

    async def pobierz_cene(self, kontrakt: Contract) -> float | None:
        self.ib.qualifyContracts(kontrakt)
        ticker_data = self.ib.reqMktData(kontrakt, "", False, False)
        await asyncio.sleep(2)  # poczekaj na dane

        cena = ticker_data.last or ticker_data.close or ticker_data.bid
        self.ib.cancelMktData(kontrakt)

        return float(cena) if cena and cena > 0 else None

    # --- Budowanie zleceń ---

    def _buduj_order(
        self,
        zlecenie: ZlecenieBrokera,
        cena_rynkowa: float | None,
    ) -> Order | list[Order]:
        """Zwraca Order lub listę [parent, sl, tp] dla BRACKET."""

        if zlecenie.typ_zlecenia == "MKT":
            return MarketOrder(zlecenie.akcja, zlecenie.ilosc)

        if zlecenie.typ_zlecenia == "LMT":
            # Limit ±0.1% od rynku – zapewnia wykonanie, ale chroni przed slippage
            if zlecenie.limit_cena:
                lmt = zlecenie.limit_cena
            elif cena_rynkowa:
                offset = 0.001 * cena_rynkowa
                lmt = (cena_rynkowa + offset if zlecenie.akcja == "BUY"
                       else cena_rynkowa - offset)
            else:
                return MarketOrder(zlecenie.akcja, zlecenie.ilosc)  # fallback
            return LimitOrder(zlecenie.akcja, zlecenie.ilosc, round(lmt, 2))

        if zlecenie.typ_zlecenia == "BRACKET":
            if not all([zlecenie.stop_loss, zlecenie.take_profit, cena_rynkowa]):
                raise ValueError("BRACKET wymaga stop_loss, take_profit i ceny")

            lmt = zlecenie.limit_cena or cena_rynkowa
            bracket = self.ib.bracketOrder(
                action=zlecenie.akcja,
                quantity=zlecenie.ilosc,
                limitPrice=round(lmt, 2),
                takeProfitPrice=round(zlecenie.take_profit, 2),
                stopLossPrice=round(zlecenie.stop_loss, 2),
            )
            return bracket  # zwraca listę [parent, takeProfit, stopLoss]

        raise ValueError(f"Nieznany typ zlecenia: {zlecenie.typ_zlecenia}")

    # --- Główna funkcja wykonania ---

    async def wykonaj_zlecenie(
        self,
        zlecenie: ZlecenieBrokera,
        timeout_sek: int = 30,
    ) -> WynikZlecenia:
        """
        Wykonuje zlecenie z pełną obsługą błędów i logowaniem.
        """
        ts = datetime.now(timezone.utc).isoformat()

        # 1. Buduj kontrakt i pobierz cenę
        try:
            kontrakt = self._buduj_kontrakt(zlecenie)
            self.ib.qualifyContracts(kontrakt)
            cena = await self.pobierz_cene(kontrakt)
            if not cena:
                return WynikZlecenia(
                    sukces=False, order_id=None,
                    status="ERROR", wypelniona_cena=None,
                    wypelniona_ilosc=None, timestamp=ts,
                    blad="Nie udało się pobrać ceny rynkowej",
                )
        except Exception as e:
            return WynikZlecenia(
                sukces=False, order_id=None,
                status="ERROR", wypelniona_cena=None,
                wypelniona_ilosc=None, timestamp=ts,
                blad=f"Błąd kontraktu: {e}",
            )

        wartosc_usd = cena * zlecenie.ilosc

        # 2. Ostateczne sprawdzenia bezpieczeństwa
        for ok, msg in [
            self._sprawdz_whitelist(zlecenie.ticker),
            self._sprawdz_max_zlecenie(wartosc_usd),
            self._sprawdz_dzienny_limit(wartosc_usd),
        ]:
            if not ok:
                _zapisz_log(self._con, zlecenie, "BLOCKED", None, None, msg, ts)
                return WynikZlecenia(
                    sukces=False, order_id=None,
                    status="BLOCKED", wypelniona_cena=None,
                    wypelniona_ilosc=None, timestamp=ts, blad=msg,
                )

        # 3. Złóż zlecenie
        try:
            orders = self._buduj_order(zlecenie, cena)

            if isinstance(orders, list):
                # BRACKET – składamy wszystkie powiązane zlecenia
                trades = []
                for order in orders:
                    trade = self.ib.placeOrder(kontrakt, order)
                    trades.append(trade)
                    await asyncio.sleep(0.1)
                parent_trade = trades[0]
            else:
                parent_trade = self.ib.placeOrder(kontrakt, orders)

        except Exception as e:
            return WynikZlecenia(
                sukces=False, order_id=None,
                status="ERROR", wypelniona_cena=None,
                wypelniona_ilosc=None, timestamp=ts,
                blad=f"Błąd składania zlecenia: {e}",
            )

        # 4. Czekaj na wypełnienie
        print(f"  [BROKER] Zlecenie złożone (orderId={parent_trade.order.orderId}), "
              f"czekam na wypełnienie...")

        deadline = asyncio.get_event_loop().time() + timeout_sek
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1)
            status = parent_trade.orderStatus.status

            if status == "Filled":
                wypelniona_cena = parent_trade.orderStatus.avgFillPrice
                wypelniona_ilosc = int(parent_trade.orderStatus.filled)
                self._dzienny_obrot += wypelniona_cena * wypelniona_ilosc

                _zapisz_log(
                    self._con, zlecenie, "FILLED",
                    wypelniona_cena, wypelniona_ilosc, "", ts,
                )
                print(f"  [BROKER] Wypełniono: {wypelniona_ilosc} szt. "
                      f"@ {wypelniona_cena:.2f}")

                return WynikZlecenia(
                    sukces=True,
                    order_id=parent_trade.order.orderId,
                    status="FILLED",
                    wypelniona_cena=wypelniona_cena,
                    wypelniona_ilosc=wypelniona_ilosc,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

            if status in ("Cancelled", "Inactive"):
                blad = f"Zlecenie anulowane przez IBKR: {status}"
                _zapisz_log(self._con, zlecenie, status, None, None, blad, ts)
                return WynikZlecenia(
                    sukces=False, order_id=parent_trade.order.orderId,
                    status=status, wypelniona_cena=None,
                    wypelniona_ilosc=None, timestamp=ts, blad=blad,
                )

        # Timeout – anuluj zlecenie
        self.ib.cancelOrder(parent_trade.order)
        blad = f"Timeout po {timeout_sek}s – zlecenie anulowane"
        _zapisz_log(self._con, zlecenie, "TIMEOUT", None, None, blad, ts)
        return WynikZlecenia(
            sukces=False, order_id=parent_trade.order.orderId,
            status="TIMEOUT", wypelniona_cena=None,
            wypelniona_ilosc=None, timestamp=ts, blad=blad,
        )

    # --- Pobieranie stanu konta ---

    async def pobierz_portfel(self) -> dict:
        """Zwraca aktualny stan konta z IBKR."""
        konto = self.ib.accountValues()
        pozycje = self.ib.positions()

        gotowka = next(
            (float(v.value) for v in konto
             if v.tag == "CashBalance" and v.currency == "USD"), 0.0
        )
        net_liquidation = next(
            (float(v.value) for v in konto
             if v.tag == "NetLiquidation" and v.currency == "USD"), 0.0
        )

        poz_dict = {}
        for p in pozycje:
            ticker = p.contract.symbol
            poz_dict[ticker] = {
                "ilosc": p.position,
                "srednia_cena": p.avgCost,
                "wartosc": p.position * p.avgCost,
                "sektor": "Unknown",  # yfinance uzupełni
            }

        return {
            "kapital_total": net_liquidation,
            "cash": gotowka,
            "pozycje": poz_dict,
            "timestamp": datetime.now().isoformat(),
        }


# ──────────────────────────────────────────────
# 4. Human-in-the-loop przez Telegram
# ──────────────────────────────────────────────

async def wyslij_telegram(token: str, chat_id: str, tekst: str):
    """Wysyła powiadomienie przez Telegram Bot API."""
    import aiohttp
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with aiohttp.ClientSession() as session:
        await session.post(url, json={
            "chat_id": chat_id,
            "text": tekst,
            "parse_mode": "HTML",
        })


async def czekaj_na_zatwierdzenie(
    zlecenie: ZlecenieBrokera,
    decyzja_reasoning: str,
    telegram_token: str,
    telegram_chat_id: str,
    timeout_min: int = 30,
) -> bool:
    """
    Wysyła alert Telegram i czeka na potwierdzenie przez plik-flagę.
    W prawdziwym systemie zastąp Telegram Botem z callbackiem.
    """
    flag_file = f"/tmp/approve_{zlecenie.ticker}_{zlecenie.akcja}.flag"
    reject_file = f"/tmp/reject_{zlecenie.ticker}_{zlecenie.akcja}.flag"

    # Usuń stare flagi
    for f in [flag_file, reject_file]:
        if os.path.exists(f):
            os.remove(f)

    tekst = (
        f"<b>SYGNAŁ INWESTYCYJNY</b>\n\n"
        f"Ticker: <b>{zlecenie.ticker}</b>\n"
        f"Akcja: <b>{zlecenie.akcja}</b>\n"
        f"Ilość: {zlecenie.ilosc} szt.\n"
        f"Stop-loss: {zlecenie.stop_loss}\n"
        f"Take-profit: {zlecenie.take_profit}\n\n"
        f"Uzasadnienie: {decyzja_reasoning}\n\n"
        f"Aby zatwierdzić:\n"
        f"<code>touch {flag_file}</code>\n"
        f"Aby odrzucić:\n"
        f"<code>touch {reject_file}</code>\n\n"
        f"Timeout: {timeout_min} minut"
    )

    await wyslij_telegram(telegram_token, telegram_chat_id, tekst)
    print(f"  [HUMAN] Alert wysłany. Czekam {timeout_min} min na odpowiedź...")

    # Czekaj na flagę
    import time
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        if os.path.exists(flag_file):
            os.remove(flag_file)
            print("  [HUMAN] Zatwierdzone!")
            return True
        if os.path.exists(reject_file):
            os.remove(reject_file)
            print("  [HUMAN] Odrzucone!")
            return False
        await asyncio.sleep(10)

    print("  [HUMAN] Timeout – automatycznie SKIP")
    return False


# ──────────────────────────────────────────────
# 5. Baza danych zleceń
# ──────────────────────────────────────────────

def _init_db_broker(sciezka: str = "zlecenia.db") -> sqlite3.Connection:
    con = sqlite3.connect(sciezka)
    con.execute("""
        CREATE TABLE IF NOT EXISTS zlecenia (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp      TEXT,
            ticker         TEXT,
            akcja          TEXT,
            ilosc          INTEGER,
            typ            TEXT,
            limit_cena     REAL,
            stop_loss      REAL,
            take_profit    REAL,
            status         TEXT,
            wypelniona_cena REAL,
            wypelniona_ilosc INTEGER,
            blad           TEXT,
            decyzja_id     INTEGER,
            wartosc_usd    REAL
        )
    """)
    con.commit()
    return con


def _zapisz_log(
    con: sqlite3.Connection,
    zlecenie: ZlecenieBrokera,
    status: str,
    cena: float | None,
    ilosc: int | None,
    blad: str,
    ts: str,
):
    wartosc = (cena or 0) * (ilosc or zlecenie.ilosc)
    con.execute("""
        INSERT INTO zlecenia
        (timestamp, ticker, akcja, ilosc, typ, limit_cena,
         stop_loss, take_profit, status, wypelniona_cena,
         wypelniona_ilosc, blad, decyzja_id, wartosc_usd)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ts, zlecenie.ticker, zlecenie.akcja, zlecenie.ilosc,
        zlecenie.typ_zlecenia, zlecenie.limit_cena,
        zlecenie.stop_loss, zlecenie.take_profit,
        status, cena, ilosc, blad, zlecenie.decyzja_id, wartosc,
    ))
    con.commit()


def _pobierz_dzienny_obrot(con: sqlite3.Connection) -> float:
    dzis = datetime.now().date().isoformat()
    row = con.execute("""
        SELECT COALESCE(SUM(wartosc_usd), 0)
        FROM zlecenia
        WHERE status='FILLED' AND timestamp LIKE ?
    """, (f"{dzis}%",)).fetchone()
    return float(row[0]) if row else 0.0


# ──────────────────────────────────────────────
# 6. Główna funkcja integracji z orkiestratorem
# ──────────────────────────────────────────────

async def wykonaj_decyzje_orkiestratora(
    decyzja: dict,            # wynik z orkiestrator.analizuj()
    kapital_portfela: float,
    cfg_broker: KonfiguracjaBrokera,
    human_approval: bool = True,
    telegram_token: str = "",
    telegram_chat_id: str = "",
) -> WynikZlecenia | None:
    """
    Łącznik między orkiestratorem a IBKR.
    Wywołuj tę funkcję zamiast bezpośrednio KlientIBKR.
    """

    # Tylko BUY i SELL trafiają do brokera
    if decyzja.get("action") not in ("BUY", "SELL"):
        print(f"  [BROKER] Pomijam {decyzja.get('action')} – brak zlecenia")
        return None

    # Przelicz % portfela → ilość akcji
    ticker = decyzja["ticker"]
    pozycja_procent = decyzja.get("pozycja_procent", 0)
    wartosc_usd = kapital_portfela * pozycja_procent / 100

    # Pobierz aktualną cenę (szybko przez yfinance)
    import yfinance as yf
    info = yf.Ticker(ticker).fast_info
    cena = float(info.get("lastPrice", 0))
    if cena <= 0:
        print(f"  [BROKER] Nie można pobrać ceny {ticker}")
        return None

    ilosc = max(1, int(wartosc_usd / cena))

    zlecenie = ZlecenieBrokera(
        ticker=ticker,
        akcja=decyzja["action"],
        ilosc=ilosc,
        typ_zlecenia="BRACKET" if (
            decyzja.get("stop_loss") and decyzja.get("take_profit")
        ) else "LMT",
        stop_loss=decyzja.get("stop_loss"),
        take_profit=decyzja.get("take_profit"),
        decyzja_id=decyzja.get("id"),
        reasoning=decyzja.get("reasoning", ""),
    )

    # Human-in-the-loop (Faza 1 i 2 mieszana)
    if human_approval and telegram_token:
        zatwierdzone = await czekaj_na_zatwierdzenie(
            zlecenie, decyzja.get("reasoning", ""),
            telegram_token, telegram_chat_id,
        )
        if not zatwierdzone:
            return None

    # Wykonaj przez IBKR
    klient = KlientIBKR(cfg_broker)
    try:
        await klient.polacz()
        wynik = await klient.wykonaj_zlecenie(zlecenie)

        if wynik.sukces and telegram_token:
            await wyslij_telegram(
                telegram_token, telegram_chat_id,
                f"WYKONANO: {zlecenie.akcja} {ticker} "
                f"{wynik.wypelniona_ilosc} szt. "
                f"@ {wynik.wypelniona_cena:.2f} USD"
            )

        return wynik

    finally:
        await klient.rozlacz()


# ──────────────────────────────────────────────
# 7. Paper trading – test całego systemu
# ──────────────────────────────────────────────

async def test_paper_trading():
    """Uruchom to najpierw – ZAWSZE na paper trading!"""

    cfg = KonfiguracjaBrokera(
        port=4002,           # paper
        tryb="paper",
        max_zlecenie_usd=5_000,
        max_dzienny_usd=15_000,
        dozwolone_tickery={"AAPL", "MSFT", "SPY"},
    )

    klient = KlientIBKR(cfg)
    await klient.polacz()

    # Test 1: pobierz stan portfela
    portfel = await klient.pobierz_portfel()
    print(f"\nStan konta paper:")
    print(f"  Kapitał: {portfel['kapital_total']:.2f} USD")
    print(f"  Gotówka: {portfel['cash']:.2f} USD")
    print(f"  Pozycje: {list(portfel['pozycje'].keys())}")

    # Test 2: złóż małe zlecenie testowe (1 akcja SPY)
    test_zlecenie = ZlecenieBrokera(
        ticker="SPY",
        akcja="BUY",
        ilosc=1,
        typ_zlecenia="LMT",
        reasoning="Test zlecenia paper trading",
    )

    print("\nTest zlecenia (1 akcja SPY)...")
    wynik = await klient.wykonaj_zlecenie(test_zlecenie, timeout_sek=15)
    print(f"  Status: {wynik.status}")
    print(f"  Cena: {wynik.wypelniona_cena}")
    print(f"  Błąd: {wynik.blad or 'brak'}")

    await klient.rozlacz()


if __name__ == "__main__":
    # Uruchom najpierw test na paper trading
    asyncio.run(test_paper_trading())