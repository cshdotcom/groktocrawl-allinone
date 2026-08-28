#!/usr/bin/env bash
# ============================================================================
# GroktoCrawl all-in-one entrypoint
#
# Responsibilities:
#   1. Resolve EMBED_* toggles (auto mode: start an embedded component only
#      when no external URL has been configured for it).
#   2. Generate /etc/searxng/settings.yml (private instance, JSON API on,
#      multilingual relevance defaults, optional engine disable list).
#   3. Emit supervisor program files for every component that should run.
#   4. exec supervisord -n  (PID1 handles signals + zombie reaping).
#
# Every upstream env var passes through unchanged — extra services pick their
# own settings up from the environment (docker-compose env_file: .env).
# ============================================================================
set -euo pipefail

LOGDIR=/var/log/groktocrawl
DATA_ROOT="${GROKTOCRAWL_DATA:-/data}"
CONF_DIR=/etc/supervisor/conf.d

mkdir -p "$DATA_ROOT/valkey" "$DATA_ROOT/qdrant" "$DATA_ROOT/huggingface" \
         "$DATA_ROOT/cloakbrowser" "$DATA_ROOT/config" \
         "$LOGDIR" "$CONF_DIR"

log() { printf '[groktocrawl-aio] %s\n' "$*" >&2; }

# ── Helper: authority of a host:port part ──────────────────────────────────
url_host() {
    # http://127.0.0.1:8888 -> 127.0.0.1 ; empty input -> empty output
    python3 - "$1" <<'PYEOF'
import sys, urllib.parse as u
try:
    print(u.urlsplit(sys.argv[1]).hostname or "")
except Exception:
    print("")
PYEOF
}

is_local_url() {
    local h; h="$(url_host "${1:-}")"
    case "$h" in
        ""|127.0.0.1|localhost|::1|0.0.0.0) return 0 ;;
        *) return 1 ;;
    esac
}

# ── 旧多镜像遗留 URL 清洗(防呆) ──────────────────────────────────────
# 旧版多镜像 compose 通过 valkey/slopsearx/searxng/scraper-svc 等容器名互联;
# 单容器内这些主机名不存在 → 搜索空结果 / 缓存静默禁用 / analytics 错误刷屏。
# auto 模式下检测到这类遗留主机名时自动忽略并回退内嵌默认值。
# 需要真正的外部组件: 用 IP 或域名(不会被清洗), 或显式设 EMBED_*=false。
embed_off() { case "${1:-auto}" in false|False|no|No|0) return 0 ;; *) return 1 ;; esac; }
LEGACY_HOSTS='valkey|redis|searxng|slopsearx|qdrant|scraper-svc|scraper|browser-svc|semantic-svc|parse-svc|agent-svc|portal-svc|mcp-svc|llm-svc|flare-solverr'
scrub_legacy_url() {
    local var="$1" val host
    val="${!var:-}"
    [ -z "$val" ] && return 0
    host="$(url_host "$val")"
    case "$host" in
        ""|127.0.0.1|localhost|::1|0.0.0.0) return 0 ;;
    esac
    if printf '%s\n' "$host" | grep -qE "^(${LEGACY_HOSTS})$"; then
        log "WARNING: ${var}=${val} 指向旧多镜像容器名 '${host}' — 单容器内不可达, 已忽略并回退内嵌默认值 (真实外部地址请用 IP/域名, 或显式设 EMBED_*=false)"
        unset "${var}"
    fi
}
embed_off "${EMBED_VALKEY:-auto}"  || scrub_legacy_url VALKEY_URL
embed_off "${EMBED_QDRANT:-auto}"  || scrub_legacy_url QDRANT_URL
embed_off "${EMBED_SEARXNG:-auto}" || scrub_legacy_url SEARXNG_URL
scrub_legacy_url SCRAPER_URL
scrub_legacy_url BROWSER_SVC_URL
scrub_legacy_url SEMANTIC_URL
scrub_legacy_url LLM_BASE_URL

