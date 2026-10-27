"""
Load orchestration against an in-memory repository with real transaction
semantics (snapshot on begin, restore on exception). The same flow runs
against PostGIS in tests/water_protection/integration.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from typing import Any

import pytest

from app.water_protection.errors import PersistenceError
from app.water_protection.geometry import GeometryPolicy
from app.water_protection.loader import load_into_staging
from app.water_protection.loader import plan_batch
from app.water_protection.mapping import LayerMapping
from app.water_protection.pipeline import ProcessingResult
from app.water_protection.pipeline import process_snapshot
from app.water_protection.pipeline import retrieve_from_file
from app.water_protection.records import QuarantinedFeature
from app.water_protection.records import StagedZone
from app.water_protection.report import IngestionReport
from tests.water_protection.conftest import FIXTURES

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


class InMemoryStaging:
    def __init__(self) -> None:
        self.zones: dict[tuple[str, str], dict[str, Any]] = {}
        self.quarantine: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.fail_on_upsert_call: int | None = None
        self.drop_rows_on_upsert = False
        self.upsert_calls = 0
        self.locked = False

    @contextmanager
    def transaction(self) -> Iterator[InMemoryStaging]:
        saved = copy.deepcopy((self.zones, self.quarantine))
        try:
            yield self
        except BaseException:
            self.zones, self.quarantine = saved
            raise
        finally:
            self.locked = False

    def acquire_load_lock(self) -> None:
        self.locked = True

    def source_ids(self, state_code: str) -> set[str]:
        return {key for state, key in self.zones if state == state_code}

    def content_hashes(self, state_code: str, source_ids: Sequence[str]) -> dict[str, str]:
        return {k: self.zones[(state_code, k)]["content_hash"] for k in source_ids if (state_code, k) in self.zones}

    def upsert_zones(self, rows: Sequence[dict[str, Any]]) -> int:
        assert self.locked
        self.upsert_calls += 1
        if self.upsert_calls == self.fail_on_upsert_call:
            raise RuntimeError("simulated database failure")
        written = 0
        for row in rows:
            key = (row["state_code"], row["source_zone_id"])
            if self.zones.get(key, {}).get("content_hash") != row["content_hash"]:
                self.zones[key] = dict(row)
                written += 1
        return written - 1 if self.drop_rows_on_upsert and written else written

    def mark_seen(self, state_code: str, source_ids: Sequence[str], seen_at: datetime, run_id: uuid.UUID) -> int:
        for key in source_ids:
            self.zones[(state_code, key)]["last_seen_run_id"] = run_id
        return len([k for k in source_ids if (state_code, k) in self.zones])

    def upsert_quarantine(self, rows: Sequence[dict[str, Any]]) -> int:
        for row in rows:
            key = (row["state_code"], row["record_key"], row["reason_code"])
            previous = self.quarantine.get(key)
            row = dict(row, occurrences=(previous["occurrences"] + 1) if previous else 1)
            self.quarantine[key] = row
        return len(rows)


def zone_row(zone: StagedZone, run_id: uuid.UUID, seen_at: datetime) -> dict[str, Any]:
    return {
        "state_code": zone.attributes.state_code,
        "source_zone_id": zone.key,
        "content_hash": zone.content_hash,
        "zone_category": zone.attributes.zone_category,
        "last_ingestion_run_id": run_id,
    }


def quarantine_row(item: QuarantinedFeature, run_id: uuid.UUID, seen_at: datetime) -> dict[str, Any]:
    return {"state_code": item.state_code, "record_key": item.record_key, "reason_code": item.reason_code}


@pytest.fixture
def processed(mapping: LayerMapping) -> ProcessingResult:
    snapshot = retrieve_from_file(FIXTURES / "getfeature_edge_cases.xml", mapping, default_srs=None)
    return process_snapshot(snapshot, mapping, GeometryPolicy(), IngestionReport(mode="load"))


def load(
    db: InMemoryStaging,
    zones: Sequence[StagedZone],
    quarantined: Sequence[QuarantinedFeature] = (),
    batch_size: int = 4,
):
    return load_into_staging(
        db.transaction,
        zones,
        quarantined,
        state_code="TH",
        run_id=uuid.uuid4(),
        seen_at=NOW,
        batch_size=batch_size,
        zone_row=zone_row,
        quarantine_row=quarantine_row,
    )


def test_repeated_load_is_idempotent(processed: ProcessingResult) -> None:
    db = InMemoryStaging()

    first = load(db, processed.zones, processed.quarantined)
    second = load(db, processed.zones, processed.quarantined)

    assert (first.inserted, first.updated, first.unchanged) == (9, 0, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 9)
    assert first.batches == second.batches == 3
    assert len(db.zones) == 9
    assert len(db.quarantine) == 8  # keyed, so a second run adds no rows
    assert {row["occurrences"] for row in db.quarantine.values()} == {2}


def test_changed_zone_is_updated_and_nothing_else(processed: ProcessingResult) -> None:
    db = InMemoryStaging()
    load(db, processed.zones)

    changed = [replace(z, content_hash="f" * 64) if z.key == "TEST-Z-002" else z for z in processed.zones]
    summary = load(db, changed)

    assert (summary.inserted, summary.updated, summary.unchanged) == (0, 1, 8)
    assert db.zones[("TH", "TEST-Z-002")]["content_hash"] == "f" * 64


def test_failure_in_a_later_batch_rolls_back_the_whole_load(processed: ProcessingResult) -> None:
    db = InMemoryStaging()
    db.fail_on_upsert_call = 2

    with pytest.raises(RuntimeError, match="simulated database failure"):
        load(db, processed.zones, processed.quarantined)

    assert db.zones == {}  # batch 1 was written, then rolled back
    assert db.quarantine == {}


def test_reconciliation_mismatch_aborts_and_rolls_back(processed: ProcessingResult) -> None:
    db = InMemoryStaging()
    load(db, processed.zones[:2])
    before = copy.deepcopy(db.zones)
    db.drop_rows_on_upsert = True

    with pytest.raises(PersistenceError, match="expected to write"):
        load(db, processed.zones)

    assert db.zones == before


def test_rows_missing_from_the_source_are_reported_not_deleted(processed: ProcessingResult) -> None:
    db = InMemoryStaging()
    load(db, processed.zones)

    rejected_now = replace(processed.quarantined[0], source_zone_id="TEST-Z-003", record_key="TEST-Z-003")
    remaining = [z for z in processed.zones if z.key not in {"TEST-Z-003", "TEST-Z-004"}]
    summary = load(db, remaining, [rejected_now])

    assert summary.absent_from_source == ["TEST-Z-004"]
    assert summary.stale_after_rejection == ["TEST-Z-003"]
    assert ("TH", "TEST-Z-004") in db.zones  # kept for review
    assert summary.to_dict()["absent_from_source"] == 1


def test_duplicate_keys_in_one_batch_are_refused(processed: ProcessingResult) -> None:
    with pytest.raises(PersistenceError, match="duplicate source_zone_id"):
        plan_batch([processed.zones[0], processed.zones[0]], {})


def test_plan_batch_classifies_rows(processed: ProcessingResult) -> None:
    a, b, c = processed.zones[:3]
    plan = plan_batch([a, b, c], {a.key: a.content_hash, b.key: "0" * 64})
    assert plan.unchanged == (a,)
    assert plan.updates == (b,)
    assert plan.inserts == (c,)
