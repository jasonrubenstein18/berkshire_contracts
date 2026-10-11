FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL -o /tmp/caddy.tgz https://github.com/caddyserver/caddy/releases/download/v2.10.2/caddy_2.10.2_linux_amd64.tar.gz \
    && tar -xzf /tmp/caddy.tgz -C /usr/local/bin caddy \
    && rm /tmp/caddy.tgz \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/opt/hf \
    HF_HUB_CACHE=/opt/hf/hub \
    COLBERT_DEVICE=cpu \
    CADDY_REQUIRED=1 \
    PYTHONUNBUFFERED=1 \
    GRADIO_ANALYTICS_ENABLED=False

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY app.py ./
RUN uv sync --frozen --no-dev --no-group ocr \
    && python -c "from berkshire_contracts.retrieval.colbert import get_encoder; get_encoder()"

COPY Caddyfile ./
COPY data ./data
COPY eval ./eval

EXPOSE 8080
CMD ["python", "-m", "berkshire_contracts.serve"]