# ── Resolve embedded toggles ───────────────────────────────────────────────
VALKEY_HOST="${VALKEY_HOST:-127.0.0.1}"
VALKEY_PORT="${VALKEY_PORT:-6379}"
VALKEY_DB="${VALKEY_DB:-0}"
export VALKEY_URL="${VALKEY_URL:-redis://${VALKEY_HOST}:${VALKEY_PORT}/${VALKEY_DB}}"

export SCRAPER_URL="${SCRAPER_URL:-http://127.0.0.1:8001}"
export BROWSER_SVC_URL="${BROWSER_SVC_URL:-http://127.0.0.1:8012}"
export SEMANTIC_URL="${SEMANTIC_URL:-http://127.0.0.1:8003}"
export QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
export SEARXNG_URL="${SEARXNG_URL:-http://127.0.0.1:8888}"
export LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:8011/v1}"
export AGENT_BASE_URL="${AGENT_BASE_URL:-http://127.0.0.1:8080}"
export GROKTOCRAWL_URL="${GROKTOCRAWL_URL:-http://127.0.0.1:8080}"

embed_enabled() {
    # $1 toggle name   $2 auto-condition command ("true" = embed)
    local toggle="$1"
    case "${toggle}" in
        false|False|no|No|0) return 1 ;;
        true|True|yes|Yes|1) return 0 ;;
        *)                                   # auto
            [ "$2" = "true" ]
            ;;
    esac
}

if embed_enabled "${EMBED_VALKEY:-auto}" "$(is_local_url "${VALKEY_URL}" && echo true || echo false)"; then
    START_VALKEY=true
else
    START_VALKEY=false
fi
if embed_enabled "${EMBED_QDRANT:-auto}" "$(is_local_url "${QDRANT_URL}" && echo true || echo false)"; then
    START_QDRANT=true
else
    START_QDRANT=false
fi
if embed_enabled "${EMBED_SEARXNG:-auto}" "$(is_local_url "${SEARXNG_URL}" && echo true || echo false)"; then
    START_SEARXNG=true
else
    START_SEARXNG=false
fi
# LLM fixture: only meaningful for zero-config demos. Start when no external
# OpenAI-compatible endpoint was given and it is not explicitly disabled.
START_LLM_FIXTURE=false
if embed_enabled "${EMBED_LLM_FIXTURE:-auto}" "$(is_local_url "${LLM_BASE_URL}" && echo true || echo false)"; then
    if [ -z "${LLM_API_KEY:-}" ]; then
        START_LLM_FIXTURE=true
    fi
fi

START_MONITORS=false
case "${MONITOR_SCHEDULER_ENABLED:-true}" in
    true|True|yes|Yes|1) START_MONITORS=true ;;
esac

log "embedded components: valkey=${START_VALKEY} qdrant=${START_QDRANT} searxng=${START_SEARXNG} llm-fixture=${START_LLM_FIXTURE} monitor-loop=${START_MONITORS}"

# ── SearXNG settings generation ────────────────────────────────────────────
S_WORKERS="${SEARXNG_WORKERS:-2}"
S_THREADS="${SEARXNG_THREADS:-12}"
if [ "${START_SEARXNG}" = "true" ]; then
    mkdir -p /etc/searxng
    # 用户 bind-mount 了 /etc/searxng 或 settings.yml 时跳过生成, 避免每次启动覆盖用户配置
    if grep -qsE " /etc/searxng(/settings\.yml)? " /proc/mounts; then
        log "bind-mounted searxng settings detected -- skip auto-generation (user-managed)"
    else
        SECRET_FILE="$DATA_ROOT/config/searxng_secret_key"
        if [ ! -s "$SECRET_FILE" ]; then
            python3 -c 'import secrets,sys; open(sys.argv[1],"w").write(secrets.token_hex(32))' "$SECRET_FILE"
            chmod 600 "$SECRET_FILE"
        fi
        S_KEY=$(cat "$SECRET_FILE")

        python3 - "$S_KEY" <<'PYEOF'
import os
import sys

key = sys.argv[1]

