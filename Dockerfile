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

# ONE port for the whole stack, default 5021. The build arg bakes the default
# into the image; AGENT_PORT from .env / compose overrides it at run time, and
# uvicorn, the HEALTHCHECK and the compose port mapping all read that one value.
ARG AGENT_PORT=5021
ENV AGENT_PORT=${AGENT_PORT}
EXPOSE ${AGENT_PORT}
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 CMD curl -fsS "http://localhost:${AGENT_PORT}/health" || exit 1
# sh -c so the port can come from the environment; exec keeps uvicorn as PID 1
# so docker's SIGTERM reaches it directly (a clean stop, not a 10 s kill)
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port \"${AGENT_PORT:-5021}\""]
