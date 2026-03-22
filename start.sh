#!/bin/bash
# ═══════════════════════════════════════════════
# TRADE-AGENT – skrypt startowy
# Użycie:
#   bash start.sh                → dry-run (domyślnie)
#   bash start.sh dry            → tryb testowy
#   bash start.sh paper          → paper trading (IBKR demo)
#   bash start.sh live           → live trading (prawdziwy kapitał!)
#   bash start.sh stop           → zatrzymaj wszystko
#   bash start.sh logs           → logi orkiestratora
#   bash start.sh status         → status wszystkich serwisów
# ═══════════════════════════════════════════════

set -e

# ── Kolory do komunikatów ───────────────────────
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLU='\033[0;34m'
NC='\033[0m'  # reset

TRYB="${1:-dry}"   # domyślnie dry jeśli brak argumentu

# ── Pomocnicze funkcje ──────────────────────────

info()    { echo -e "${BLU}[INFO]${NC} $1"; }
ok()      { echo -e "${GRN}[ OK ]${NC} $1"; }
warn()    { echo -e "${YLW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERR ]${NC} $1"; exit 1; }
separator(){ echo -e "${BLU}══════════════════════════════════════${NC}"; }

sprawdz_env() {
    if [ ! -f .env ]; then
        error "Brak pliku .env – skopiuj .env.example i uzupełnij:\n  cp .env.example .env && nano .env"
    fi
    ok ".env istnieje"
}

sprawdz_docker() {
    if ! docker info &>/dev/null; then
        error "Docker nie działa – uruchom Docker Desktop lub dockerd"
    fi
    ok "Docker działa"
}

czekaj_na_serwis() {
    local nazwa="$1"
    local sekundy="${2:-30}"
    info "Czekam na serwis: $nazwa (max ${sekundy}s)..."
    local i=0
    while [ $i -lt $sekundy ]; do
        if docker compose ps "$nazwa" 2>/dev/null | grep -q "healthy"; then
            ok "$nazwa gotowy"
            return 0
        fi
        sleep 5
        i=$((i + 5))
        echo -n "."
    done
    echo ""
    warn "$nazwa nie odpowiada po ${sekundy}s – sprawdź: docker compose logs $nazwa"
}

pobierz_model_ollama() {
    local model
    model=$(grep OLLAMA_MODEL .env | cut -d= -f2 | tr -d '"' | tr -d "'" || echo "llama3.2")
    info "Sprawdzam model Ollama: $model"
    if docker compose exec -T ollama ollama list 2>/dev/null | grep -q "$model"; then
        ok "Model $model już pobrany"
    else
        info "Pobieram model $model (może chwilę potrwać)..."
        docker compose exec -T ollama ollama pull "$model"
        ok "Model $model pobrany"
    fi
}

# ── Infrastruktura wspólna dla wszystkich trybów ─

start_infrastruktura() {
    info "Uruchamiam infrastrukturę (postgres, redis, ollama)..."
    docker compose up -d postgres redis ollama
    czekaj_na_serwis postgres 60
    czekaj_na_serwis redis    30
    czekaj_na_serwis ollama   90
    pobierz_model_ollama
}

start_agenci() {
    info "Uruchamiam agentów analizy..."
    docker compose up -d \
        agent-techniczny \
        agent-fundamentalny \
        agent-sentyment \
        agent-ryzyko
    ok "Agenci uruchomieni"
}

# ══════════════════════════════════════════════
# TRYBY
# ══════════════════════════════════════════════

cmd_dry() {
    separator
    echo -e "${GRN}  TRYB: DRY RUN – tylko analiza, zero transakcji${NC}"
    separator

    sprawdz_env
    sprawdz_docker

    # Ustaw zmienne dla tego trybu
    export DRY_RUN=true
    export IBKR_TRADING_MODE=paper

    # Nadpisz w .env tymczasowo przez plik override
    cat > .env.override << EOF
DRY_RUN=true
IBKR_TRADING_MODE=paper
EOF

    start_infrastruktura
    start_agenci

    info "Uruchamiam orkiestratora (bez brokera i TWS)..."
    docker compose \
        --env-file .env \
        --env-file .env.override \
        up -d orkiestrator

    rm -f .env.override

    separator
    ok "System uruchomiony w trybie DRY RUN"
    echo ""
    echo "  Logi:    docker compose logs -f orkiestrator"
    echo "  Status:  bash start.sh status"
    echo "  Stop:    bash start.sh stop"
    separator
}

cmd_paper() {
    separator
    echo -e "${YLW}  TRYB: PAPER TRADING – konto demo IBKR${NC}"
    separator

    sprawdz_env
    sprawdz_docker

    # Sprawdź czy są credentiale IBKR
    if ! grep -q "IBKR_USERNAME=" .env || grep -q "IBKR_USERNAME=twoj" .env; then
        error "Uzupełnij IBKR_USERNAME i IBKR_PASSWORD w .env\n  Konto demo: ibkr.com → Paper Trading Account"
    fi
    if ! grep -q "TELEGRAM_BOT_TOKEN=" .env || grep -q "TELEGRAM_BOT_TOKEN=1234" .env; then
        warn "Brak TELEGRAM_BOT_TOKEN – human-in-the-loop nie będzie działał"
    fi

    cat > .env.override << EOF
DRY_RUN=false
IBKR_TRADING_MODE=paper
IBKR_PORT=4002
EOF

    start_infrastruktura

    info "Uruchamiam TWS Gateway (IBKR paper)..."
    docker compose --env-file .env --env-file .env.override up -d tws-gateway
    czekaj_na_serwis tws-gateway 150

    start_agenci

    info "Uruchamiam orkiestratora i brokera..."
    docker compose \
        --env-file .env \
        --env-file .env.override \
        --profile paper \
        up -d orkiestrator broker

    rm -f .env.override

    separator
    ok "System uruchomiony w trybie PAPER TRADING"
    echo ""
    echo "  TWS VNC:  localhost:5900  (podgląd GUI)"
    echo "  Logi:     docker compose logs -f orkiestrator broker"
    echo "  Stop:     bash start.sh stop"
    separator
}

