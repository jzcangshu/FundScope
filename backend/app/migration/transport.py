"""HTTP adapter for the existing import batch API."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.auth.dependencies import SESSION_COOKIE_NAME


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    remote_source_file_id: int | str | None
    duplicate: bool


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes


class ImportTransport(Protocol):
    def create_batch(self, source_type: str) -> int | str: ...

    def upload_file(
        self,
        batch_id: int | str,
        original_filename: str,
        stream: BinaryIO,
        file_size: int,
    ) -> UploadReceipt: ...

    def complete_batch(self, batch_id: int | str) -> None: ...


RequestFunction = Callable[[str, str, dict[str, str], bytes], HttpResponse]


class HttpTransportError(RuntimeError):
    """Stable, non-sensitive error raised by the HTTP adapter."""


class HttpImportTransport:
    """Call import endpoints with the same session cookie as the web client."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        request: RequestFunction | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url:
            raise ValueError("base URL is required")
        self.token = token
        self.timeout = timeout
        self._request = request or self._request_with_urllib

    def create_batch(self, source_type: str) -> int | str:
        payload = self._json_request(
            "POST",
            "/api/v1/imports",
            {"source_type": source_type},
            operation="create_batch",
        )
        try:
            data = cast(dict[str, object], payload["data"])
            return cast(int | str, data["id"])
        except (KeyError, TypeError) as exc:
            raise HttpTransportError("create_batch_invalid_response") from exc

    def upload_file(
        self,
        batch_id: int | str,
        original_filename: str,
        stream: BinaryIO,
        file_size: int,
    ) -> UploadReceipt:
        safe_filename = _basename(original_filename)
        content = stream.read()
        if len(content) != file_size:
            raise HttpTransportError("source_size_changed")
        boundary = "migration-" + secrets.token_hex(16)
        body = _multipart_body(boundary, safe_filename, content)
        payload = self._json_request(
            "POST",
            f"/api/v1/imports/{batch_id}/files",
            body,
            operation="upload_file",
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        try:
            data = cast(dict[str, object], payload["data"])
            return UploadReceipt(
                remote_source_file_id=cast(int | str | None, data.get("id")),
                duplicate=bool(data.get("duplicate", False)),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise HttpTransportError("upload_file_invalid_response") from exc

    def complete_batch(self, batch_id: int | str) -> None:
        self._json_request(
            "POST",
            f"/api/v1/imports/{batch_id}/complete",
            None,
            operation="complete_batch",
        )

    def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | bytes | None,
        *,
        operation: str,
        content_type: str = "application/json",
    ) -> dict[str, object]:
        if isinstance(payload, dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            body = payload or b""
        headers = {
            "Accept": "application/json",
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        if self.token:
            # The API deliberately has one authentication contract: fund_session.
            # Migration uses a short-lived value obtained from an authenticated login.
            headers["Cookie"] = f"{SESSION_COOKIE_NAME}={self.token}"
        response = self._request("POST", self.base_url + path, headers, body)
        if not 200 <= response.status_code < 300:
            raise HttpTransportError(f"{operation}_http_{response.status_code}")
        if not response.body:
            return {}
        try:
            result = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpTransportError(f"{operation}_invalid_json") from exc
        if not isinstance(result, dict):
            raise HttpTransportError(f"{operation}_invalid_response")
        return result

    def _request_with_urllib(
        self, method: str, url: str, headers: dict[str, str], body: bytes
    ) -> HttpResponse:
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return HttpResponse(response.status, response.read())
        except HTTPError as exc:
            return HttpResponse(exc.code, exc.read())
        except URLError as exc:
            raise HttpTransportError("network_error") from exc


def _basename(filename: str) -> str:
    if (
        not filename
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
    ):
        raise ValueError("upload filename must be a basename")
    return filename


def _multipart_body(boundary: str, filename: str, content: bytes) -> bytes:
    safe_filename = filename.replace('"', "'")
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    return b"".join((prefix, content, suffix))
