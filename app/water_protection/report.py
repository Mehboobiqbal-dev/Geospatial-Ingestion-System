from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_WITH_REJECTIONS = 3

MAX_SAMPLES = 50


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class IngestionReport:
    """Everything an operator needs to judge a run, serialised as JSON."""

    mode: str
    run_id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    status: str = "running"
    source: dict[str, Any] = field(default_factory=dict)
    mapping: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    rejections: Counter[str] = field(default_factory=Counter)
    warnings: Counter[str] = field(default_factory=Counter)
    rejected_samples: list[dict[str, Any]] = field(default_factory=list)
    zone_categories: Counter[str] = field(default_factory=Counter)
    source_crs: Counter[str] = field(default_factory=Counter)
    transformations: set[str] = field(default_factory=set)
    schema: dict[str, Any] = field(default_factory=dict)
    database: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    notes: list[str] = field(default_factory=list)
    refused: bool = False

    def add_rejection(self, reason: str, key: str, detail: str) -> None:
        self.rejections[reason] += 1
        if len(self.rejected_samples) < MAX_SAMPLES:
            self.rejected_samples.append({"record_key": key, "reason": reason, "detail": detail[:500]})

    def fail(self, exc: BaseException, *, refused: bool = False) -> None:
        self.status = "failed"
        self.refused = refused
        self.error = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        self.finish()

    def finish(self) -> None:
        if self.status == "running":
            if self.counts.get("retrieved", 0) == 0:
                # A successful, verified empty result - not an error.
                self.status = "succeeded_empty"
            elif sum(self.rejections.values()):
                self.status = "succeeded_with_rejections"
            else:
                self.status = "succeeded"
        self.finished_at = utc_now()

    @property
    def exit_code(self) -> int:
        if self.status == "failed":
            return EXIT_REFUSED if self.refused else EXIT_FAILED
        if self.status == "succeeded_with_rejections":
            return EXIT_WITH_REJECTIONS
        return EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "source": self.source,
            "mapping": self.mapping,
            "schema": self.schema,
            "counts": self.counts,
            "zone_categories": dict(sorted(self.zone_categories.items())),
            "source_crs": dict(self.source_crs),
            "transformations": sorted(self.transformations),
            "rejections_by_reason": dict(sorted(self.rejections.items())),
            "warnings_by_code": dict(sorted(self.warnings.items())),
            "rejected_samples": self.rejected_samples,
            "database": self.database,
            "error": self.error,
            "notes": self.notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")

    def run_log_row(self) -> dict[str, Any]:
        database = self.database or {}
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at or utc_now(),
            "status": self.status,
            "source_url": self.source.get("request_url") or self.source.get("endpoint"),
            "source_layer": self.source.get("layer"),
            "mapping_version": self.mapping.get("identifier"),
            "mapping_verified": self.mapping.get("schema_verified"),
            "features_retrieved": self.counts.get("retrieved"),
            "features_accepted": self.counts.get("accepted"),
            "features_rejected": self.counts.get("rejected"),
            "rows_inserted": database.get("inserted"),
            "rows_updated": database.get("updated"),
            "rows_unchanged": database.get("unchanged"),
            "error_type": (self.error or {}).get("type"),
            "error_message": (self.error or {}).get("message"),
            "report": json.loads(self.to_json()),
        }