cmd_live() {
    separator
    echo -e "${RED}  TRYB: LIVE TRADING – PRAWDZIWY KAPITAŁ!${NC}"
    separator

    sprawdz_env
    sprawdz_docker

    # Podwójne potwierdzenie dla live
    echo -e "${RED}  UWAGA: Zlecenia będą wykonywane na PRAWDZIWYM koncie IBKR!${NC}"
    echo -e "${RED}  Upewnij się że:${NC}"
    echo "    1. Paper trading działał stabilnie przez min. 3 miesiące"
    echo "    2. Sharpe ratio > 1.0, max drawdown < 10%"
    echo "    3. Agent ryzyka ma ustawione właściwe limity"
    echo ""
    read -r -p "  Wpisz 'ROZUMIEM' aby kontynuować: " potwierdzenie
    if [ "$potwierdzenie" != "ROZUMIEM" ]; then
        info "Anulowano – dobra decyzja jeśli masz wątpliwości"
        exit 0
    fi

    echo ""
    read -r -p "  Podaj maksymalny dzienny limit strat w USD (np. 200): " limit
    if ! [[ "$limit" =~ ^[0-9]+$ ]]; then
        error "Nieprawidłowy limit – podaj liczbę"
    fi

    # Sprawdź credentiale
    if grep -q "IBKR_USERNAME=twoj" .env; then
        error "Uzupełnij prawdziwe dane IBKR w .env"
    fi
    if grep -q "TELEGRAM_BOT_TOKEN=1234" .env; then
        error "Telegram wymagany dla live trading – uzupełnij TELEGRAM_BOT_TOKEN"
    fi

    cat > .env.override << EOF
DRY_RUN=false
IBKR_TRADING_MODE=live
IBKR_PORT=4001
MAX_DZIENNY_USD=$limit
EOF

    start_infrastruktura

    info "Uruchamiam TWS Gateway (IBKR LIVE)..."
    docker compose --env-file .env --env-file .env.override up -d tws-gateway
    czekaj_na_serwis tws-gateway 150

    start_agenci

    info "Uruchamiam orkiestratora i brokera (LIVE)..."
    docker compose \
        --env-file .env \
        --env-file .env.override \
        --profile live \
        up -d orkiestrator broker

    rm -f .env.override

    separator
    ok "System uruchomiony w trybie LIVE"
    echo -e "${RED}  Kill switch: touch /tmp/KILL_SWITCH${NC}"
    echo "  Logi:       docker compose logs -f orkiestrator broker"
    separator
}

cmd_stop() {
    separator
    info "Zatrzymuję wszystkie serwisy..."
    touch /tmp/KILL_SWITCH          # sygnał dla agentów Python
    docker compose --profile paper --profile live down
    rm -f /tmp/KILL_SWITCH
    ok "Wszystko zatrzymane"
    separator
}

cmd_logs() {
    local serwis="${2:-orkiestrator}"
    docker compose logs -f --tail=100 "$serwis"
}

cmd_status() {
    separator
    echo "  Status serwisów:"
    separator
    docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
    separator
    echo ""
    echo "  Logi ostatnich 5 decyzji (jeśli baza działa):"
    docker compose exec -T postgres psql \
        -U "$(grep POSTGRES_USER .env | cut -d= -f2)" \
        -d "$(grep POSTGRES_DB   .env | cut -d= -f2)" \
        -c "SELECT ticker, action, confidence, consensus, timestamp
            FROM decyzje ORDER BY timestamp DESC LIMIT 5;" \
        2>/dev/null || warn "Baza niedostępna"
}

# ══════════════════════════════════════════════
# ROUTER – wybór trybu
# ══════════════════════════════════════════════

case "$TRYB" in
    dry   | d)  cmd_dry   ;;
    paper | p)  cmd_paper ;;
    live  | l)  cmd_live  ;;
    stop  | s)  cmd_stop  ;;
    logs)       cmd_logs "$@" ;;
    status)     cmd_status    ;;
    *)
        echo "Użycie: bash start.sh [dry|paper|live|stop|logs|status]"
        echo ""
        echo "  dry    – tylko analiza, zero transakcji (domyślnie)"
        echo "  paper  – paper trading na koncie demo IBKR"
        echo "  live   – live trading (wymaga potwierdzenia)"
        echo "  stop   – zatrzymaj wszystko"
        echo "  logs   – logi orkiestratora (lub: logs broker)"
        echo "  status – status serwisów + ostatnie decyzje"
        exit 1
        ;;
esac