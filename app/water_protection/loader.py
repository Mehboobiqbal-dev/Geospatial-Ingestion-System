"""
Idempotent load of processed zones into staging.

The whole load runs in one transaction (all batches, quarantine rows),
so a failure in any batch rolls everything back and the staging table
never holds a half-applied run. Within the transaction, rows are
written in batches to keep statements a reasonable size.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from itertools import islice
from typing import Any
from typing import Protocol

from app.water_protection.errors import PersistenceError
from app.water_protection.records import QuarantinedFeature
from app.water_protection.records import StagedZone

logger = logging.getLogger(__name__)

# A zone that disappears while a new one with (nearly) the same geometry
# appears was most likely renumbered at the source. Up to 1 % of the old
# area may differ, to allow for a re-digitised boundary.
RENUMBERING_MAX_AREA_DIFFERENCE = 0.01


class StagingRepository(Protocol):
    def acquire_load_lock(self) -> None: ...
    def source_ids(self, state_code: str) -> set[str]: ...
    def content_hashes(self, state_code: str, source_ids: Sequence[str]) -> dict[str, str]: ...
    def upsert_zones(self, rows: Sequence[dict[str, Any]]) -> int: ...
    def mark_seen(self, state_code: str, source_ids: Sequence[str], seen_at: datetime, run_id: uuid.UUID) -> int: ...
    def upsert_quarantine(self, rows: Sequence[dict[str, Any]]) -> int: ...
    def matching_geometries(
        self, state_code: str, old_ids: Sequence[str], new_ids: Sequence[str], max_area_difference: float
    ) -> list[tuple[str, str]]: ...


Transaction = Callable[[], AbstractContextManager[StagingRepository]]
RowBuilder = Callable[[Any, uuid.UUID, datetime], dict[str, Any]]


@dataclass(frozen=True)
class BatchPlan:
    inserts: tuple[StagedZone, ...]
    updates: tuple[StagedZone, ...]
    unchanged: tuple[StagedZone, ...]

    @property
    def writes(self) -> tuple[StagedZone, ...]:
        return self.inserts + self.updates


@dataclass
class LoadSummary:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    quarantined: int = 0
    batches: int = 0
    absent_from_source: list[str] = field(default_factory=list)
    stale_after_rejection: list[str] = field(default_factory=list)
    possible_renumbering: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "quarantined": self.quarantined,
            "batches": self.batches,
            "absent_from_source": len(self.absent_from_source),
            "absent_from_source_sample": sorted(self.absent_from_source)[:50],
            "stale_after_rejection": len(self.stale_after_rejection),
            "stale_after_rejection_sample": sorted(self.stale_after_rejection)[:50],
            "possible_renumbering": len(self.possible_renumbering),
            "possible_renumbering_sample": [
                {"absent_id": old, "new_id": new} for old, new in sorted(self.possible_renumbering)[:50]
            ],
        }


def plan_batch(batch: Sequence[StagedZone], existing_hashes: dict[str, str]) -> BatchPlan:
    keys = [zone.key for zone in batch]
    if len(keys) != len(set(keys)):
        # ON CONFLICT cannot touch the same row twice in one statement; the
        # pipeline de-duplicates earlier, so this indicates a bug.
        raise PersistenceError("duplicate source_zone_id within one batch")
    inserts, updates, unchanged = [], [], []
    for zone in batch:
        current = existing_hashes.get(zone.key)
        if current is None:
            inserts.append(zone)
        elif current != zone.content_hash:
            updates.append(zone)
        else:
            unchanged.append(zone)
    return BatchPlan(tuple(inserts), tuple(updates), tuple(unchanged))


def _batches(items: Sequence[StagedZone], size: int) -> Iterator[list[StagedZone]]:
    iterator = iter(items)
    while chunk := list(islice(iterator, size)):
        yield chunk


def load_into_staging(
    transaction: Transaction,
    zones: Sequence[StagedZone],
    quarantined: Sequence[QuarantinedFeature],
    *,
    state_code: str,
    run_id: uuid.UUID,
    seen_at: datetime,
    batch_size: int,
    zone_row: RowBuilder,
    quarantine_row: RowBuilder,
) -> LoadSummary:
    summary = LoadSummary()
    inserted_ids: list[str] = []

    with transaction() as repo:
        repo.acquire_load_lock()
        existing_ids = repo.source_ids(state_code)

        for batch in _batches(zones, batch_size):
            summary.batches += 1
            plan = plan_batch(batch, repo.content_hashes(state_code, [z.key for z in batch]))

            written = repo.upsert_zones([zone_row(z, run_id, seen_at) for z in plan.writes])
            if written != len(plan.writes):
                raise PersistenceError(
                    f"batch {summary.batches}: expected to write {len(plan.writes)} rows, wrote {written}"
                )
            touched = repo.mark_seen(state_code, [z.key for z in plan.unchanged], seen_at, run_id)
            if touched != len(plan.unchanged):
                raise PersistenceError(
                    f"batch {summary.batches}: expected {len(plan.unchanged)} unchanged rows, found {touched}"
                )

            inserted_ids.extend(z.key for z in plan.inserts)
            summary.inserted += len(plan.inserts)
            summary.updated += len(plan.updates)
            summary.unchanged += len(plan.unchanged)
            logger.info(
                "staging batch written",
                extra={
                    "batch": summary.batches,
                    "inserted": len(plan.inserts),
                    "updated": len(plan.updates),
                    "unchanged": len(plan.unchanged),
                },
            )

        if quarantined:
            summary.quarantined = repo.upsert_quarantine(
                [quarantine_row(item, run_id, seen_at) for item in quarantined]
            )

        # Nothing is deleted. Rows missing from the source, or whose source
        # feature was rejected this run, are reported for review instead.
        accepted_ids = {z.key for z in zones}
        rejected_ids = {q.source_zone_id for q in quarantined if q.source_zone_id}
        summary.absent_from_source = sorted(existing_ids - accepted_ids - rejected_ids)
        summary.stale_after_rejection = sorted(existing_ids & (rejected_ids - accepted_ids))
        if summary.absent_from_source and inserted_ids:
            summary.possible_renumbering = repo.matching_geometries(
                state_code, summary.absent_from_source, inserted_ids, RENUMBERING_MAX_AREA_DIFFERENCE
            )
            if summary.possible_renumbering:
                logger.warning(
                    "zones absent from the source match newly inserted zones; the source may have renumbered them",
                    extra={"pairs": len(summary.possible_renumbering)},
                )
    return summary
