#!/usr/bin/env bash
# ============================================================================
# GroktoCrawl all-in-one entrypoint
#
# LITE edition — no embedded SearXNG / Qdrant / semantic-svc / portal.
#
# Responsibilities:
#   1. Resolve the EMBED_VALKEY toggle (auto mode: start embedded Valkey only
#      when no external URL has been configured).
#   2. Emit supervisor program files for every component that should run.
#   3. exec supervisord -n  (PID1 handles signals + zombie reaping).
#
# Every upstream env var passes through unchanged — extra services pick their
# own settings up from the environment (docker-compose env_file: .env).
# ============================================================================
set -euo pipefail

LOGDIR=/var/log/groktocrawl
DATA_ROOT="${GROKTOCRAWL_DATA:-/data}"
CONF_DIR=/etc/supervisor/conf.d

mkdir -p "$DATA_ROOT/valkey" "$DATA_ROOT/cloakbrowser" \
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
scrub_legacy_url SCRAPER_URL
scrub_legacy_url BROWSER_SVC_URL
scrub_legacy_url LLM_BASE_URL

# ── Resolve embedded toggles ───────────────────────────────────────────────
VALKEY_HOST="${VALKEY_HOST:-127.0.0.1}"
VALKEY_PORT="${VALKEY_PORT:-6379}"
VALKEY_DB="${VALKEY_DB:-0}"
export VALKEY_URL="${VALKEY_URL:-redis://${VALKEY_HOST}:${VALKEY_PORT}/${VALKEY_DB}}"

export SCRAPER_URL="${SCRAPER_URL:-http://127.0.0.1:8001}"
export BROWSER_SVC_URL="${BROWSER_SVC_URL:-http://127.0.0.1:8012}"
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

log "embedded components: valkey=${START_VALKEY} llm-fixture=${START_LLM_FIXTURE} monitor-loop=${START_MONITORS} (lite: no qdrant/searxng/semantic/portal)"

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


emit_program agent-svc 50 "/var/log/groktocrawl/agent-svc.log" \
    python -m uvicorn agent.app:app --host 0.0.0.0 --port 8080


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
