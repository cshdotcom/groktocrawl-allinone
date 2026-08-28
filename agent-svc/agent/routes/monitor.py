"""Monitor route handlers — scheduled web monitoring."""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from ..exceptions import NotFoundError
from ..models import (
    MonitorCreateRequest,
    MonitorDeleteResponse,
    MonitorListResponse,
    MonitorResponse,
    MonitorUpdateRequest,
)
from ..monitor import (
    delete_monitor,
    get_all_monitors,
    get_monitor,
    run_monitor,
    save_monitor,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v2/monitor", response_model=MonitorResponse)
async def create_monitor(body: MonitorCreateRequest) -> MonitorResponse:
    monitor_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    if body.monitor_type == "search":
        config = {
            "monitor_type": "search",
            "search_config": body.search_config.model_dump()
            if body.search_config
            else {},
            "schedule": body.schedule,
            "webhook": body.webhook,
            "created_at": now,
        }
        save_monitor(monitor_id, config)
        return MonitorResponse(
            id=monitor_id,
            monitor_type="search",
            search_config=config["search_config"],  # type: ignore[arg-type]
            schedule=body.schedule,
            webhook=body.webhook,
            created_at=now,
        )
    else:
        # Scrape type (default)
        config = {
            "monitor_type": "scrape",
            "url": body.url,
            "schedule": body.schedule,
            "webhook": body.webhook,
            "created_at": now,
            "last_content": "",
        }
        save_monitor(monitor_id, config)
        return MonitorResponse(
            id=monitor_id,
            monitor_type="scrape",
            url=body.url,
            schedule=body.schedule,
            webhook=body.webhook,
            created_at=now,
        )


@router.get("/v2/monitor", response_model=MonitorListResponse)
async def list_monitors() -> MonitorListResponse:
    monitors = get_all_monitors()
    items = []
    for mid, cfg in monitors.items():
        mt = cfg.get("monitor_type", "scrape")
        if mt == "search":
            items.append(
                MonitorResponse(
                    id=mid,
                    monitor_type="search",
                    search_config=cfg.get("search_config"),  # type: ignore[arg-type]
                    schedule=cfg.get("schedule", ""),
                    webhook=cfg.get("webhook"),
                    last_checked=cfg.get("last_checked"),
                    last_result=cfg.get("last_result"),
                    created_at=cfg.get("created_at", ""),
                )
            )
        else:
            items.append(
                MonitorResponse(
                    id=mid,
                    monitor_type="scrape",
                    url=cfg.get("url", ""),
                    schedule=cfg.get("schedule", ""),
                    webhook=cfg.get("webhook"),
                    last_checked=cfg.get("last_checked"),
                    last_result=cfg.get("last_result"),
                    created_at=cfg.get("created_at", ""),
                )
            )
    return MonitorListResponse(monitors=items)


@router.get("/v2/monitor/{monitor_id}", response_model=MonitorResponse)
async def get_monitor_status(monitor_id: str) -> MonitorResponse:
    cfg = get_monitor(monitor_id)
    if cfg is None:
        raise NotFoundError(
            detail="Monitor not found", details={"monitor_id": monitor_id}
        )
    mt = cfg.get("monitor_type", "scrape")
    if mt == "search":
        return MonitorResponse(
            id=monitor_id,
            monitor_type="search",
            search_config=cfg.get("search_config"),  # type: ignore[arg-type]
            schedule=cfg.get("schedule", ""),
            webhook=cfg.get("webhook"),
            last_checked=cfg.get("last_checked"),
            last_result=cfg.get("last_result"),
            created_at=cfg.get("created_at", ""),
        )
    else:
        return MonitorResponse(
            id=monitor_id,
            monitor_type="scrape",
            url=cfg.get("url", ""),
            schedule=cfg.get("schedule", ""),
            webhook=cfg.get("webhook"),
            last_checked=cfg.get("last_checked"),
            last_result=cfg.get("last_result"),
            created_at=cfg.get("created_at", ""),
        )


@router.patch("/v2/monitor/{monitor_id}", response_model=MonitorResponse)
async def update_monitor(
    monitor_id: str, body: MonitorUpdateRequest
) -> MonitorResponse:
    cfg = get_monitor(monitor_id)
    if cfg is None:
        raise NotFoundError(
            detail="Monitor not found", details={"monitor_id": monitor_id}
        )
    if body.url is not None:
        cfg["url"] = body.url
    if body.schedule is not None:
        cfg["schedule"] = body.schedule
    if body.webhook is not None:
        cfg["webhook"] = body.webhook
    if body.search_config is not None:
        cfg["search_config"] = body.search_config.model_dump()
    save_monitor(monitor_id, cfg)
    mt = cfg.get("monitor_type", "scrape")
    if mt == "search":
        return MonitorResponse(
            id=monitor_id,
            monitor_type="search",
            search_config=cfg.get("search_config"),  # type: ignore[arg-type]
            schedule=cfg.get("schedule", ""),
            webhook=cfg.get("webhook"),
            last_checked=cfg.get("last_checked"),
            last_result=cfg.get("last_result"),
            created_at=cfg.get("created_at", ""),
        )
    else:
        return MonitorResponse(
            id=monitor_id,
            monitor_type="scrape",
            url=cfg.get("url", ""),
            schedule=cfg.get("schedule", ""),
            webhook=cfg.get("webhook"),
            last_checked=cfg.get("last_checked"),
            last_result=cfg.get("last_result"),
            created_at=cfg.get("created_at", ""),
        )


@router.delete("/v2/monitor/{monitor_id}", response_model=MonitorDeleteResponse)
async def delete_monitor_route(monitor_id: str) -> MonitorDeleteResponse:
    cfg = get_monitor(monitor_id)
    if cfg is None:
        raise NotFoundError(
            detail="Monitor not found", details={"monitor_id": monitor_id}
        )
    # Clean up search monitor seen set
    if cfg.get("monitor_type") == "search":
        from redis import Redis

        r = Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
        r.delete(f"monitor:{monitor_id}:seen")
    delete_monitor(monitor_id)
    return MonitorDeleteResponse(success=True)


@router.post("/v2/monitor/{monitor_id}/run", response_model=MonitorResponse)
async def run_monitor_check(request: Request, monitor_id: str) -> MonitorResponse:
    """Manually trigger a monitor check immediately.

    Runs the check regardless of the cron schedule and returns
    the updated monitor status including any diff or new results.
    """
    scraper_url = request.app.state.scraper_url

    try:
        await run_monitor(monitor_id, scraper_url=scraper_url)
    except ValueError:
        raise NotFoundError(
            detail="Monitor not found", details={"monitor_id": monitor_id}
        )

    cfg = get_monitor(monitor_id)
    if cfg is None:
        raise NotFoundError(
            detail="Monitor not found", details={"monitor_id": monitor_id}
        )

    search_config = cfg.get("search_config")
    if isinstance(search_config, str):
        import json

        search_config = json.loads(search_config)

    return MonitorResponse(
        id=monitor_id,
        monitor_type=cfg.get("monitor_type", "scrape"),
        url=cfg.get("url"),
        search_config=search_config,
        schedule=cfg.get("schedule", ""),
        webhook=cfg.get("webhook"),
        last_checked=cfg.get("last_checked"),
        last_result=cfg.get("last_result"),
        created_at=cfg.get("created_at", ""),
    )
