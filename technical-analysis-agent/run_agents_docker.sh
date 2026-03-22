docker compose up -d ollama
docker exec -it <ollama-container> ollama pull llama3.2
docker compose up agent