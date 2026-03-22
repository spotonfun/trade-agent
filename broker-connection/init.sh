# Skopiuj credentiale do .env
echo "IBKR_USERNAME=twoj_login" >> .env
echo "IBKR_PASSWORD=twoje_haslo" >> .env

docker compose up -d tws-gateway

# Sprawdź czy gateway działa (po ~60 sekundach)
docker logs tws-gateway | tail -20
```

---

## Kompletny kod brokera

**`requirements.txt`** (dopisz):
```
ib_insync
python-telegram-bot
aiohttp