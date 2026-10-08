"""Bound one multipart upload before allocating its complete temporary file."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

from fastapi import HTTPException, Request
from python_multipart.exceptions import FormParserError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

MULTIPART_OVERHEAD_BYTES = 64 * 1024


class _RequestTooLarge(MultiPartException):
    pass


@asynccontextmanager
async def bounded_upload(
    request: Request, *, max_file_bytes: int
) -> AsyncIterator[UploadFile]:
    """Call only after authorization; count the stream even without a length."""

    limit = max_file_bytes + MULTIPART_OVERHEAD_BYTES
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
            if length < 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="invalid_content_length"
            ) from exc
        if length > limit:
            raise HTTPException(status_code=413, detail="file_too_large")

    async def limited_stream() -> AsyncGenerator[bytes, None]:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                # MultiPartParser closes partial files on MultiPartException.
                raise _RequestTooLarge("file_too_large")
            yield chunk

    parser = MultiPartParser(
        request.headers, limited_stream(), max_files=1, max_fields=0
    )
    form = None
    try:
        try:
            form = await parser.parse()
        except _RequestTooLarge as exc:
            raise HTTPException(status_code=413, detail="file_too_large") from exc
        except MultiPartException as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        except (FormParserError, UnicodeError, LookupError, ClientDisconnect) as exc:
            raise HTTPException(
                status_code=400, detail="invalid_multipart_body"
            ) from exc
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=422, detail="file_required")
        yield upload
    finally:
        try:
            if form is not None:
                await form.close()
        finally:
            # Truncated input can leave spools outside FormData; cancellation
            # can also interrupt its async close. Always release all spools.
            for partial_file in parser._files_to_close_on_error:
                if not partial_file.closed:
                    partial_file.close()