# ── 引擎白名单 ───────────────────────────────────────────────
# SEARXNG_ENGINES 语义:
#   ""(空)   → 使用下面的中国大陆直连可达精选集(默认)
#   "all"    → 保留 SearXNG 全部默认引擎(海外服务器/自备代理场景)
#   "a,b,c"  → 精确使用这些引擎(名字须存在于 SearXNG 默认注册表)
# 白名单中的引擎一律强制启用(disabled:false), 因此默认关闭的引擎
# (crossref/bilibili/microsoft learn/chinaso news/sina 等)同样会生效。
CHINA_DEFAULT_ENGINES = [
    # 通用网页与资讯(bing: 大陆可达的唯一国际主流全网索引)
    "bing", "bing news", "bing images", "bing videos",
    # 开发者 / IT
    "github", "mdn", "microsoft learn", "stackoverflow",
    # 学术(零广告, 直连可达)
    "arxiv", "crossref", "semantic scholar", "pubmed",
    # 中文垂直源(bilibili/国家搜索新闻/新浪新闻)
    "bilibili", "chinaso news", "sina",
]

engines_env = os.getenv("SEARXNG_ENGINES", "").strip()
disabled = [e.strip() for e in os.getenv("SEARXNG_DISABLED_ENGINES", "").split(",") if e.strip()]
proxy = os.getenv("SEARXNG_OUTGOING_PROXY", "").strip()

if engines_env == "all":
    keep_only = []
elif engines_env:
    keep_only = [e.strip() for e in engines_env.split(",") if e.strip()]
else:
    keep_only = list(CHINA_DEFAULT_ENGINES)

lines = [
    "# AUTO-GENERATED by groktocrawl-aio-entrypoint — do not edit here.",
    "# 完全自定义: bind-mount 覆盖 /etc/searxng/settings.yml (检测到挂载即跳过生成),",
    "# 或在 .env 用 SEARXNG_ENGINES / SEARXNG_DISABLED_ENGINES / SEARXNG_OUTGOING_PROXY 控制。",
    "",
]

if keep_only or disabled:
    lines.append("use_default_settings:")
    lines.append("  engines:")
    if keep_only:
        lines.append("    keep_only:")
        lines.extend(f'      - "{e}"' for e in keep_only)
    if disabled:
        lines.append("    remove:")
        lines.extend(f"      - {e}" for e in disabled)
else:
    lines.append("use_default_settings: true")

if keep_only:
    lines.append("")
    lines.append("engines:")
    for e in keep_only:
        lines.append(f'  - name: "{e}"')
        lines.append("    disabled: false")

lines += [
    "",
    "server:",
    f"  secret_key: '{key}'",
    "  limiter: false          # private single-user instance: no bot-detection throttle",
    "  image_proxy: true",
    "  public_instance: false",
    "",
    "search:",
    "  safe_search: 1         # moderate: 过滤不适宜内容(0=off 1=moderate 2=strict)",
    "  autocomplete: ''       # 大陆直连 google suggest 不可达, 关闭自动补全",
    "  default_lang: 'auto'    # per-query language autodetect → high CJK relevance",
    "  formats: [html, json, csv, rss]",
    "",
    "ui:",
    "  static_use_hash: true",
]

# 可选出站代理: 仅作用于 SearXNG 引擎出站请求(httpx), 不影响容器内服务互联
if proxy:
    lines += [
        "",
        "outgoing:",
        "  proxies:",
        "    all://:",
        f"      - {proxy}",
    ]

open("/etc/searxng/settings.yml", "w").write("\n".join(lines) + "\n")
PYEOF
        log "generated /etc/searxng/settings.yml"
    fi
fi

