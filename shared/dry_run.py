import os
import json
from datetime import datetime
from functools import wraps

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

def dry_run_guard(fn):
    """
    Dekorator – gdy DRY_RUN=true, loguje zamiast wykonywać.
    Użycie: @dry_run_guard nad każdą funkcją składającą zlecenie.
    """
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        if DRY_RUN:
            print(f"[DRY RUN] Pominięto: {fn.__name__}({args}, {kwargs})")
            _zapisz_dry_run_log(fn.__name__, args, kwargs)
            return {"sukces": False, "status": "DRY_RUN",
                    "info": "Tryb testowy – zlecenie nie zostało wysłane"}
        return await fn(*args, **kwargs)

    @wraps(fn)
    def sync_wrapper(*args, **kwargs):
        if DRY_RUN:
            print(f"[DRY RUN] Pominięto: {fn.__name__}")
            _zapisz_dry_run_log(fn.__name__, args, kwargs)
            return {"sukces": False, "status": "DRY_RUN",
                    "info": "Tryb testowy – zlecenie nie zostało wysłane"}
        return fn(*args, **kwargs)

    # Zwróć odpowiedni wrapper
    import asyncio
    if asyncio.iscoroutinefunction(fn):
        return wrapper
    return sync_wrapper


def _zapisz_dry_run_log(nazwa_funkcji: str, args, kwargs):
    """Zapisuje co BYŁOBY wykonane do pliku JSON."""
    wpis = {
        "timestamp": datetime.now().isoformat(),
        "funkcja": nazwa_funkcji,
        "args": str(args)[:300],
        "kwargs": str(kwargs)[:300],
    }
    with open("/app/data/dry_run_log.jsonl", "a") as f:
        f.write(json.dumps(wpis, ensure_ascii=False) + "\n")


def sprawdz_tryb():
    """Wywołaj na starcie każdego agenta – czytelny komunikat."""
    if DRY_RUN:
        print("=" * 50)
        print("  TRYB DRY RUN – tylko analiza, zero transakcji")
        print("  Aby wykonywać zlecenia: DRY_RUN=false w .env")
        print("=" * 50)
    else:
        tryb_ibkr = os.getenv("IBKR_TRADING_MODE", "paper").upper()
        print("=" * 50)
        print(f"  TRYB LIVE – zlecenia aktywne ({tryb_ibkr})")
        if tryb_ibkr == "LIVE":
            print("  *** UWAGA: PRAWDZIWY KAPITAŁ ***")
        print("=" * 50)