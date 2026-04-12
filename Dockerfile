FROM python:3.12-slim

WORKDIR /app

# Dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source
COPY . .

# Cloud Run sets PORT env var
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn mcp_server.app:create_app --factory --host 0.0.0.0 --port ${PORT} --log-level info"]
