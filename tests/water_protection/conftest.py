from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
import requests
import responses

from app.water_protection.config import WfsSettings
from app.water_protection.mapping import LayerMapping
from app.water_protection.mapping import load_mapping
from app.water_protection.wfs.client import WfsClient
from app.water_protection.wfs.http import WfsHttpClient

FIXTURES = Path(__file__).parent / "fixtures"
WFS_URL = "https://wfs.test/wsg"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep developer environment variables out of the tests."""
    import os

    for key in list(os.environ):
        if key.startswith(("WSG_", "IRIS_")):
            monkeypatch.delenv(key)


@pytest.fixture
def settings() -> WfsSettings:
    return WfsSettings(
        _env_file=None,
        wfs_url=WFS_URL,
        page_size=2,
        max_retries=2,
        backoff_base_s=0.01,
        backoff_max_s=0.05,
        min_request_interval_s=0,
    )


@pytest.fixture
def mapping() -> LayerMapping:
    return load_mapping()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def http(settings: WfsSettings, sleeps: list[float]) -> WfsHttpClient:
    return WfsHttpClient(settings, requests.Session(), sleep=sleeps.append, jitter=lambda: 0.0)


@pytest.fixture
def client(http: WfsHttpClient) -> WfsClient:
    return WfsClient(http, WFS_URL)


class FakeWfs:
    """
    Routes mocked WFS requests by their KVP parameters.

    ``pages`` maps STARTINDEX to a fixture body; ``overrides`` maps a
    request name (GetCapabilities, DescribeFeatureType, hits, GetFeature)
    to a (status, body) pair.
    """

    def __init__(self) -> None:
        self.capabilities = fixture_bytes("capabilities_wfs200.xml")
        self.schema = fixture_bytes("describe_feature_type.xsd")
        self.hits = fixture_bytes("getfeature_hits.xml")
        self.pages: dict[int, bytes] = {
            0: fixture_bytes("getfeature_page_0.xml"),
            2: fixture_bytes("getfeature_page_2.xml"),
            4: fixture_bytes("getfeature_page_4.xml"),
        }
        self.single: bytes | None = None
        self.overrides: dict[str, tuple[int, bytes]] = {}
        self.requests: list[dict[str, str]] = []

    def __call__(self, request: requests.PreparedRequest) -> tuple[int, dict[str, str], bytes]:
        query = {k.upper(): v[0] for k, v in parse_qs(urlsplit(request.url or "").query).items()}
        self.requests.append(query)
        name = query.get("REQUEST", "")
        if name == "GetFeature" and query.get("RESULTTYPE") == "hits":
            name = "hits"
        if name in self.overrides:
            status, body = self.overrides[name]
            return status, {"Content-Type": "application/xml"}, body
        if name == "GetCapabilities":
            body = self.capabilities
        elif name == "DescribeFeatureType":
            body = self.schema
        elif name == "hits":
            body = self.hits
        elif name == "GetFeature" and "STARTINDEX" in query:
            body = self.pages.get(int(query["STARTINDEX"]), fixture_bytes("getfeature_empty.xml"))
        elif name == "GetFeature":
            body = self.single or fixture_bytes("getfeature_empty.xml")
        else:
            return 400, {}, b"unexpected request"
        return 200, {"Content-Type": "application/gml+xml; version=3.2"}, body

    def requests_named(self, name: str) -> list[dict[str, str]]:
        return [r for r in self.requests if r.get("REQUEST") == name]


@pytest.fixture
def fake_wfs() -> Callable[[], FakeWfs]:
    def install() -> FakeWfs:
        fake = FakeWfs()
        responses.add_callback(responses.GET, WFS_URL, callback=fake)
        return fake

    return install
