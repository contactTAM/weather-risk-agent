FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY config ./config
COPY data/hubs.yaml data/seed.sqlite.gz ./data/
COPY static ./static
COPY evals ./evals
# DB is restored from data/seed.sqlite.gz on first start (ephemeral disk is fine)
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
