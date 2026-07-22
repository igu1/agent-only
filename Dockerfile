FROM python:3.12.3-slim
WORKDIR /app/agent
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/agent
RUN apt-get update && apt-get install -y --no-install-recommends curl gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd -m -u 10001 app && \
    mkdir -p /app/agent/logs && \
    mkdir -p /app/agent/mem && \
    chown -R app:app /app/agent
USER app

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 CMD curl -fsS http://localhost:8001/health || exit 1
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8001"]
