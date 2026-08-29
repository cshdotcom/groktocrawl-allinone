"""Structured data extraction (/v2/extract) — lite edition.

Self-contained replacement for the deleted ``research`` package on the lite
branch. Only the extract pipeline is kept: scrape → LLM → structured output.
No SearXNG / semantic-svc / research-loop dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import jsonschema
from jsonschema.validators import validator_for
from referencing.exceptions import Unresolvable

from .exceptions import StructuredOutputError
from .llm import LLMClient
from .scraper_client import ScraperClient

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM_PROMPT = """You are GroktoCrawl, a structured data extraction agent.
Your job is to extract the requested information from the provided web content
as completely and accurately as possible.

Rules:
- Extract data based ONLY on the content provided below.
- If multiple instances of the requested data exist, extract ALL of them —
  do not stop after the first match.
- If a value is missing, incomplete, or ambiguous, note it rather than fabricating.
- If the content doesn't contain the requested information at all, return an
  empty result.
- If a schema is provided, respond with valid JSON matching that schema exactly.
- Organise extracted data clearly. If no schema is provided, format your answer
  in clean markdown with structure (tables, lists, sections as appropriate)."""

# Per-source Markdown truncation for the LLM context block.
DOCUMENT_MAX_CHARS = 8000
# Concurrent scrape budget and per-URL timeout for extract jobs.
MAX_CONCURRENT = 5
URL_TIMEOUT_SECONDS = 70


def _validate_json_if_schema(answer: str, schema: dict | None) -> None:
    """Reject non-JSON or schema-invalid structured output."""
    if not schema:
        return
    try:
        cleaned = answer.strip()
        cleaned = cleaned.removeprefix("```json")
        cleaned = cleaned.removeprefix("```")
        cleaned = cleaned.removesuffix("```")
        value = json.loads(cleaned)
        validator_cls = validator_for(schema)
        validator_cls.check_schema(schema)
        validator_cls(schema).validate(value)
    except (
        json.JSONDecodeError,
        jsonschema.exceptions.SchemaError,
        jsonschema.exceptions.ValidationError,
        Unresolvable,
        TypeError,
        AttributeError,
        KeyError,
    ) as exc:
        logger.warning("LLM response failed structured-output validation: %s", exc)
        raise StructuredOutputError(
            detail="LLM response did not satisfy the requested output schema"
        ) from exc


async def _scrape_one(
    url: str,
    scraper: ScraperClient,
) -> dict | None:
    """Scrape one URL and return ``{url, markdown, source, char_count}`` or None.

    Barrier-flagged payloads (challenge/interstitial content) are refused so
    they never reach the LLM (#586) — mirrors the full edition's behaviour.
    """
    from .barrier_guard import is_barrier_flagged, log_refusal

    try:
        logger.info("Scraping: %s", url)
        result = await asyncio.wait_for(
            scraper.scrape_with_fallback(url),
            timeout=URL_TIMEOUT_SECONDS,
        )
        if result.get("success") and result.get("data", {}).get("markdown"):
            if is_barrier_flagged(result):
                log_refusal(url, result)
                return None
            md = result["data"]["markdown"]
            return {
                "url": url,
                "markdown": md,
                "source": result["data"].get("source", "unknown"),
                "char_count": len(md),
            }
        logger.warning("Failed to scrape %s: %s", url, result.get("error"))
        return None
    except TimeoutError:
        logger.warning("Timeout scraping %s after %ss", url, URL_TIMEOUT_SECONDS)
        return None
    except Exception as e:
        logger.warning("Error scraping %s: %s", url, e)
        return None


async def run_extract(
    urls: list[str],
    prompt: str | None = None,
    schema: dict | None = None,
    scraper_url: str = "http://127.0.0.1:8001",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_api_key: str = "",
    llm_model: str | None = None,
) -> dict:
    """Extract structured data from given URLs. No search step."""
    if llm_model is None:
        raise ValueError("llm_model is required — set via LLM_MODEL env var")
    scraper = ScraperClient(scraper_url)
    llm = LLMClient(llm_base_url, llm_api_key, llm_model)

    try:
        results = await asyncio.gather(
            *(_scrape_one(url, scraper) for url in urls), return_exceptions=True
        )
        artifacts = [r for r in results if isinstance(r, dict)]
        source_details = [
            {"url": a["url"], "source": a["source"], "char_count": a["char_count"]}
            for a in artifacts
        ]
        documents = [
            f"Source: {a['url']}\n\n{a['markdown'][:DOCUMENT_MAX_CHARS]}"
            for a in artifacts
        ]
        context = "\n\n---\n\n".join(documents) if documents else ""

        if not context:
            return {
                "result": "No content could be extracted from the provided URLs.",
                "sources": [],
                "source_details": [],
            }

        user_prompt = (
            prompt or "Extract the requested information from the provided content."
        )
        answer = await llm.generate(
            system_prompt=EXTRACT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            context=context,
            schema=schema,
            stage="extract",
        )
        _validate_json_if_schema(answer, schema)
        return {
            "result": answer,
            "sources": [s["url"] for s in source_details],
            "source_details": source_details,
        }
    finally:
        await scraper.close()
        await llm.close()
