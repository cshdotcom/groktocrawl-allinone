"""MCP server exposing GroktoCrawl tools via Model Context Protocol.

Uses FastMCP from the official mcp SDK (v1.x) with Streamable HTTP
transport.  Defines 35 tools matching the GroktoCrawl agent-svc
API surface, with proper readOnlyHint/destructiveHint annotations.

Tool surface policy (see scripts/check-mcp-coverage.py): every agent-svc
``/v2`` endpoint that is expressible as a tool has one.  SSE-streaming,
two-phase-upload, and pre-admission-internal endpoints, plus the
plan/session/research-memory subsystems, are exempted explicitly.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from typing import Any

from browser_handler import BrowserHandler
from groktocrawl_client import GroktocrawlClient
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from session_store import SessionStore

logger = logging.getLogger("grokto_crawl.mcp")

# ── Configuration ──────────────────────────────────────────────────

API_URL: str = os.environ.get(
    "GROKTOCRAWL_URL",
    # 兜底默认指向单容器(all-in-one)内 agent; 旧多镜像名 agent-svc 在
    # 单容器内不可解析, 仅当用户显式设置 GROKTOCRAWL_API_URL 时才会覆盖。
    os.environ.get("GROKTOCRAWL_API_URL", "http://127.0.0.1:8080"),
)
API_KEY: str | None = os.environ.get("GROKTOCRAWL_API_KEY") or None
PORT: int = int(os.environ.get("MCP_PORT", "8002"))
DEFAULT_TIMEOUT: float = float(os.environ.get("HTTP_TIMEOUT", "60"))
_SERVER_START_TIME: float = time.time()

# ── Shared state ───────────────────────────────────────────────────

_client = GroktocrawlClient(
    base_url=API_URL,
    api_key=API_KEY,
    default_timeout=DEFAULT_TIMEOUT,
)

# In-process TTL session store + browser-session router. Browser sessions
# are tracked locally (with SESSION_TTL/SESSION_SWEEP_INTERVAL semantics)
# so tools can validate a session exists before acting on it.
_session_store = SessionStore()
_browser_handler = BrowserHandler(_client, _session_store)

# ── FastMCP server ─────────────────────────────────────────────────

# DNS-rebinding protection: the mcp SDK auto-enables loopback-only Host
# validation when FastMCP is constructed without explicit transport_security
# (its default host is 127.0.0.1). That rejects every non-loopback Host header
# with 421 — the server binds 0.0.0.0, so real clients (Hermes, Claude Code,
# remote MCP consumers) can never connect. Pass explicit settings instead:
#
# - MCP_ALLOWED_HOSTS set   -> protection ON, allowlist = parsed hosts
# - MCP_ALLOWED_HOSTS unset -> protection ON, fail-closed loopback-only
#   allowlist (the SDK's own default: 127.0.0.1, localhost, ::1). The service
#   is published on 0.0.0.0, so deployments that need LAN/Tailscale clients
#   must extend the allowlist via MCP_ALLOWED_HOSTS.
# - MCP_ALLOWED_ORIGINS set -> Origin allowlist = parsed origins (overrides
#   the host-derived default below)
#
# Values are comma-separated Host patterns, e.g.
# "hal2000:*,localhost:*,127.0.0.1:*,[::1]:*". Wildcards apply to the port
# only ("host:*"); a bare "*" matches nothing in this SDK version.


def _build_transport_security() -> TransportSecuritySettings:
    """Build DNS-rebinding protection settings from MCP_ALLOWED_HOSTS.

    Fail-closed: protection is always enabled. An unset MCP_ALLOWED_HOSTS
    falls back to the SDK's loopback-only allowlist; operators extend it
    (never disable it) via MCP_ALLOWED_HOSTS.
    """
    raw = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    allowed = [h.strip() for h in raw.split(",") if h.strip()]
    if not allowed:
        allowed = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        logger.info(
            "MCP_ALLOWED_HOSTS unset — protection ON with loopback-only allowlist"
        )
    else:
        logger.info("DNS-rebinding protection enabled for hosts: %s", allowed)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
        allowed_origins=_build_allowed_origins(allowed),
    )


def _build_allowed_origins(hosts: list[str]) -> list[str]:
    """Derive the Origin allowlist, honoring MCP_ALLOWED_ORIGINS when set.

    Origins default to the host allowlist under both http and https schemes
    so legitimate browser-served client pages are not rejected with 403.
    Operators can scope origins independently of the server's Host allowlist
    via MCP_ALLOWED_ORIGINS (comma-separated, e.g.
    "http://hal2000:*,https://hal2000:*").
    """
    raw = os.environ.get("MCP_ALLOWED_ORIGINS", "").strip()
    if raw:
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        logger.info("MCP_ALLOWED_ORIGINS set — origin allowlist: %s", origins)
        return origins
    origins: list[str] = []
    for h in hosts:
        origins.append(f"http://{h}")
        origins.append(f"https://{h}")
    return origins


mcp = FastMCP("GroktoCrawl", transport_security=_build_transport_security())

# ── Annotation helpers ─────────────────────────────────────────────

_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
# Neutral: modifies server/third-party state without being destructive
# (e.g. updating a monitor, running a check, acting on a browser session).
_NEUTRAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def _json_text(data: dict[str, Any]) -> str:
    """Serialize a dict to indented JSON for use as MCP text content."""
    return json.dumps(data, indent=2, default=str, ensure_ascii=False)


def _resp(data: dict[str, Any]) -> str:
    """Convert a client result dict to a JSON string response.

    Always returns valid JSON.  When the result contains a truthy
    ``error`` value (non-None, non-empty) and ``success`` is not
    explicitly True, wraps it in a ``{"success": false, ...}``
    envelope so that MCP clients can parse it reliably.
    """
    if isinstance(data, dict):
        error_val = data.get("error")
        success_val = data.get("success")
        if error_val and success_val is not True:
            error_obj: dict[str, Any] = {
                "success": False,
                "error": str(error_val),
            }
            if "status_code" in data:
                error_obj["status_code"] = data["status_code"]
            return _json_text(error_obj)
    return _json_text(data)


def _browser_action_kwargs(**params: Any) -> dict[str, Any]:
    """Drop unset browser-action params so nulls are never sent upstream.

    agent-svc's ``BrowserExecuteRequest`` has non-optional scalar fields
    (e.g. ``timeout``), so passing ``None`` would 422.  Only explicitly
    provided values are forwarded.
    """
    return {key: value for key, value in params.items() if value is not None}


def _ensure_success(data: dict[str, Any]) -> None:
    """Raise :class:`ToolError` if *data* contains an upstream error.

    FastMCP converts ``ToolError`` to a response with ``isError: true``
    so that MCP clients can programmatically detect 4xx/5xx failures
    and failed jobs from the upstream agent-svc.

    Two conditions raise:

    * A truthy ``error`` value (non-None, non-empty) when ``success`` is
      not explicitly True — covers HTTP-level errors.
    * ``status == "failed"`` with a truthy ``error`` even when
      ``success`` is True — covers failed background jobs returned by
      the status endpoints (``get_agent_status``, ``get_crawl_status``,
      etc.), so clients can detect job failure programmatically instead
      of parsing ``{"success": true, "status": "failed"}`` as a success.

    Important: agent-svc includes ``"error": null`` in successful
    responses, so null/empty error values are never treated as errors,
    and ``cancelled`` / ``retry_scheduled`` statuses are returned as-is.
    """
    if isinstance(data, dict):
        error_val = data.get("error")
        success_val = data.get("success")
        status_val = data.get("status")
        # Only treat as error when there's a truthy error value AND
        # success is not explicitly True.
        if error_val and success_val is not True:
            raise ToolError(_resp(data))
        # Failed background jobs: success is True but the job itself failed.
        if status_val == "failed" and error_val:
            raise ToolError(_resp(data))


# ── Tools 1–2: scrape, search (read-only) ─────────────────────────


@mcp.tool(annotations=_RO)
async def scrape(
    url: str,
    formats: list[str] | None = None,
    only_main_content: bool = True,
) -> str:
    """Scrape a single URL and return its content as markdown or other formats.

    Calls POST /v2/scrape on the GroktoCrawl API.  The primary output is
    clean markdown suitable for LLM consumption.  Optionally request
    additional formats (html, screenshot, links, rawHtml, images).

    Args:
        url: The URL to scrape.  Must start with http:// or https://.
        formats: Additional output formats to include.  Supported:
            markdown, html, links, screenshot, rawHtml,
            screenshot@fullPage, images.
        only_main_content: When True (default), extract only the main
            article content.  Set False to get the full page.
    """
    result = await _client.scrape(
        url=url,
        formats=formats,
        only_main_content=only_main_content,
    )
    _ensure_success(result)
    return _resp(result)


# ── Tools 3–6: crawl, get_crawl_status, cancel_crawl, get_crawl_errors ──


@mcp.tool(annotations=_RO)
async def crawl(
    url: str,
    max_pages: int | None = None,
    max_depth: int | None = None,
    limit: int | None = None,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    sitemap: str | None = None,
    ignore_robots_txt: bool = False,
    max_concurrency: int | None = None,
    delay: float | None = None,
    allow_subdomains: bool = False,
    allow_external_links: bool = False,
    prompt: str | None = None,
) -> str:
    """Start a recursive crawl of a website.  Returns a job ID immediately.

    Calls POST /v2/crawl on the GroktoCrawl API.  The crawl runs
    asynchronously — use get_crawl_status to poll for results and
    cancel_crawl to stop an in-progress crawl.

    Args:
        url: The starting URL for the crawl (http:// or https://).
        max_pages: Maximum number of pages to scrape.  Omit for
            unlimited (agent-svc default is 0 = unlimited; set a
            concrete value to bound resource usage).
        max_depth: Maximum link-follow depth from the start URL.
            Default 2 when omitted.
        limit: Optional cap on the total number of pages (alias for
            a bounded crawl budget).
        include_paths: Only crawl URLs whose path matches one of these
            prefixes/regexes.
        exclude_paths: Skip URLs whose path matches one of these
            prefixes/regexes.
        sitemap: Sitemap mode — ``include`` (default), ``skip``, or
            ``only``.
        ignore_robots_txt: When True, bypass robots.txt enforcement.
        max_concurrency: Maximum concurrent page scrapes (1-50).
        delay: Delay in seconds between scrapes (forces concurrency 1).
        allow_subdomains: Crawl links on subdomains of the start URL.
        allow_external_links: Crawl links on other domains.
        prompt: Natural-language description of what to crawl; derives
            include/exclude paths and depth via LLM.  Explicit params
            override LLM-derived ones.
    """
    result = await _client.create_crawl(
        url=url,
        max_pages=max_pages,
        max_depth=max_depth,
        limit=limit,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        sitemap=sitemap,
        ignore_robots_txt=ignore_robots_txt,
        max_concurrency=max_concurrency,
        delay=delay,
        allow_subdomains=allow_subdomains,
        allow_external_links=allow_external_links,
        prompt=prompt,
    )
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_RO)
async def get_crawl_status(job_id: str) -> str:
    """Poll the status of a crawl job and return page data when complete.

    Calls GET /v2/crawl/{job_id} on the GroktoCrawl API.  Returns the
    current status (processing/completed/failed), page counts, and
    (when finished) the scraped page content with metadata.

    Args:
        job_id: The crawl job ID returned by the crawl tool.
    """
    result = await _client.get_crawl_status(job_id)
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_DESTRUCTIVE)
async def cancel_crawl(job_id: str) -> str:
    """Cancel an in-progress crawl job.  The crawl transitions to cancelled.

    Calls DELETE /v2/crawl/{job_id} on the GroktoCrawl API.  Already
    scraped pages are preserved in subsequent status polls.

    Args:
        job_id: The crawl job ID to cancel.
    """
    result = await _client.cancel_crawl(job_id)
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_RO)
async def get_crawl_errors(job_id: str) -> str:
    """Retrieve per-URL errors and robots-blocked URLs for a crawl job.

    Calls GET /v2/crawl/{job_id}/errors on the GroktoCrawl API.
    Returns a structured list of errors including the failing URL,
    error type, and timestamp.

    Args:
        job_id: The crawl job ID returned by the crawl tool.
    """
    result = await _client.get_crawl_errors(job_id)
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_RO)
async def get_active_crawls() -> str:
    """List active/processing crawl jobs.

    Calls GET /v2/crawl/active on the GroktoCrawl API.  Returns crawl
    jobs currently processing with crawl-specific fields (url,
    max_pages, max_depth, completed, total).

    Args:
        (none)
    """
    result = await _client.get_active_crawls()
    _ensure_success(result)
    return _resp(result)


# ── Tool 7: map ────────────────────────────────────────────────────


@mcp.tool(annotations=_RO)
async def map(
    url: str,
    limit: int = 100,
    search: str | None = None,
    allow_subdomains: bool = False,
    allow_external_links: bool = False,
) -> str:
    """Discover all URLs linked from a given page (site mapping).

    Calls POST /v2/map on the GroktoCrawl API.  Returns a list of
    URLs found on the page, classified as internal, subdomain, or
    external links.

    Args:
        url: The page URL to map.  Must start with http:// or https://.
        limit: Maximum number of links to return (default 100).
        search: Optional case-insensitive substring filter on links.
        allow_subdomains: Include links on subdomains of the page origin.
        allow_external_links: Include links to other domains.
    """
    result = await _client.map(
        url=url,
        limit=limit,
        search=search,
        allow_subdomains=allow_subdomains,
        allow_external_links=allow_external_links,
    )
    _ensure_success(result)
    return _resp(result)


# ── Tools 8–9: agent, get_agent_status ─────────────────────────────


# ── Tool 10: answer ────────────────────────────────────────────────


# ── Tools 11–12: extract, get_extract_status ───────────────────────


@mcp.tool(annotations=_RO)
async def extract(
    urls: list[str],
    prompt: str | None = None,
    schema: dict[str, Any] | None = None,
    model: str | None = None,
) -> str:
    """Extract structured data from one or more URLs.  Returns a job ID.

    Calls POST /v2/extract on the GroktoCrawl API.  The extraction runs
    asynchronously — use get_extract_status to poll for results.

    Args:
        urls: List of URLs to extract data from.
        prompt: Natural language description of what to extract
            (e.g. "Extract all product names and prices").
        schema: Optional JSON Schema for structured output.  When
            provided, the LLM returns data matching the schema.
        model: Optional per-request LLM model override (e.g. ``gpt-4o``).
    """
    result = await _client.create_extract(
        urls=urls, prompt=prompt, schema=schema, model=model
    )
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_RO)
async def get_extract_status(job_id: str) -> str:
    """Poll the status of an extract job and return structured data when done.

    Calls GET /v2/extract/{job_id} on the GroktoCrawl API.  Returns the
    current status and, when completed, the extracted structured data
    matching the requested schema or prompt.

    Args:
        job_id: The extract job ID returned by the extract tool.
    """
    result = await _client.get_extract_status(job_id)
    _ensure_success(result)
    return _resp(result)


# ── Tool 15: extract ───────────────────────────────────────────────


# ── Tool 15: batch_scrape ──────────────────────────────────────────


@mcp.tool(annotations=_RO)
async def batch_scrape(
    urls: list[str],
    max_concurrency: int | None = None,
) -> str:
    """Scrape multiple URLs in a single batch job.  Returns a job ID.

    Calls POST /v2/batch/scrape on the GroktoCrawl API.  The batch runs
    asynchronously — use get_batch_scrape_status to poll for results.

    Args:
        urls: List of URLs to scrape.  All URLs are processed
            concurrently by the scraper service.
        max_concurrency: Optional cap on concurrent scrapes.
    """
    result = await _client.create_batch_scrape(
        urls=urls, max_concurrency=max_concurrency
    )
    _ensure_success(result)
    return _resp(result)


# ── Tool 16: generate_llmstxt ──────────────────────────────────────


@mcp.tool(annotations=_RO)
async def generate_llmstxt(url: str, max_pages: int | None = None) -> str:
    """Generate an llms.txt file for a website.  Returns a job ID.

    Calls POST /v2/generate-llmstxt on the GroktoCrawl API.  The
    generation runs asynchronously — use get_llmstxt_status to poll
    for the completed llms.txt content.

    Args:
        url: The website URL to generate llms.txt for.  Must start
            with http:// or https://.
        max_pages: Maximum pages to include in the llms.txt file.
            Omit for server default.
    """
    result = await _client.create_llmstxt(url=url, max_pages=max_pages)
    _ensure_success(result)
    return _resp(result)


# ── Tool 17: get_activity ──────────────────────────────────────────


@mcp.tool(annotations=_RO)
async def get_activity() -> str:
    """Retrieve recent API activity including job IDs types statuses and timestamps.

    Calls GET /v2/activity on the GroktoCrawl API.  Returns a list of
    recent jobs across all types (scrape crawl agent extract etc.) with
    their current status and creation timestamps.  Useful for monitoring
    and debugging.
    """
    result = await _client.get_activity()
    _ensure_success(result)
    return _resp(result)


# ── Tool 18: get_batch_scrape_status ────────────────────────────────


@mcp.tool(annotations=_RO)
async def get_batch_scrape_status(job_id: str) -> str:
    """Poll the status of a batch scrape job and return results when done.

    Calls GET /v2/batch/scrape/{job_id} on the GroktoCrawl API.
    Returns the current status (processing/completed/failed) and,
    when finished, the scraped content for each URL in the batch.

    Args:
        job_id: The batch scrape job ID returned by the batch_scrape tool.
    """
    result = await _client.get_batch_scrape_status(job_id)
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_DESTRUCTIVE)
async def cancel_batch_scrape(job_id: str) -> str:
    """Cancel an in-progress batch scrape job.

    Calls DELETE /v2/batch/scrape/{job_id} on the GroktoCrawl API.  The
    job transitions to cancelled.

    Args:
        job_id: The batch scrape job ID returned by the batch_scrape tool.
    """
    result = await _client.cancel_batch_scrape(job_id)
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_RO)
async def get_batch_scrape_errors(job_id: str) -> str:
    """Retrieve per-URL errors for a batch scrape job.

    Calls GET /v2/batch/scrape/{job_id}/errors on the GroktoCrawl API.
    Returns a structured list of errors including the failing URL and
    error type.

    Args:
        job_id: The batch scrape job ID returned by the batch_scrape tool.
    """
    result = await _client.get_batch_scrape_errors(job_id)
    _ensure_success(result)
    return _resp(result)


# ── Tool 19: get_llmstxt_status ─────────────────────────────────────


@mcp.tool(annotations=_RO)
async def get_llmstxt_status(job_id: str) -> str:
    """Poll the status of an llms.txt generation job and return the file when done.

    Calls GET /v2/generate-llmstxt/{job_id} on the GroktoCrawl API.
    Returns the current status and, when completed, the generated
    llms.txt content.

    Args:
        job_id: The llms.txt generation job ID returned by the
            generate_llmstxt tool.
    """
    result = await _client.get_llmstxt_status(job_id)
    _ensure_success(result)
    return _resp(result)


# ── Tool 21: parse ────────────────────────────────────────────────


@mcp.tool(annotations=_RO)
async def parse(file_url: str) -> str:
    """Parse a document (PDF, DOCX, PPTX, etc.) served at a URL to markdown.

    Calls POST /v2/parse on the GroktoCrawl API.  Downloads the file
    (with a separate unauthenticated client, so the GroktoCrawl API key
    is never sent to third-party hosts) and uploads it to the parse
    endpoint.

    Args:
        file_url: Direct URL of the document to parse.
    """
    result = await _client.parse(file_url)
    _ensure_success(result)
    return _resp(result)


# ── Tools 22-25: browser sessions ─────────────────────────────────


@mcp.tool(annotations=_RO)
async def create_browser_session(ttl: int = 300) -> str:
    """Create a short-lived headless browser session.  Returns a session ID.

    Calls POST /v2/browser on the GroktoCrawl API.  Use
    browser_execute to act on the session and destroy_browser_session
    to close it.

    Args:
        ttl: Session TTL in seconds (30–3600, default 300).
    """
    result = await _browser_handler.create_session(ttl=ttl)
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_NEUTRAL)
async def browser_execute(
    session_id: str,
    action: str,
    url: str | None = None,
    selector: str | None = None,
    text: str | None = None,
    script: str | None = None,
    timeout: int | None = None,
    wait_until: str | None = None,
) -> str:
    """Execute an action in a headless browser session.

    Calls POST /v2/browser/{session_id}/execute on the GroktoCrawl API.

    Args:
        session_id: The browser session ID from create_browser_session.
        action: One of: navigate, click, type, screenshot, scroll, wait,
            getContent, executeScript, select, write.
        url: Target URL (navigate action).
        selector: CSS selector (click/type/select actions).
        text: Text to type (type/write actions).
        script: JavaScript source (executeScript action).
        timeout: Action timeout in milliseconds (default 10000).
        wait_until: Navigation wait condition for navigate: domcontentloaded
            (default), load, commit, or networkidle. Prefer domcontentloaded
            for portal sites with continuous analytics traffic.
    """
    result = await _browser_handler.execute_action(
        session_id=session_id,
        action=action,
        **_browser_action_kwargs(
            url=url,
            selector=selector,
            text=text,
            script=script,
            timeout=timeout,
            wait_until=wait_until,
        ),
    )
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_RO)
async def list_browser_sessions() -> str:
    """List active headless browser sessions.

    Calls GET /v2/browser on the GroktoCrawl API.

    Args:
        (none)
    """
    result = await _client.browser_list()
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_DESTRUCTIVE)
async def destroy_browser_session(session_id: str) -> str:
    """Destroy a headless browser session.

    Calls DELETE /v2/browser/{session_id} on the GroktoCrawl API.

    Args:
        session_id: The browser session ID to destroy.
    """
    result = await _browser_handler.destroy_session(session_id)
    _ensure_success(result)
    return _resp(result)


# ── Tools 26-31: change monitors ──────────────────────────────────


@mcp.tool(annotations=_RO)
async def create_monitor(
    url: str | None = None,
    schedule: str = "0 */6 * * *",
    webhook: str | None = None,
    monitor_type: str = "scrape",
) -> str:
    """Create a scheduled change monitor.  Returns a monitor ID.

    Calls POST /v2/monitor on the GroktoCrawl API.  Scrape-type monitors
    watch a URL for content changes.  (Lite edition: search-type monitors
    are not available — no embedded SearXNG.)

    Args:
        url: URL to monitor (required for monitor_type ``scrape``).
        schedule: Cron expression for check frequency (default
            ``0 */6 * * *`` = every 6 hours).
        webhook: URL called when a change is detected.
        monitor_type: ``scrape`` (default). Search type is not supported
            on the lite edition.
    """
    if monitor_type != "scrape":
        raise ToolError(
            f"create_monitor: lite edition supports monitor_type='scrape' only "
            f'(got {monitor_type!r}; search monitors need the full image)'
        )
    if not url:
        raise ToolError("create_monitor: url is required for monitor_type='scrape'")
    result = await _client.monitor_create(
        url=url,
        schedule=schedule,
        webhook=webhook,
        monitor_type=monitor_type,
    )
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_RO)
async def list_monitors() -> str:
    """List all change monitors.

    Calls GET /v2/monitor on the GroktoCrawl API.

    Args:
        (none)
    """
    result = await _client.monitor_list()
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_RO)
async def get_monitor(monitor_id: str) -> str:
    """Get a change monitor's details and last check result.

    Calls GET /v2/monitor/{monitor_id} on the GroktoCrawl API.

    Args:
        monitor_id: The monitor ID returned by create_monitor.
    """
    result = await _client.monitor_get(monitor_id)
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_NEUTRAL)
async def update_monitor(
    monitor_id: str,
    url: str | None = None,
    schedule: str | None = None,
    webhook: str | None = None,
) -> str:
    """Update a change monitor's configuration.

    Calls PATCH /v2/monitor/{monitor_id} on the GroktoCrawl API.  Only
    provided fields are changed.  (Lite edition: scrape-type monitors only.)

    Args:
        monitor_id: The monitor ID to update.
        url: New URL to monitor (scrape type).
        schedule: New cron expression.
        webhook: New webhook URL.
    """
    result = await _client.monitor_update(
        monitor_id=monitor_id,
        url=url,
        schedule=schedule,
        webhook=webhook,
    )
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_NEUTRAL)
async def run_monitor(monitor_id: str) -> str:
    """Manually trigger a monitor check immediately.

    Calls POST /v2/monitor/{monitor_id}/run on the GroktoCrawl API.
    Runs the check regardless of the cron schedule and returns the
    updated monitor status including any diff or new results.

    Args:
        monitor_id: The monitor ID to run.
    """
    result = await _client.monitor_run(monitor_id)
    _ensure_success(result)
    return _resp(result)


@mcp.tool(annotations=_DESTRUCTIVE)
async def delete_monitor(monitor_id: str) -> str:
    """Delete a change monitor.

    Calls DELETE /v2/monitor/{monitor_id} on the GroktoCrawl API.

    Args:
        monitor_id: The monitor ID to delete.
    """
    result = await _client.monitor_delete(monitor_id)
    _ensure_success(result)
    return _resp(result)


# ── Auth middleware ────────────────────────────────────────────────


class _AuthMiddleware:
    """ASGI middleware that enforces Bearer token auth when
    GROKTOCRAWL_API_KEY is set in the environment.
    """

    def __init__(self, app: Any, api_key: str) -> None:
        self._app = app
        self._api_key = api_key

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # Only protect /mcp path; allow health and others through
        path: str = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_bytes = headers.get(b"authorization", b"")
        auth_str = auth_bytes.decode() if auth_bytes else ""

        valid = auth_str.startswith("Bearer ") and hmac.compare_digest(
            auth_str[7:], self._api_key
        )
        if not valid:
            body = json.dumps({"error": "Unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self._app(scope, receive, send)


# ── Health endpoint ────────────────────────────────────────────────


async def _check_agent_svc() -> bool:
    """Check whether agent-svc is reachable and healthy.

    Returns True if agent-svc responds with HTTP 200, False otherwise.
    Uses a short timeout so the health check is non-blocking.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            resp = await client.get(f"{API_URL}/health")
            return resp.status_code == 200
    except Exception:
        return False


async def _health_endpoint(scope: dict, receive: Any, send: Any) -> None:
    """ASGI health-check handler returning server status and agent-svc connectivity."""
    agent_svc_status = await _check_agent_svc()
    uptime = time.time() - _SERVER_START_TIME
    body = json.dumps(
        {
            "status": "ok",
            "agent_svc": "connected" if agent_svc_status else "disconnected",
            "uptime_seconds": round(uptime, 1),
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# ── Logging middleware ─────────────────────────────────────────────


class _LoggingMiddleware:
    """ASGI middleware that logs MCP requests at INFO level.

    Logs method, tool name (when available), and a session prefix.
    Never logs API keys or full content bodies.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Collect request body for MCP method extraction
        body_chunks: list[bytes] = []

        async def _recv() -> dict:
            msg = await receive()
            if msg.get("type") == "http.request" and msg.get("body"):
                body_chunks.append(msg["body"])
            return msg

        async def _send(msg: dict) -> None:
            # Log on response start
            if msg.get("type") == "http.response.start":
                status: int = msg.get("status", 0)
                session_prefix = ""
                headers = dict(scope.get("headers", []))
                sid = headers.get(b"mcp-session-id", b"")
                if sid:
                    session_prefix = sid.decode()[:8] + "... "
                if path.startswith("/mcp"):
                    tool_name = ""
                    if body_chunks:
                        try:
                            body = json.loads(b"".join(body_chunks))
                            mcp_method = body.get("method", "")
                            if mcp_method == "tools/call":
                                tool_name = " tool=" + body.get("params", {}).get(
                                    "name", "?"
                                )
                            mcp_info = f"{mcp_method}{tool_name}"
                        except (json.JSONDecodeError, KeyError, TypeError):
                            mcp_info = "?"
                    else:
                        mcp_info = "?"
                    logger.info(
                        "MCP request: method=%s%s session=%sstatus=%s",
                        mcp_info,
                        "",
                        session_prefix,
                        status,
                    )
            await send(msg)

        if path == "/health":
            await _health_endpoint(scope, receive, send)
        else:
            await self._app(scope, _recv, _send)


# ── Entrypoint ─────────────────────────────────────────────────────


def main() -> None:
    """Start the MCP server with Streamable HTTP transport on port 8002."""
    import asyncio
    import signal

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info(
        "Starting GroktoCrawl MCP server on port %s (agent-svc: %s)",
        PORT,
        API_URL,
    )

    app = mcp.streamable_http_app()

    # Logging middleware (innermost, so it sees all MCP requests)
    app.add_middleware(_LoggingMiddleware)

    # Auth middleware (outermost, so it blocks before logging/processing)
    if API_KEY:
        logger.info("API key auth enabled for /mcp path")
        app.add_middleware(_AuthMiddleware, api_key=API_KEY)
    else:
        logger.info("No API key set — auth disabled")

    import uvicorn

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        timeout_graceful_shutdown=30,
    )
    server = uvicorn.Server(config)

    # Signal handlers log shutdown so operators can see the drain window.
    # uvicorn's own signal handling (installed inside serve()) drives the
    # actual graceful shutdown with timeout_graceful_shutdown.
    def _handle_shutdown() -> None:
        logger.info("Received shutdown signal, draining in-flight requests...")

    loop = asyncio.new_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, _handle_shutdown)
        loop.add_signal_handler(signal.SIGINT, _handle_shutdown)
    except NotImplementedError:
        # Signal handlers not available on this platform (e.g. Windows)
        import contextlib

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, lambda *_: _handle_shutdown())

    loop.run_until_complete(server.serve())


if __name__ == "__main__":
    main()
