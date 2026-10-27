from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import geopandas as gpd
import pytest
import responses

from app.water_protection import cli
from tests.water_protection.conftest import FIXTURES
from tests.water_protection.conftest import WFS_URL
from tests.water_protection.conftest import FakeWfs


@pytest.fixture(autouse=True)
def fast_wfs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env from the developer's checkout
    monkeypatch.setenv("WSG_WFS_URL", WFS_URL)
    monkeypatch.setenv("WSG_MIN_REQUEST_INTERVAL_S", "0")
    monkeypatch.setenv("WSG_MAX_RETRIES", "0")


def test_validate_with_saved_response(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report_path = tmp_path / "out" / "report.json"
    export_path = tmp_path / "out" / "zones.gpkg"

    code = cli.main(
        [
            "--log-level",
            "WARNING",
            "validate",
            "--source-file",
            str(FIXTURES / "getfeature_edge_cases.xml"),
            "--report",
            str(report_path),
            "--export",
            str(export_path),
        ]
    )

    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert code == 3  # completed, some features rejected
    assert printed == saved
    assert saved["status"] == "succeeded_with_rejections"
    assert saved["counts"]["accepted"] == 9
    assert saved["counts"]["exported"] == 9
    assert saved["database"] is None
    assert len(gpd.read_file(export_path)) == 9


@responses.activate
def test_validate_against_mocked_wfs(
    fake_wfs: Callable[[], FakeWfs], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_wfs()
    monkeypatch.setenv("WSG_PAGE_SIZE", "2")

    code = cli.main(["--log-level", "WARNING", "validate"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["status"] == "succeeded"
    assert report["source"]["pages"] == 3
    assert report["counts"]["accepted"] == 5


@responses.activate
def test_inspect_records_capabilities_schema_and_sample(
    fake_wfs: Callable[[], FakeWfs], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_wfs()
    fake.single = (FIXTURES / "getfeature_page_0.xml").read_bytes()
    output = tmp_path / "inspection.json"

    code = cli.main(["--log-level", "WARNING", "inspect", "--output", str(output), "--sample-size", "2"])

    inspection = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert inspection["capabilities"]["version"] == "2.0.0"
    assert inspection["capabilities"]["implements_result_paging"] is True
    assert inspection["layer_schema"]["element_name"] == "th_wsg_zonen"
    assert inspection["mapping_check"] == {"matches": True, "unmapped_properties": ["bemerkung"], "warnings": []}
    assert [s["gml_id"] for s in inspection["sample"]] == ["th_wsg_zonen.1", "th_wsg_zonen.2"]
    sample_request = [r for r in fake.requests_named("GetFeature") if "STARTINDEX" not in r][0]
    assert sample_request["COUNT"] == "2"
    assert json.loads(capsys.readouterr().out) == inspection


@responses.activate
def test_inspect_reports_bot_protection(fake_wfs: Callable[[], FakeWfs], capsys: pytest.CaptureFixture[str]) -> None:
    fake = fake_wfs()
    fake.capabilities = (FIXTURES / "html_challenge.html").read_bytes()

    code = cli.main(["--log-level", "WARNING", "inspect"])

    inspection = json.loads(capsys.readouterr().out)
    assert code == 1
    assert inspection["error"]["type"] == "UnexpectedContentError"
    assert inspection["error"]["operation"] == "GetCapabilities"


def test_load_without_staging_configuration_is_refused() -> None:
    assert cli.main(["load", "--source-file", str(FIXTURES / "getfeature_edge_cases.xml")]) == 2


def test_invalid_settings_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSG_WFS_URL", "http://insecure.example/wfs")
    assert cli.main(["validate"]) == 2
