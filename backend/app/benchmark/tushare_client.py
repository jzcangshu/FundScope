"""Minimal Tushare Pro HTTP API client using stdlib urllib."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TUSHARE_API_URL = "http://api.tushare.pro"
REQUEST_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


class TushareError(Exception):
    """Raised when the Tushare API returns a non-zero code."""


class TushareClient:
    """Thin wrapper around the Tushare Pro HTTP JSON API."""

    def __init__(self, token: str, *, api_url: str = TUSHARE_API_URL) -> None:
        if not token:
            raise ValueError("Tushare token is required")
        self._token = token
        self._api_url = api_url

    def query(
        self,
        api_name: str,
        *,
        params: dict[str, Any] | None = None,
        fields: str = "",
    ) -> list[dict[str, Any]]:
        """Execute a Tushare API query and return rows as dicts.

        Raises TushareError on API-level errors and OSError on network issues.
        """

        body = json.dumps(
            {
                "api_name": api_name,
                "token": self._token,
                "params": params or {},
                "fields": fields,
            }
        ).encode("utf-8")

        request = Request(
            self._api_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise TushareError(f"Network error: {exc}") from exc

        code = raw.get("code")
        if code != 0:
            msg = raw.get("msg") or "Unknown Tushare error"
            raise TushareError(f"Tushare API error (code={code}): {msg}")

        data = raw.get("data")
        if data is None:
            return []

        fields_list: list[str] = data.get("fields", [])
        items: list[list[Any]] = data.get("items", [])
        return [dict(zip(fields_list, row)) for row in items]

    def index_daily(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        """Fetch daily OHLC for an index between two dates (inclusive).

        Dates are converted to Tushare's YYYYMMDD format.  Results are sorted
        by trade_date ascending.
        """

        rows = self.query(
            "index_daily",
            params={
                "ts_code": ts_code,
                "start_date": start_date.strftime("%Y%m%d"),
                "end_date": end_date.strftime("%Y%m%d"),
            },
            fields="trade_date,close,pct_chg",
        )
        # Tushare returns descending by trade_date; sort ascending.
        rows.sort(key=lambda r: r.get("trade_date", ""))
        return rows


__all__ = ["TushareClient", "TushareError"]
