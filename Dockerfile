# ============================================================================
# GroktoCrawl — ALL-IN-ONE single-image build
# Repo: https://github.com/cshdotcom/groktocrawl-allinone
#
# ONE container runs every component:
#   - agent-svc     :8080   Firecrawl-compatible API / job coordinator  (公开)
#   - portal-svc    :8081   Web portal UI (single search bar)           (公开)
#   - mcp-svc       :8002   Model Context Protocol server (可选发布)
#   - scraper-svc   :8001   Five-tier scraper (Playwright + CloakBrowser)
#   - browser-svc   :8012   Headless browser session service
#   - semantic-svc  :8003   BGE-M3 embeddings + rerank + vector index
#   - parse-svc     :8013   PDF/Office document parsing
#   - llm-svc       :8011   Offline fixture LLM (zero-config demo)
#   - searxng       :8888   Embedded metasearch engine (SearXNG, pip 安装)
#   - qdrant        :6333   Embedded vector database (官方静态二进制)
#   - valkey        :6379   Embedded RESP cache/queue server (redis-server)
#   - monitor loop          每10分钟巡检 monitors（替代上游 ofelia 容器）
#
# Internal components bind to 127.0.0.1 only. Only agent/portal/mcp listen
# on 0.0.0.0 so the host publishes exactly what docker-compose.yml decides.
# All embedded services can be swapped for external ones via EMBED_* switches.
# ============================================================================

# syntax=docker/dockerfile:1
FROM python:3.13-slim

ARG TARGETARCH=""
ARG QDRANT_VERSION=v1.18.2
ARG SEARXNG_GIT_URL=https://github.com/searxng/searxng.git

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    HF_HOME=/data/huggingface \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# ── 1. System packages ────────────────────────────────────────────────────
# redis-server      → embedded Valkey-compatible cache/queue (RESP protocol)
# poppler/tesseract → parse-svc PDF & OCR stack (+ Simplified Chinese OCR)
# libglib2.0/libgomp→ Chromium & PyTorch native deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git xz-utils procps bash \
        redis-server \
        poppler-utils \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-osd \
        libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# ── 2. Embedded SearXNG (metasearch, multilingual engines) ────────────────
# The PyPI package "searxng" is an unrelated placeholder; install from the
# official repository instead. static/** templates/** ship via package_data.
# SearXNG's setup.py imports searx/__init__.py (needs msgspec…), so deps are
# installed first and the wheel is built WITHOUT build isolation.
RUN git clone --depth 1 ${SEARXNG_GIT_URL} /tmp/searxng-src \
    && pip install --no-cache-dir "setuptools>=75" wheel \
    && pip install --no-cache-dir -r /tmp/searxng-src/requirements.txt \
    && pip install --no-cache-dir --no-deps --no-build-isolation /tmp/searxng-src \
    && rm -rf /tmp/searxng-src

# Gunicorn serves the SearXNG WSGI app inside this container.
RUN pip install --no-cache-dir "gunicorn>=22.0,<24" supervisor

# ── 3. Python services (editable installs mirror upstream Dockerfiles) ────
COPY agent-svc/pyproject.toml agent-svc/pyproject.toml
COPY browser-svc/pyproject.toml browser-svc/pyproject.toml
COPY parse-svc/pyproject.toml parse-svc/pyproject.toml
COPY portal-svc/pyproject.toml portal-svc/pyproject.toml
COPY llm-svc/pyproject.toml llm-svc/pyproject.toml
RUN pip install --no-cache-dir \
        -e ./agent-svc \
        -e ./browser-svc \
        -e ./parse-svc \
        -e ./portal-svc \
        -e ./llm-svc \
        "mcp>=1.27,<2"

# Scraper extras: curl_cffi readability markdownify cloakbrowser playwright
COPY scraper-svc/pyproject.toml scraper-svc/pyproject.toml
RUN pip install --no-cache-dir -e "./scraper-svc[playwright]"

COPY semantic-svc/pyproject.toml semantic-svc/pyproject.toml
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -e ./semantic-svc

# Headless Chromium shared by scraper-svc and browser-svc
RUN python -m playwright install --with-deps chromium

# parse-svc optional OCR enrichments (tolerant install mirrors upstream;
# tabula/camelot omitted — they pull Java/Ghostscript runtime deps)
RUN pip install --no-cache-dir pytesseract pdf2image 2>/dev/null || true

# ── 4. Embedded Qdrant (official musl static binary) ──────────────────────
RUN set -eux; \
    arch="${TARGETARCH:-$(uname -m)}"; \
    case "$arch" in \
        amd64|x86_64)  qarch="x86_64" ;; \
        arm64|aarch64) qarch="aarch64" ;; \
        *) echo "unsupported TARGETARCH=$arch" >&2; exit 1 ;; \
    esac; \
    mkdir -p /opt/qdrant; \
    curl -fsSL "https://github.com/qdrant/qdrant/releases/download/${QDRANT_VERSION}/qdrant-${qarch}-unknown-linux-musl.tar.gz" \
        | tar xz -C /opt/qdrant; \
    chmod +x /opt/qdrant/qdrant; \
    /opt/qdrant/qdrant --version | head -n 1

# ── 5. Application code ───────────────────────────────────────────────────
COPY common/ common/
COPY agent-svc/agent/ agent/
COPY scripts/reconcile-jobs.py scripts/reconcile-jobs.py
COPY scraper-svc/scraper/ scraper/
COPY scraper-svc/docker-entrypoint.sh /usr/local/bin/scraper-entrypoint
RUN chmod +x /usr/local/bin/scraper-entrypoint
COPY browser-svc/browser_svc/ browser_svc/
COPY portal-svc/portal/ portal/
COPY llm-svc/llm_svc/ llm_svc/
# semantic-svc keeps its upstream flat-module layout (models/auth/metrics…)
# resolved from cwd=/app exactly like the original per-service image.
COPY semantic-svc/app.py semantic-svc/auth.py semantic-svc/models.py \
     semantic-svc/retention.py semantic-svc/router_index.py \
     semantic-svc/router_migration.py semantic-svc/router_search.py \
     semantic-svc/metrics.py ./
COPY mcp-svc/groktocrawl_client.py mcp-svc/browser_handler.py \
     mcp-svc/mcp_server.py mcp-svc/session_store.py mcp_app/

# CLI entry (docs use `./groktocrawl …`); deps already present above
COPY groktocrawl /usr/local/bin/groktocrawl
RUN chmod +x /usr/local/bin/groktocrawl

# ── 6. Orchestration assets ───────────────────────────────────────────────
COPY docker/aio/entrypoint.sh /usr/local/bin/groktocrawl-aio-entrypoint
COPY docker/aio/supervisord.conf /etc/supervisor/supervisord.conf
RUN chmod +x /usr/local/bin/groktocrawl-aio-entrypoint \
    && mkdir -p /etc/supervisor/conf.d /data/config /etc/searxng /var/log/groktocrawl

# Public surface only: API 后台、门户 UI、MCP。内部组件一律 127.0.0.1。
EXPOSE 8080 8081 8002

# supervisord as PID1: nodaemon + own zombie reaper + signal forwarding.
ENTRYPOINT ["/usr/local/bin/groktocrawl-aio-entrypoint"]
