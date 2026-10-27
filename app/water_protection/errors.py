"""
Exception hierarchy for the water protection zone ingestion.

Retrieval failures (``SourceError`` and subclasses) abort a run.
Record-level problems (``RecordRejected``) only quarantine the
affected feature, so one bad zone never blocks the valid ones.
"""

from __future__ import annotations

from typing import Any


class WaterProtectionError(Exception):
    """Base class for all errors raised by this package."""


# ==========================================================
# Configuration and safety
# ==========================================================


class ConfigurationError(WaterProtectionError):
    """Invalid or missing configuration."""


class MappingError(ConfigurationError):
    """The source-to-destination mapping file is invalid."""


class StagingTargetRefused(WaterProtectionError):
    """
    A database write target failed the staging safety guard.

    Raised before any connection is opened whenever the configured
    target could be iris_core, a production database, or anything
    other than the iris_staging schema.
    """


# ==========================================================
# Source retrieval
# ==========================================================


class SourceError(WaterProtectionError):
    """
    A WFS operation failed.

    ``operation`` names the failing request (for example
    ``GetFeature[startIndex=1000]``) and ``url`` is always redacted.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        url: str | None = None,
    ) -> None:
        self.operation = operation
        self.url = url
        detail = f"{operation}: {message}"
        if url:
            detail = f"{detail} (url={url})"
        super().__init__(detail)


class HttpRequestError(SourceError):
    """Transport failure (timeout, connection error) after all retries."""


class HttpStatusError(SourceError):
    """Unsuccessful HTTP status without a parseable OWS exception."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        status_code: int,
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(message, operation=operation, url=url)


class WfsServiceException(SourceError):
    """The service answered with an OWS/OGC exception report."""

    def __init__(
        self,
        *,
        operation: str,
        code: str | None,
        locator: str | None,
        text: str,
        status_code: int | None = None,
        url: str | None = None,
    ) -> None:
        self.code = code
        self.locator = locator
        self.text = text
        self.status_code = status_code
        parts = [f"service exception code={code or 'unspecified'}"]
        if locator:
            parts.append(f"locator={locator}")
        if status_code is not None:
            parts.append(f"http_status={status_code}")
        parts.append(f"text={text!r}")
        super().__init__(", ".join(parts), operation=operation, url=url)


class UnexpectedContentError(SourceError):
    """The response is not the XML document the operation should return."""


class ResponseTooLargeError(SourceError):
    """The response exceeded the configured size limit."""


class CapabilitiesError(SourceError):
    """The capabilities do not support the requested retrieval."""


class SchemaMismatchError(SourceError):
    """The live layer schema does not match the configured mapping."""


class IncompleteRetrievalError(SourceError):
    """Fewer (or different) features were retrieved than the service reported."""


class RetrievalLimitExceeded(SourceError):
    """The configured feature or page limit would be exceeded."""


# ==========================================================
# Record level
# ==========================================================


class RecordRejected(WaterProtectionError):
    """
    A single feature cannot be staged.

    ``reason_code`` is a stable machine-readable code used in the
    ingestion report and the quarantine table; ``evidence`` carries
    optional structured detail (for example repair diagnostics).
    """

    def __init__(
        self,
        reason_code: str,
        detail: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.detail = detail
        self.evidence = evidence
        super().__init__(f"{reason_code}: {detail}")


class GeometryParseError(WaterProtectionError):
    """A GML geometry could not be parsed into coordinates."""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


# ==========================================================
# Persistence
# ==========================================================


class PersistenceError(WaterProtectionError):
    """A staging write failed; the transaction has been rolled back."""
