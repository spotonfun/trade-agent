FROM python:3.12-slim

WORKDIR /app

# Zależności systemowe (dla pandas-ta i newspaper3k)
RUN apt-get update && apt-get install -y \
    gcc g++ libffi-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY fundamental-analysis-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fundamental-analysis-agent/ .

# Healthcheck – sprawdza czy Ollama odpowiada
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -f ${OLLAMA_HOST}/api/tags || exit 1

CMD ["python", "fundamental_agent.py"]
