-- Win rate per action
SELECT action,
       COUNT(*) as decyzje,
       AVG(czy_trafiona) * 100 as win_rate_procent,
       AVG(zwrot_procent) as avg_zwrot
FROM decyzje d JOIN wyniki w ON d.id = w.decyzja_id
GROUP BY action;

-- Które consensus działa najlepiej?
SELECT consensus,
       COUNT(*) as n,
       AVG(zwrot_procent) as avg_zwrot
FROM decyzje d JOIN wyniki w ON d.id = w.decyzja_id
GROUP BY consensus ORDER BY avg_zwrot DESC;

-- Najgorsze decyzje (do analizy błędów)
SELECT d.ticker, d.action, d.reasoning, d.devil_advocate, w.zwrot_procent
FROM decyzje d JOIN wyniki w ON d.id = w.decyzja_id
ORDER BY w.zwrot_procent ASC LIMIT 10;