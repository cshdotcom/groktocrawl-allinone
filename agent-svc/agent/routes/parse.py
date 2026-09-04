"""Parse route handlers — file upload and content extraction."""

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Request

from ..exceptions import InvalidRequestError, NotFoundError, UpstreamError
from ..models import ParseResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# 单容器(all-in-one)内 parse-svc 固定监听 127.0.0.1:8013, 入口点亦导出同名
# 环境变量; 外部实例用 PARSE_SVC_URL 覆盖。旧多镜像默认值 parse-svc:8013
# 在单容器内不可解析(DNS Name not known → /v2/parse 500), 故必须从环境读取。
PARSE_SVC_URL = os.environ.get("PARSE_SVC_URL", "http://127.0.0.1:8013")
PARSE_UPLOAD_TTL = 3 * 60 * 60  # 3 hours, matches parse-svc/config.py

# Lua script: atomically get and delete the upload data.
# Prevents race conditions where two concurrent parse requests
# with the same upload_id both retrieve and process the file.
_ATOMIC_GETDEL_SCRIPT = """
local data = redis.call('GET', KEYS[1])
if data then
    redis.call('DEL', KEYS[1], KEYS[2], KEYS[3], KEYS[4])
end
return data
"""


@router.put("/v2/parse/upload/{upload_id}")
async def upload_parse_file(upload_id: str, request: Request) -> dict[str, Any]:
    """Upload file bytes for a previously requested upload_id.

    Stores the raw bytes, content-type, and filename in Valkey.
    The content-type is read from the ``Content-Type`` request header.
    The filename is read from the ``X-Filename`` request header.
    """
    from redis import Redis

    r = Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=False)
    meta = r.get(f"parse:upload:{upload_id}")
    if meta is None:
        raise NotFoundError(
            detail="Upload ID not found or expired",
            details={"upload_id": upload_id},
        )

    raw_body = await request.body()
    if not raw_body:
        raise InvalidRequestError(detail="Empty body — no file data received")

    content_type = request.headers.get("Content-Type", "application/octet-stream")
    filename = request.headers.get("X-Filename", "uploaded_file")

    pipe = r.pipeline()
    pipe.set(f"parse:upload:{upload_id}:data", raw_body, ex=PARSE_UPLOAD_TTL)
    pipe.set(
        f"parse:upload:{upload_id}:content_type", content_type, ex=PARSE_UPLOAD_TTL
    )
    pipe.set(f"parse:upload:{upload_id}:filename", filename, ex=PARSE_UPLOAD_TTL)
    pipe.set(f"parse:upload:{upload_id}", b"uploaded", ex=PARSE_UPLOAD_TTL)
    pipe.execute()

    return {"status": "uploaded", "upload_id": upload_id}


@router.post("/v2/parse", response_model=ParseResponse)
async def parse_file(request: Request) -> Any:
    """Upload a file and get its content as markdown.

    Supports two modes:

    - Direct: multipart form with ``file`` field (small files)
    - Two-step: form field ``upload_id`` referencing a pre-uploaded file

    ``ocr=hosted`` is an explicit opt-in and is forwarded only when the caller
    requests hosted OCR. The parse service remains local-first by default.
    """
    form = await request.form()
    form_ocr = form.get("ocr", "local")
    ocr = form_ocr if isinstance(form_ocr, str) else "local"
    if ocr not in {"local", "hosted"}:
        raise InvalidRequestError(detail="ocr must be 'local' or 'hosted'")

    # Two-step mode: retrieve pre-uploaded file from Valkey
    upload_id_raw = form.get("upload_id")
    upload_id_str = upload_id_raw if isinstance(upload_id_raw, str) else None
    if upload_id_str:
        from redis import Redis

        r = Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=False)
        data_key = f"parse:upload:{upload_id_str}:data"
        ct_key = f"parse:upload:{upload_id_str}:content_type"
        fn_key = f"parse:upload:{upload_id_str}:filename"
        meta_key = f"parse:upload:{upload_id_str}"
        getdel = r.register_script(_ATOMIC_GETDEL_SCRIPT)
        content = getdel(keys=[data_key, ct_key, fn_key, meta_key])
        if content is None:
            raise InvalidRequestError(
                detail="Upload data not found or expired",
                details={"upload_id": upload_id_str},
            )
        # These keys were deleted atomically by Lua; try to read headers
        # from a fresh GET — they'll be None if the Lua script already
        # deleted them (normal case).
        fn_val = r.get(fn_key)
        filename = fn_val.decode() if isinstance(fn_val, bytes) else "uploaded_file"
        ct_val = r.get(ct_key)
        content_type = (
            ct_val.decode() if isinstance(ct_val, bytes) else "application/octet-stream"
        )
        # Clean up any remaining keys (should already be deleted by Lua)
        r.delete(meta_key, ct_key, fn_key)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{PARSE_SVC_URL}/parse",
                files={"file": (filename, content, content_type)},
                data={"ocr": ocr},
            )
            try:
                return resp.json()
            except Exception:
                raise UpstreamError(
                    detail="Parse service returned invalid response",
                    details={"status_code": resp.status_code},
                )

    # Direct mode: file in multipart form
    if "file" not in form:
        raise InvalidRequestError(
            detail="No file provided. Use multipart form with 'file' field."
        )

    upload = form["file"]  # type: ignore[union-attr]
    content = await upload.read()  # type: ignore[union-attr]
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{PARSE_SVC_URL}/parse",
            files={
                "file": (
                    upload.filename or "file",  # type: ignore[union-attr]
                    content,
                    upload.content_type or "application/octet-stream",  # type: ignore[union-attr]
                )
            },
            data={"ocr": ocr},
        )
        try:
            return resp.json()
        except Exception:
            raise UpstreamError(
                detail=f"Parse service error: {resp.text[:200]}"
            ) from None
