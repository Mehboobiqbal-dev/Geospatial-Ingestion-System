from __future__ import annotations

from collections.abc import Callable

import pytest
import responses

from app.water_protection.errors import IncompleteRetrievalError
from app.water_protection.errors import RetrievalLimitExceeded
from app.water_protection.errors import UnexpectedContentError
from app.water_protection.errors import WfsServiceException
from app.water_protection.wfs.client import WfsClient
from tests.water_protection.conftest import FakeWfs
from tests.water_protection.conftest import fixture_bytes
from tests.water_protection.gml_builder import collection

FakeInstaller = Callable[[], FakeWfs]


def fetch(client: WfsClient, *, page_size: int = 2, max_features: int = 100, max_pages: int = 10):
    capabilities, _ = client.get_capabilities()
    plan = client.plan(capabilities, "th_wsg_zonen", page_size=page_size, sort_by="zone_id")
    return plan, client.fetch_all(plan, geometry_property="geom", max_features=max_features, max_pages=max_pages)


@responses.activate
def test_paged_retrieval_requests_every_page_in_a_stable_order(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()

    plan, result = fetch(client)

    assert [f.properties["zone_id"] for f in result.features] == [f"TEST-Z-00{i}" for i in range(1, 6)]
    assert result.number_matched == 5
    assert result.pages == 3
    assert result.duplicate_features_collapsed == 0

    pages = [r for r in fake.requests_named("GetFeature") if r.get("RESULTTYPE") != "hits"]
    assert [r["STARTINDEX"] for r in pages] == ["0", "2", "4"]
    first = pages[0]
    assert first["VERSION"] == "2.0.0"
    assert first["TYPENAMES"] == "wsg:th_wsg_zonen"
    assert first["NAMESPACES"] == "xmlns(wsg,https://example.invalid/wsg)"
    assert first["COUNT"] == "2"
    assert first["SORTBY"] == "zone_id ASC"
    assert first["OUTPUTFORMAT"] == "application/gml+xml; version=3.2"
    assert "SRSNAME" not in first  # data is requested in its declared CRS and transformed locally
    assert "STARTINDEX" not in result.request_url


@responses.activate
def test_empty_result_is_a_success(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.hits = fixture_bytes("getfeature_empty.xml")
    fake.pages = {0: fixture_bytes("getfeature_empty.xml")}

    _, result = fetch(client)

    assert result.features == ()
    assert result.number_matched == 0
    assert result.pages == 1


@responses.activate
def test_missing_page_is_detected(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    # Server stops early: startIndex=4 is empty although numberMatched still says 5.
    fake.pages[4] = collection([], number_matched=5, number_returned=0)

    with pytest.raises(IncompleteRetrievalError, match="reported 5 features but 4"):
        fetch(client)


@responses.activate
def test_number_matched_changing_while_paging_is_detected(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.pages[2] = fake.pages[2].replace(b'numberMatched="5"', b'numberMatched="6"')

    with pytest.raises(IncompleteRetrievalError, match="numberMatched changed from 5 to 6"):
        fetch(client)


@responses.activate
def test_number_returned_must_match_members(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.pages[0] = fake.pages[0].replace(b'numberReturned="2"', b'numberReturned="3"')

    with pytest.raises(IncompleteRetrievalError, match="numberReturned=3 but 2 members"):
        fetch(client)


@responses.activate
def test_repeated_feature_means_another_one_was_skipped(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.pages[2] = fake.pages[0].replace(b'numberMatched="5"', b'numberMatched="5"')  # page 2 repeats page 0

    with pytest.raises(IncompleteRetrievalError, match="2 repeated"):
        fetch(client)


@responses.activate
def test_conflicting_repeat_aborts(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.pages[2] = fake.pages[0].replace(b"Fassungsbereich", b"Geaendert")

    with pytest.raises(IncompleteRetrievalError, match="different content"):
        fetch(client)


@responses.activate
def test_unknown_total_pages_until_an_empty_page(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.overrides["hits"] = (400, fixture_bytes("exception_report.xml"))
    for start, body in list(fake.pages.items()):
        fake.pages[start] = body.replace(b'numberMatched="5"', b'numberMatched="unknown"')

    _, result = fetch(client)

    assert len(result.features) == 5
    assert result.number_matched is None
    assert result.pages == 4  # three data pages plus the empty page that ends paging


@responses.activate
def test_limit_is_enforced_before_downloading(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()

    with pytest.raises(RetrievalLimitExceeded, match="above the configured limit of 4"):
        fetch(client, max_features=4)
    assert all(r.get("RESULTTYPE") == "hits" for r in fake.requests_named("GetFeature"))


@responses.activate
def test_page_limit(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake_wfs()
    with pytest.raises(RetrievalLimitExceeded, match="page limit of 2"):
        fetch(client, max_pages=2)


@responses.activate
def test_server_page_cap_is_respected(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.capabilities = fake.capabilities.replace(
        b"<ows:DefaultValue>1000</ows:DefaultValue>", b"<ows:DefaultValue>2</ows:DefaultValue>"
    )

    plan, _ = fetch(client, page_size=500)
    assert plan.page_size == 2


@responses.activate
def test_wfs110_uses_one_bounded_request(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.capabilities = fixture_bytes("capabilities_wfs110.xml")
    fake.hits = collection([], number_matched=2, number_returned=0)
    fake.single = fake.pages[0].replace(b'numberMatched="5"', b'numberMatched="2"')

    plan, result = fetch(client, max_features=50)

    assert not plan.paging
    request = [r for r in fake.requests_named("GetFeature") if r.get("RESULTTYPE") != "hits"][0]
    assert request["TYPENAME"] == "wsg:th_wsg_zonen"
    assert request["VERSION"] == "1.1.0"
    assert request["MAXFEATURES"] == "51"
    assert "STARTINDEX" not in request
    assert [f.properties["zone_id"] for f in result.features] == ["TEST-Z-001", "TEST-Z-002"]


@responses.activate
def test_exception_report_on_getfeature(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.pages[0] = fixture_bytes("exception_report.xml")

    with pytest.raises(WfsServiceException) as excinfo:
        fetch(client)
    assert excinfo.value.operation == "GetFeature[startIndex=0]"


@responses.activate
def test_html_challenge_on_capabilities(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.capabilities = fixture_bytes("html_challenge.html")

    with pytest.raises(UnexpectedContentError, match="bot protection"):
        client.get_capabilities()


@responses.activate
def test_malformed_page(client: WfsClient, fake_wfs: FakeInstaller) -> None:
    fake = fake_wfs()
    fake.pages[2] = b"<wfs:FeatureCollection xmlns:wfs='http://www.opengis.net/wfs/2.0'><wfs:member>"

    with pytest.raises(UnexpectedContentError, match=r"GetFeature\[startIndex=2\]: response is not well-formed"):
        fetch(client)
