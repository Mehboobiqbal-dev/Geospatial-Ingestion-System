"""
HTTP transport for WFS requests.

The shared ``app.scrapers.base.HttpClient`` is not reused here because
WFS ingestion needs behaviour it does not provide: separate connect and
read timeouts, OWS exception reports read from non-2xx bodies (instead
of ``raise_for_status`` discarding them), a minimum interval between
requests, a response-size cap and injectable clocks for deterministic
tests.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import requests

from app.water_protection.config import WfsSettings
from app.water_protection.errors import HttpRequestError
from app.water_protection.errors import HttpStatusError
from app.water_protection.errors import ResponseTooLargeError
from app.water_protection.errors import WfsServiceException
from app.water_protection.wfs.xml_utils import try_exception_info

logger = logging.getLogger(__name__)

# 500 is retried only when the body is not an OWS exception report:
# many WFS servers use 500/400 for "your request is invalid", which a
# retry cannot fix.
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_SENSITIVE_PARAMS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "apikey",
        "api_key",
        "key",
        "secret",
        "auth",
        "signature",
        "sig",
    }
)


def redact_url(url: str) -> str:
    """Remove userinfo and mask credential-like query parameters."""

    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    query = urlencode(
        [
            (k, "***" if k.lower() in _SENSITIVE_PARAMS else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ],
        safe=":/,*",
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def build_url(base_url: str, params: Mapping[str, str]) -> str:
    request = requests.Request("GET", base_url, params=dict(params)).prepare()
    return request.url or base_url


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    content: bytes
    content_type: str
    url: str  # redacted


class WfsHttpClient:
    """Bounded, rate-limited GET client with exponential backoff and jitter."""

    def __init__(
        self,
        settings: WfsSettings,
        session: requests.Session | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "application/gml+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
            }
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._jitter = jitter
        self._last_request_at: float | None = None

    # =====================================================
    # PUBLIC
    # =====================================================

    def get(
        self,
        base_url: str,
        params: Mapping[str, str],
        *,
        operation: str,
    ) -> HttpResponse:
        url = redact_url(build_url(base_url, params))
        attempts = self.settings.max_retries + 1

        for attempt in range(1, attempts + 1):
            self._throttle()
            retry_after: float | None = None
            try:
                status, content, content_type, headers = self._fetch(base_url, params, operation=operation, url=url)
            except (requests.Timeout, requests.ConnectionError) as exc:
                failure = f"{type(exc).__name__}: {exc}"
                if attempt == attempts:
                    raise HttpRequestError(
                        f"transport failure after {attempt} attempt(s): {failure}",
                        operation=operation,
                        url=url,
                    ) from exc
            except requests.RequestException as exc:
                raise HttpRequestError(
                    f"request could not be sent: {type(exc).__name__}: {exc}",
                    operation=operation,
                    url=url,
                ) from exc
            else:
                if 200 <= status < 300:
                    return HttpResponse(status, content, content_type, url)

                report = try_exception_info(content)
                retryable = status in RETRYABLE_STATUS or (status == 500 and report is None)
                if not retryable or attempt == attempts:
                    if report is not None:
                        raise WfsServiceException(
                            operation=operation,
                            code=report.code,
                            locator=report.locator,
                            text=report.text,
                            status_code=status,
                            url=url,
                        )
                    suffix = f" after {attempt} attempt(s)" if retryable else ""
                    raise HttpStatusError(
                        f"HTTP {status}{suffix}",
                        operation=operation,
                        status_code=status,
                        url=url,
                    )
                failure = f"HTTP {status}"
                retry_after = _parse_retry_after(headers.get("Retry-After"))

            delay = self._backoff_delay(attempt, retry_after)
            logger.warning(
                "WFS request failed, retrying",
                extra={
                    "operation": operation,
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "failure": failure,
                    "delay_s": round(delay, 2),
                },
            )
            self._sleep(delay)

        raise AssertionError("unreachable")  # pragma: no cover

    # =====================================================
    # INTERNALS
    # =====================================================

    def _fetch(
        self,
        base_url: str,
        params: Mapping[str, str],
        *,
        operation: str,
        url: str,
    ) -> tuple[int, bytes, str, Mapping[str, str]]:
        response = self.session.get(
            base_url,
            params=dict(params),
            timeout=(self.settings.connect_timeout_s, self.settings.read_timeout_s),
            stream=True,
        )
        try:
            limit = self.settings.max_response_bytes
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise ResponseTooLargeError(
                    f"declared Content-Length {declared} exceeds limit {limit}",
                    operation=operation,
                    url=url,
                )
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ResponseTooLargeError(
                        f"response exceeds limit of {limit} bytes",
                        operation=operation,
                        url=url,
                    )
                chunks.append(chunk)
            return (
                response.status_code,
                b"".join(chunks),
                response.headers.get("Content-Type", ""),
                response.headers,
            )
        finally:
            response.close()

    def _throttle(self) -> None:
        interval = self.settings.min_request_interval_s
        now = self._monotonic()
        if self._last_request_at is not None and interval > 0:
            wait = interval - (now - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
                now = self._monotonic()
        self._last_request_at = now

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        # "Equal jitter": half of the exponential step is fixed, half random,
        # so retries spread out without collapsing to zero delay.
        cap = min(
            self.settings.backoff_max_s,
            self.settings.backoff_base_s * (2 ** (attempt - 1)),
        )
        delay = cap / 2 + self._jitter() * cap / 2
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.settings.backoff_max_s))
        return delay


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # HTTP-date form is ignored; normal backoff applies
    return seconds if seconds >= 0 else None
