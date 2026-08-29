"""Routes package for GroktoCrawl API — combines all domain routers into one."""

import logging

from fastapi import APIRouter

from .activity import router as activity_router
from .browser import router as browser_router
from .crawl import router as crawl_router
from .extract import router as extract_router
from .llmstxt import router as llmstxt_router
from .map import router as map_router
from .monitor import router as monitor_router
from .parse import router as parse_router
from .scrape import router as scrape_router
from .webhook import router as webhook_router

logger = logging.getLogger(__name__)

router = APIRouter()

router.include_router(activity_router)
router.include_router(scrape_router)
router.include_router(crawl_router)
router.include_router(map_router)
router.include_router(extract_router)
router.include_router(monitor_router)
router.include_router(browser_router)
router.include_router(webhook_router)
router.include_router(llmstxt_router)
router.include_router(parse_router)
