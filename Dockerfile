# ============================================================================
# GroktoCrawl — ALL-IN-ONE single-image build (LITE edition)
# Repo: https://github.com/cshdotcom/groktocrawl-allinone (branch: lite)
#
# ONE container runs every component:
#   - agent-svc     :8080   Firecrawl-compatible API / job coordinator  (公开)
#   - mcp-svc       :8002   Model Context Protocol server (可选发布)
#   - scraper-svc   :8001   Five-tier scraper (Playwright + CloakBrowser)
#   - browser-svc   :8012   Headless browser session service
#   - parse-svc     :8013   PDF/Office document parsing
#   - llm-svc       :8011   Offline fixture LLM (zero-config demo)
#   - valkey        :6379   Embedded RESP cache/queue server (redis-server)
#   - monitor loop          每10分钟巡检 monitors（替代上游 ofelia 容器）
#
# LITE 与 full 的区别: 不含 SearXNG 搜索 / Qdrant 向量库 / semantic-svc
# (BGE-M3) / portal 门户 / 自主研究 Agent(/v2/agent,/v2/answer,/v2/search
# 等)。scrape/crawl/map/extract/parse/browser/MCP 全部可用。
#
# Internal components bind to 127.0.0.1 only. Only agent/mcp listen
# on 0.0.0.0 so the host publishes exactly what docker-compose.yml decides.
# Valkey can be swapped for an external one via EMBED_VALKEY switch.
# ============================================================================

# syntax=docker/dockerfile:1
FROM python:3.13-slim

ARG TARGETARCH=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
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

RUN pip install --no-cache-dir supervisor

# ── 2. Python services (editable installs mirror upstream Dockerfiles) ────
COPY agent-svc/pyproject.toml agent-svc/pyproject.toml
COPY browser-svc/pyproject.toml browser-svc/pyproject.toml
COPY parse-svc/pyproject.toml parse-svc/pyproject.toml
COPY llm-svc/pyproject.toml llm-svc/pyproject.toml
RUN pip install --no-cache-dir \
        -e ./agent-svc \
        -e ./browser-svc \
        -e ./parse-svc \
        -e ./llm-svc \
        "mcp>=1.27,<2"

# Scraper extras: curl_cffi readability markdownify cloakbrowser playwright
COPY scraper-svc/pyproject.toml scraper-svc/pyproject.toml
RUN pip install --no-cache-dir -e "./scraper-svc[playwright]"

# Headless Chromium shared by scraper-svc and browser-svc
RUN python -m playwright install --with-deps chromium

# parse-svc optional OCR enrichments (tolerant install mirrors upstream;
# tabula/camelot omitted — they pull Java/Ghostscript runtime deps)
RUN pip install --no-cache-dir pytesseract pdf2image 2>/dev/null || true


# ── 3. Application code ───────────────────────────────────────────────────
COPY common/ common/
COPY agent-svc/agent/ agent/
COPY scripts/reconcile-jobs.py scripts/reconcile-jobs.py
COPY scraper-svc/scraper/ scraper/
COPY scraper-svc/docker-entrypoint.sh /usr/local/bin/scraper-entrypoint
RUN chmod +x /usr/local/bin/scraper-entrypoint
COPY browser-svc/browser_svc/ browser_svc/
COPY llm-svc/llm_svc/ llm_svc/
# parse_svc 包体此前从未被拷入镜像 → uvicorn parse_svc.app:app 直接
# ModuleNotFoundError 崩溃循环(日志包实锤), 这里补上与其余服务一致的拷贝
COPY parse-svc/parse_svc/ parse_svc/
COPY mcp-svc/groktocrawl_client.py mcp-svc/browser_handler.py \
     mcp-svc/mcp_server.py mcp-svc/session_store.py mcp_app/

# CLI entry (docs use `./groktocrawl …`); deps already present above
COPY groktocrawl /usr/local/bin/groktocrawl
RUN chmod +x /usr/local/bin/groktocrawl

# ── 4. Orchestration assets ───────────────────────────────────────────────
COPY docker/aio/entrypoint.sh /usr/local/bin/groktocrawl-aio-entrypoint
COPY docker/aio/supervisord.conf /etc/supervisor/supervisord.conf
RUN chmod +x /usr/local/bin/groktocrawl-aio-entrypoint \
    && mkdir -p /etc/supervisor/conf.d /data/config /var/log/groktocrawl

# Public surface only: API 后台、MCP。内部组件一律 127.0.0.1。
EXPOSE 8080 8002

# supervisord as PID1: nodaemon + own zombie reaper + signal forwarding.
ENTRYPOINT ["/usr/local/bin/groktocrawl-aio-entrypoint"]
