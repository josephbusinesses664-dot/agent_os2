# Agent OS — control plane image
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_RETRIES=5 \
    PIP_TIMEOUT=60

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY agentos ./agentos
COPY skills ./skills
COPY workflows ./workflows
COPY prompts ./prompts
COPY docker ./docker

RUN pip install --no-cache-dir --retries 5 --timeout 60 .

EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://localhost:8300/api/health || exit 1

CMD ["uvicorn", "agentos.api.server:app", "--host", "0.0.0.0", "--port", "8300"]