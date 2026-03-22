-- Tworzony automatycznie przy pierwszym uruchomieniu PostgreSQL

CREATE TABLE IF NOT EXISTS decyzje (
    id              SERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    timestamp       TIMESTAMPTZ DEFAULT NOW(),
    action          TEXT,
    confidence      REAL,
    consensus       TEXT,
    pozycja_procent REAL,
    stop_loss       REAL,
    take_profit     REAL,
    cena_wejscia    REAL,
    reasoning       TEXT,
    devil_advocate  TEXT,
    dane_json       JSONB
);

CREATE TABLE IF NOT EXISTS wyniki (
    id                    SERIAL PRIMARY KEY,
    decyzja_id            INTEGER REFERENCES decyzje(id),
    cena_wyjscia          REAL,
    zwrot_procent         REAL,
    czy_trafiona          BOOLEAN,
    timestamp_zamkniecia  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS zlecenia (
    id               SERIAL PRIMARY KEY,
    timestamp        TIMESTAMPTZ DEFAULT NOW(),
    ticker           TEXT,
    akcja            TEXT,
    ilosc            INTEGER,
    typ              TEXT,
    status           TEXT,
    wypelniona_cena  REAL,
    wartosc_usd      REAL,
    blad             TEXT,
    decyzja_id       INTEGER REFERENCES decyzje(id)
);

CREATE TABLE IF NOT EXISTS audyt_ryzyka (
    id           SERIAL PRIMARY KEY,
    timestamp    TIMESTAMPTZ DEFAULT NOW(),
    ticker       TEXT,
    akcja_ryzyka TEXT,
    flagi        TEXT[],
    powod        TEXT,
    vix          REAL
);

-- Indeksy dla szybkich zapytań
CREATE INDEX IF NOT EXISTS idx_decyzje_ticker ON decyzje(ticker);
CREATE INDEX IF NOT EXISTS idx_decyzje_timestamp ON decyzje(timestamp);
CREATE INDEX IF NOT EXISTS idx_zlecenia_status ON zlecenia(status);