# ── Supervisor program emission helpers ────────────────────────────────────
emit_program() { # name priority stdout_logfile command...
    local name="$1" prio="$2" logfile="$3"; shift 3
    {
        echo "[program:${name}]"
        echo "command=${*}"
        echo "directory=/app"
        echo "priority=${prio}"
        echo "autostart=true"
        echo "autorestart=unexpected"
        echo "exitcodes=0"
        echo "startsecs=5"
        echo "startretries=15"
        echo "stopsignal=TERM"
        echo "stopasgroup=true"
        echo "killasgroup=true"
        echo "stopwaitsecs=25"
        echo "redirect_stderr=true"
        echo "stdout_logfile=${logfile}"
        echo "stdout_logfile_maxbytes=20MB"
        echo "stdout_logfile_backups=3"
        echo "environment=PYTHONPATH=\"/app\",PYTHONUNBUFFERED=\"1\""
    } > "${CONF_DIR}/${name}.conf"
}

# ── Embedded infrastructure ────────────────────────────────────────────────
if [ "${START_VALKEY}" = "true" ]; then
    emit_program valkey 10 "/var/log/groktocrawl/valkey.log" \
        redis-server --bind 127.0.0.1 --port "${VALKEY_PORT}" \
        --dir "$DATA_ROOT/valkey" --appendonly yes --appendfsync everysec --save ""
fi

if [ "${START_QDRANT}" = "true" ]; then
    {
        echo "[program:qdrant]"
        echo "command=/opt/qdrant/qdrant"
        echo "directory=${DATA_ROOT}/qdrant"
        echo "priority=20"
        echo "autostart=true"
        echo "autorestart=unexpected"
        echo "exitcodes=0"
        echo "startsecs=5"
        echo "startretries=15"
        echo "stopsignal=TERM"
        echo "stopasgroup=true"
        echo "killasgroup=true"
        echo "stopwaitsecs=25"
        echo "redirect_stderr=true"
        echo "stdout_logfile=/var/log/groktocrawl/qdrant.log"
        echo "stdout_logfile_maxbytes=20MB"
        echo "stdout_logfile_backups=3"
        echo "environment=QDRANT__SERVICE__HOST=\"127.0.0.1\",QDRANT__SERVICE__HTTP_PORT=\"6333\",QDRANT__STORAGE__STORAGE_PATH=\"${DATA_ROOT}/qdrant/storage\",QDRANT__STORAGE__SNAPSHOTS_PATH=\"${DATA_ROOT}/qdrant/snapshots\""
    } > "${CONF_DIR}/qdrant.conf"
fi

if [ "${START_SEARXNG}" = "true" ]; then
    {
        echo "[program:searxng]"
        S_BIND="${SEARXNG_BIND:-127.0.0.1}"
        echo "command=gunicorn --workers ${S_WORKERS} --threads ${S_THREADS} --worker-class gthread --timeout 120 --graceful-timeout 30 --bind ${S_BIND}:8888 searx.webapp:app"
        echo "directory=/app"
        echo "priority=20"
        echo "autostart=true"
        echo "autorestart=unexpected"
        echo "exitcodes=0"
        echo "startsecs=5"
        echo "startretries=15"
        echo "stopsignal=TERM"
        echo "stopasgroup=true"
        echo "killasgroup=true"
        echo "stopwaitsecs=25"
        echo "redirect_stderr=true"
        echo "stdout_logfile=/var/log/groktocrawl/searxng.log"
        echo "stdout_logfile_maxbytes=20MB"
        echo "stdout_logfile_backups=3"
        echo "environment=PYTHONPATH=\"/app\",SEARXNG_SETTINGS_PATH=\"/etc/searxng/settings.yml\",SEARX_SETTINGS_PATH=\"/etc/searxng/settings.yml\""
    } > "${CONF_DIR}/searxng.conf"
fi

# ── Core GroktoCrawl services ──────────────────────────────────────────────
emit_program parse-svc 30 "/var/log/groktocrawl/parse-svc.log" \
    python -m uvicorn parse_svc.app:app --host 0.0.0.0 --port 8013

emit_program browser-svc 30 "/var/log/groktocrawl/browser-svc.log" \
    python -m uvicorn browser_svc.app:app --host 0.0.0.0 --port 8012

