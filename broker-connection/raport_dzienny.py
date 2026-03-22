async def raport_dzienny():
    klient = KlientIBKR(cfg)
    await klient.polacz()
    portfel = await klient.pobierz_portfel()

    tekst = (
        f"RAPORT DZIENNY\n"
        f"Kapitał: {portfel['kapital_total']:.2f} USD\n"
        f"Gotówka: {portfel['cash']:.2f} USD\n"
        f"Pozycje: {len(portfel['pozycje'])}\n"
        f"Obrót dzienny: {_pobierz_dzienny_obrot(con):.2f} USD"
    )
    await wyslij_telegram(TOKEN, CHAT_ID, tekst)
    await klient.rozlacz()

schedule.every().day.at("17:30").do(lambda: asyncio.run(raport_dzienny()))