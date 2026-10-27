from __future__ import annotations

from typing import Any

import pytest
import requests
import responses

from app.water_protection.config import WfsSettings
from app.water_protection.errors import HttpRequestError
from app.water_protection.errors import HttpStatusError
from app.water_protection.errors import ResponseTooLargeError
from app.water_protection.errors import WfsServiceException
from app.water_protection.wfs.http import WfsHttpClient
from app.water_protection.wfs.http import redact_url
from tests.water_protection.conftest import WFS_URL
from tests.water_protection.conftest import fixture_bytes

PARAMS = {"SERVICE": "WFS", "REQUEST": "GetCapabilities"}


@responses.activate
def test_transient_status_is_retried_with_exponential_backoff(http: WfsHttpClient, sleeps: list[float]) -> None:
    responses.add(responses.GET, WFS_URL, status=503)
    responses.add(responses.GET, WFS_URL, status=503)
    responses.add(responses.GET, WFS_URL, body=b"<ok/>", status=200)

    response = http.get(WFS_URL, PARAMS, operation="GetCapabilities")

    assert response.content == b"<ok/>"
    assert len(responses.calls) == 3
    # jitter() == 0 -> half of each exponential step: 0.01/2, 0.02/2
    assert sleeps == pytest.approx([0.005, 0.01])


@responses.activate
def test_gives_up_after_bounded_retries(http: WfsHttpClient, sleeps: list[float]) -> None:
    responses.add(responses.GET, WFS_URL, status=503)

    with pytest.raises(HttpStatusError) as excinfo:
        http.get(WFS_URL, PARAMS, operation="GetCapabilities")

    assert excinfo.value.status_code == 503
    assert "after 3 attempt(s)" in str(excinfo.value)
    assert len(responses.calls) == 3  # max_retries=2 -> 3 attempts
    assert len(sleeps) == 2


@responses.activate
def test_timeouts_are_retried_then_reported(http: WfsHttpClient) -> None:
    responses.add(responses.GET, WFS_URL, body=requests.ConnectTimeout("connect timed out"))

    with pytest.raises(HttpRequestError, match="transport failure after 3 attempt"):
        http.get(WFS_URL, PARAMS, operation="GetCapabilities")
    assert len(responses.calls) == 3


@responses.activate
def test_read_timeout_then_success(http: WfsHttpClient) -> None:
    responses.add(responses.GET, WFS_URL, body=requests.ReadTimeout("read timed out"))
    responses.add(responses.GET, WFS_URL, body=b"<ok/>")

    assert http.get(WFS_URL, PARAMS, operation="GetCapabilities").content == b"<ok/>"


def test_connect_and_read_timeouts_are_passed_separately(settings: WfsSettings) -> None:
    captured: dict[str, Any] = {}

    class RecordingSession(requests.Session):
        def get(self, url: str | bytes, **kwargs: Any) -> requests.Response:  # type: ignore[override]
            captured.update(kwargs)
            response = requests.Response()
            response.status_code = 200
            response.raw = __import__("io").BytesIO(b"<ok/>")
            return response

    custom = settings.model_copy(update={"connect_timeout_s": 3.0, "read_timeout_s": 45.0})
    WfsHttpClient(custom, RecordingSession()).get(WFS_URL, PARAMS, operation="GetCapabilities")

    assert captured["timeout"] == (3.0, 45.0)
    assert captured["stream"] is True


@responses.activate
def test_exception_report_with_client_error_is_not_retried(http: WfsHttpClient) -> None:
    responses.add(responses.GET, WFS_URL, body=fixture_bytes("exception_report.xml"), status=400)

    with pytest.raises(WfsServiceException) as excinfo:
        http.get(WFS_URL, PARAMS, operation="GetFeature")

    assert excinfo.value.code == "InvalidParameterValue"
    assert excinfo.value.locator == "typeNames"
    assert excinfo.value.status_code == 400
    assert len(responses.calls) == 1


@responses.activate
def test_server_error_with_exception_report_is_not_retried(http: WfsHttpClient) -> None:
    responses.add(responses.GET, WFS_URL, body=fixture_bytes("exception_report.xml"), status=500)

    with pytest.raises(WfsServiceException):
        http.get(WFS_URL, PARAMS, operation="GetFeature")
    assert len(responses.calls) == 1


@responses.activate
def test_plain_server_error_is_retried(http: WfsHttpClient) -> None:
    responses.add(responses.GET, WFS_URL, body=b"Internal Server Error", status=500)
    responses.add(responses.GET, WFS_URL, body=b"<ok/>")

    assert http.get(WFS_URL, PARAMS, operation="GetFeature").content == b"<ok/>"
    assert len(responses.calls) == 2


@responses.activate
def test_retry_after_header_is_honoured_within_limit(http: WfsHttpClient, sleeps: list[float]) -> None:
    responses.add(responses.GET, WFS_URL, status=429, headers={"Retry-After": "0.04"})
    responses.add(responses.GET, WFS_URL, body=b"<ok/>")

    http.get(WFS_URL, PARAMS, operation="GetCapabilities")

    assert sleeps == pytest.approx([0.04])


@responses.activate
def test_response_size_limit(settings: WfsSettings) -> None:
    responses.add(responses.GET, WFS_URL, body=b"x" * 2048)
    small = WfsHttpClient(settings.model_copy(update={"max_response_bytes": 1024}), requests.Session())

    with pytest.raises(ResponseTooLargeError):
        small.get(WFS_URL, PARAMS, operation="GetFeature")
    assert len(responses.calls) == 1  # not retried


@responses.activate
def test_minimum_interval_between_requests(settings: WfsSettings) -> None:
    responses.add(responses.GET, WFS_URL, body=b"<ok/>")
    clock = iter([100.0, 100.2, 101.0])
    waits: list[float] = []
    client = WfsHttpClient(
        settings.model_copy(update={"min_request_interval_s": 1.0}),
        requests.Session(),
        sleep=waits.append,
        monotonic=lambda: next(clock),
    )

    client.get(WFS_URL, PARAMS, operation="first")
    client.get(WFS_URL, PARAMS, operation="second")

    assert waits == pytest.approx([0.8])


def test_redact_url_removes_credentials() -> None:
    url = "https://user:s3cret@wfs.test:8443/wsg?SERVICE=WFS&token=abc123&apikey=k"
    redacted = redact_url(url)
    assert "s3cret" not in redacted
    assert "abc123" not in redacted
    assert "user" not in redacted
    assert redacted == "https://wfs.test:8443/wsg?SERVICE=WFS&token=***&apikey=***"


@responses.activate
def test_error_messages_name_the_operation_without_secrets(settings: WfsSettings) -> None:
    secret_url = "https://wfs.test/wsg"
    responses.add(responses.GET, secret_url, status=404)
    client = WfsHttpClient(settings, requests.Session(), sleep=lambda _: None)

    with pytest.raises(HttpStatusError) as excinfo:
        client.get(secret_url, {"REQUEST": "GetFeature", "token": "abc123"}, operation="GetFeature[startIndex=0]")

    message = str(excinfo.value)
    assert message.startswith("GetFeature[startIndex=0]: HTTP 404")
    assert "abc123" not in message
    assert "token=***" in message
