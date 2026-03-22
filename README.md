# TRADE-AGENT – Local Multi-Agent Investment Analysis System

A fully local, privacy-first multi-agent system for investment analysis.
Runs entirely on your own hardware using open-source LLMs via Ollama.
No data is sent to external AI services.

## What it does

Five autonomous AI agents collaborate to analyze stocks and cryptocurrencies:

- **Technical agent** – RSI, MACD, Bollinger Bands, EMA crossovers
- **Fundamental agent** – P/E, P/B, ROE, DCF valuation, margin of safety
- **Sentiment agent** – reads public finance subreddits and news RSS feeds
- **Risk agent** – enforces position limits, stop-losses, drawdown limits
- **Orchestrator** – aggregates all signals, runs LLM deliberation, decides

The system supports three operating modes:

- `dry` – analysis only, zero transactions (default)
- `paper` – paper trading via Interactive Brokers demo account
- `live` – real trading (requires explicit confirmation)

## Tech stack

| Component     | Technology                      |
| ------------- | ------------------------------- |
| LLM (local)   | Ollama + Llama 3.2 / Qwen 2.5   |
| Orchestration | Python, asyncio, schedule       |
| Market data   | yfinance, feedparser, PRAW      |
| Broker API    | Interactive Brokers (ib_insync) |
| Database      | SQLite / PostgreSQL             |
| Cache         | Redis                           |
| Monitoring    | Grafana                         |
| Runtime       | Docker Compose                  |

## Project structure

```
TRADE-AGENT/
├── docker-compose.yml
├── start.sh                        # bash start.sh [dry|paper|live|stop]
├── shared/
│   └── dry_run.py                  # DRY_RUN flag shared across agents
├── technical-analysis-agent/
│   ├── technical_agent.py
│   ├── scheduler.py
│   └── requirements.txt
├── fundamental-analysis-agent/
│   └── fundamental_agent.py
├── sentiment-analysis-agent/
│   └── sentiment_agent.py          # reads Reddit + RSS, no posting
├── risk-management-agent/
│   └── risk_agent.py
├── orchestrator-agent/
│   └── orkiestrator.py
└── broker-connection/
    └── broker_ibkr.py
```

## Reddit API usage

The sentiment agent uses the Reddit API in **read-only** mode:

- Searches public posts by stock ticker symbol (e.g. "AAPL", "NVDA")
- Reads post titles and bodies from: r/stocks, r/investing, r/SecurityAnalysis
- Does **not** post, comment, vote, or interact with any user
- Runs at most once per hour per ticker
- All collected data stays on the local machine, never redistributed

## Quickstart

```bash
# 1. Copy and fill in credentials
cp .env.example .env

# 2. Start in analysis-only mode (no transactions)
bash start.sh dry

# 3. Check logs
docker compose logs -f orkiestrator
```

## Environment variables

See [`.env.example`](.env.example) for all required variables.
Credentials are never committed to this repository.

## Disclaimer

This tool is for personal research and educational purposes only.
It does not constitute financial advice. Always do your own research
before making investment decisions.

## License

MIT – free to use, modify, and distribute.