# scraper-entrypoint tries a best-effort CloakBrowser binary download into the
# mounted cache first (falls back to stock Chromium), then serves uvicorn.
{
    echo "[program:scraper-svc]"
    echo "command=/bin/bash -c '/usr/local/bin/scraper-entrypoint'"
    echo "directory=/app"
    echo "priority=30"
    echo "autostart=true"
    echo "autorestart=unexpected"
    echo "exitcodes=0"
    echo "startsecs=5"
    echo "startretries=15"
    echo "stopsignal=TERM"
    echo "stopasgroup=true"
    echo "killasgroup=true"
    echo "stopwaitsecs=25"
    echo "redirect_stderr=true"
    echo "stdout_logfile=/var/log/groktocrawl/scraper-svc.log"
    echo "stdout_logfile_maxbytes=20MB"
    echo "stdout_logfile_backups=3"
    echo "environment=PYTHONPATH=\"/app\",PYTHONUNBUFFERED=\"1\",HOME=\"/root\""
} > "${CONF_DIR}/scraper-svc.conf"

emit_program semantic-svc 40 "/var/log/groktocrawl/semantic-svc.log" \
    python -m uvicorn app:app --host 0.0.0.0 --port 8003

emit_program agent-svc 50 "/var/log/groktocrawl/agent-svc.log" \
    python -m uvicorn agent.app:app --host 0.0.0.0 --port 8080

emit_program portal-svc 60 "/var/log/groktocrawl/portal-svc.log" \
    python -m uvicorn portal.app:app --host 0.0.0.0 --port 8081

{
    echo "[program:mcp-svc]"
    echo "command=python mcp_server.py"
    echo "directory=/app/mcp_app"
    echo "priority=60"
    echo "autostart=true"
    echo "autorestart=unexpected"
    echo "exitcodes=0"
    echo "startsecs=5"
    echo "startretries=15"
    echo "stopsignal=TERM"
    echo "stopasgroup=true"
    echo "killasgroup=true"
    echo "stopwaitsecs=35"
    echo "redirect_stderr=true"
    echo "stdout_logfile=/var/log/groktocrawl/mcp-svc.log"
    echo "stdout_logfile_maxbytes=20MB"
    echo "stdout_logfile_backups=3"
    echo "environment=PYTHONPATH=\"/app:/app/mcp_app\""
} > "${CONF_DIR}/mcp-svc.conf"

if [ "${START_LLM_FIXTURE}" = "true" ]; then
    emit_program llm-fixture 35 "/var/log/groktocrawl/llm-fixture.log" \
        python -m uvicorn llm_svc.app:app --host 127.0.0.1 --port 8011
fi

# Replaces the upstream `ofelia` sidecar: run monitor checks in-process.
# 注意: 不能走 emit_program —— 它用 ${*} 拼 command= 会丢掉引号, supervisor 对
# command= 做 shlex 拆词后 bash -c 只会拿到 "sleep" 一个词(即 sleep: missing
# operand 直接 FATAL)。这里整段脚本用单引号包裹写入 conf。
if [ "${START_MONITORS}" = "true" ]; then
    {
        echo "[program:monitor-loop]"
        echo "command=/bin/bash -c 'sleep 300; while :; do python3 -m agent.monitor check_all; sleep \${MONITOR_INTERVAL_SECONDS:-600}; done'"
        echo "directory=/app"
        echo "priority=70"
        echo "autostart=true"
        echo "autorestart=unexpected"
        echo "exitcodes=0"
        echo "startsecs=5"
        echo "startretries=15"
        echo "stopsignal=TERM"
        echo "stopasgroup=true"
        echo "killasgroup=true"
        echo "stopwaitsecs=25"
        echo "redirect_stderr=true"
        echo "stdout_logfile=/var/log/groktocrawl/monitor-loop.log"
        echo "stdout_logfile_maxbytes=20MB"
        echo "stdout_logfile_backups=3"
        echo "environment=PYTHONPATH=\"/app\",PYTHONUNBUFFERED=\"1\""
    } > "${CONF_DIR}/monitor-loop.conf"
fi

log "supervisor programs generated:"
ls -1 "${CONF_DIR}" >&2 || true

exec supervisord -n -c /etc/supervisor/supervisord.conf
