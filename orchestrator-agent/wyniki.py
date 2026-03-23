def zamknij_pozycje(con, decyzja_id: int, cena_wyjscia: float):
    """Uzupełnia wynik po zamknięciu pozycji."""
    row = con.execute(
        "SELECT cena_wejscia, action FROM decyzje WHERE id=?",
        (decyzja_id,)
    ).fetchone()

    if not row:
        return

    cena_wejscia, action = row
    zwrot = (cena_wyjscia - cena_wejscia) / cena_wejscia
    if action == "SELL":
        zwrot = -zwrot   # dla short liczymy odwrotnie

    czy_trafiona = 1 if zwrot > 0 else 0

    con.execute("""
        INSERT INTO wyniki
        (decyzja_id, cena_wyjscia, zwrot_procent, czy_trafiona, timestamp_zamkniecia)
        VALUES (?,?,?,?,?)
    """, (decyzja_id, cena_wyjscia, zwrot * 100,
          czy_trafiona, datetime.now().isoformat()))
    con.commit()
    print(f"Pozycja {decyzja_id}: zwrot {zwrot:.2%} | "
          f"{'TRAFIONA' if czy_trafiona else 'CHYBIONA'}")