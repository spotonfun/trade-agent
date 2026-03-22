## Kilka ważnych uwag praktycznych

Jakość danych z yfinance jest nierówna – dla dużych spółek US działa świetnie, dla GPW (np. CDR.WA) bywa niekompletna. Dla polskiej giełdy rozważ Stooq API lub Biznesradar.

DCF jest bardzo czuły na założenia. Mała zmiana wacc lub stopy wzrostu radykalnie zmienia wynik. Traktuj go jako jeden z sygnałów, nie wyrocznię – dlatego agent przekazuje margin_of_safety do orkiestratora, który waży go razem z innymi agentami.

Dane kwartalne (earnings history, quarterly FCF) możesz pobrać przez spółka.quarterly_financials i spółka.quarterly_cashflow z yfinance – warto dodać trend kwartalny do promptu przy kolejnej iteracji.
