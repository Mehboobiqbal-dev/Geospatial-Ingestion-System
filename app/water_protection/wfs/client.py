"""
WFS operations: capabilities, schema, feature count and paged retrieval.

Retrieval is all-or-nothing. If the number of features received does
not match what the service reported, or the reported total changes
while paging, the run fails with ``IncompleteRetrievalError`` instead
of loading a partial snapshot.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from app.water_protection.errors import CapabilitiesError
from app.water_protection.errors import IncompleteRetrievalError
from app.water_protection.errors import RetrievalLimitExceeded
from app.water_protection.errors import WfsServiceException
from app.water_protection.wfs.capabilities import FeatureTypeInfo
from app.water_protection.wfs.capabilities import WfsCapabilities
from app.water_protection.wfs.capabilities import parse_capabilities
from app.water_protection.wfs.gml import FeaturePage
from app.water_protection.wfs.gml import RawFeature
from app.water_protection.wfs.gml import parse_feature_collection
from app.water_protection.wfs.http import WfsHttpClient
from app.water_protection.wfs.http import build_url
from app.water_protection.wfs.http import redact_url
from app.water_protection.wfs.schema import FeatureTypeSchema
from app.water_protection.wfs.schema import parse_feature_type_schema

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievalPlan:
    """Everything derived from the capabilities that shapes GetFeature requests."""

    capabilities: WfsCapabilities
    feature_type: FeatureTypeInfo
    output_format: str | None
    paging: bool
    page_size: int
    sort_by: str | None


@dataclass(frozen=True)
class RetrievalResult:
    features: tuple[RawFeature, ...]
    number_matched: int | None
    pages: int
    request_url: str  # canonical, redacted, without paging parameters
    server_time_stamp: str | None
    default_srs: str | None
    duplicate_features_collapsed: int


class WfsClient:
    def __init__(
        self,
        http: WfsHttpClient,
        base_url: str,
        *,
        accept_versions: tuple[str, ...] = ("2.0.0", "1.1.0"),
    ) -> None:
        self.http = http
        self.base_url = base_url
        self.accept_versions = accept_versions

    # =====================================================
    # METADATA
    # =====================================================

    def get_capabilities(self) -> tuple[WfsCapabilities, bytes]:
        params = {
            "SERVICE": "WFS",
            "REQUEST": "GetCapabilities",
            "ACCEPTVERSIONS": ",".join(self.accept_versions),
        }
        response = self.http.get(self.base_url, params, operation="GetCapabilities")
        return parse_capabilities(response.content, url=response.url), response.content

    def describe_feature_type(
        self,
        capabilities: WfsCapabilities,
        feature_type: FeatureTypeInfo,
    ) -> FeatureTypeSchema:
        params = self._base_params(capabilities, "DescribeFeatureType", feature_type)
        response = self.http.get(self.base_url, params, operation="DescribeFeatureType")
        return parse_feature_type_schema(response.content, feature_type.local_name, url=response.url)

    def plan(
        self,
        capabilities: WfsCapabilities,
        layer: str,
        *,
        page_size: int,
        sort_by: str | None,
    ) -> RetrievalPlan:
        feature_type = capabilities.find_feature_type(layer)
        if "GetFeature" not in capabilities.operations and capabilities.operations:
            raise CapabilitiesError("service does not advertise GetFeature", operation="GetCapabilities")
        effective_page = page_size
        if capabilities.count_default is not None:
            effective_page = min(page_size, capabilities.count_default)
        return RetrievalPlan(
            capabilities=capabilities,
            feature_type=feature_type,
            output_format=capabilities.select_output_format(feature_type),
            paging=capabilities.implements_result_paging,
            page_size=effective_page,
            # A stable sort keeps pages consistent between requests; only
            # used when the service declares sorting support.
            sort_by=sort_by if capabilities.implements_sorting else None,
        )

    def canonical_request_url(self, plan: RetrievalPlan) -> str:
        params = self._getfeature_params(plan)
        return redact_url(build_url(self.base_url, params))

    # =====================================================
    # FEATURES
    # =====================================================

    def count_features(self, plan: RetrievalPlan) -> int | None:
        params = self._getfeature_params(plan)
        params["RESULTTYPE"] = "hits"
        try:
            response = self.http.get(self.base_url, params, operation="GetFeature[hits]")
        except WfsServiceException as exc:
            logger.warning("resultType=hits not supported; total is unknown", extra={"error": str(exc)})
            return None
        page = parse_feature_collection(
            response.content,
            layer_local_name=plan.feature_type.local_name,
            geometry_property="",
            operation="GetFeature[hits]",
            url=response.url,
        )
        return page.number_matched if page.number_matched is not None else page.number_returned

    def fetch_sample(self, plan: RetrievalPlan, *, geometry_property: str, count: int) -> FeaturePage:
        params = self._getfeature_params(plan)
        params[self._count_param(plan)] = str(count)
        return self._get_page(plan, params, geometry_property, "GetFeature[sample]")

    def fetch_all(
        self,
        plan: RetrievalPlan,
        *,
        geometry_property: str,
        max_features: int,
        max_pages: int,
    ) -> RetrievalResult:
        total = self.count_features(plan)
        if total is not None and total > max_features:
            raise RetrievalLimitExceeded(
                f"service reports {total} features, above the configured limit of {max_features}",
                operation="GetFeature[hits]",
            )

        features: list[RawFeature] = []
        seen: dict[str, RawFeature] = {}
        collapsed = 0
        pages = 0
        time_stamp: str | None = None
        default_srs: str | None = None

        for page in self._pages(plan, geometry_property, total, max_features, max_pages):
            pages += 1
            time_stamp = time_stamp or page.time_stamp
            default_srs = default_srs or page.collection_srs
            for feature in page.features:
                # Paging without a stable order can repeat a feature on two
                # pages; identical repeats are collapsed and counted.
                if feature.gml_id is not None and feature.gml_id in seen:
                    if seen[feature.gml_id] == feature:
                        collapsed += 1
                        continue
                    raise IncompleteRetrievalError(
                        f"feature {feature.gml_id!r} was returned twice with different content; "
                        "the source changed during paging",
                        operation="GetFeature",
                    )
                if feature.gml_id is not None:
                    seen[feature.gml_id] = feature
                features.append(feature)

        # Compare unique features: under unstable paging a repeated feature
        # means another one was skipped, so repeats must not make up the count.
        if total is not None and len(features) != total:
            raise IncompleteRetrievalError(
                f"service reported {total} features but {len(features)} unique features were "
                f"received ({collapsed} repeated)",
                operation="GetFeature",
            )
        if collapsed:
            logger.warning(
                "identical features repeated across pages were collapsed",
                extra={"collapsed": collapsed},
            )

        return RetrievalResult(
            features=tuple(features),
            number_matched=total,
            pages=pages,
            request_url=self.canonical_request_url(plan),
            server_time_stamp=time_stamp,
            default_srs=default_srs,
            duplicate_features_collapsed=collapsed,
        )

    def _pages(
        self,
        plan: RetrievalPlan,
        geometry_property: str,
        total: int | None,
        max_features: int,
        max_pages: int,
    ) -> Iterator[FeaturePage]:
        if not plan.paging:
            # No standard paging: one bounded request. Asking for one more
            # than the limit lets us detect that the limit was exceeded.
            params = self._getfeature_params(plan)
            params[self._count_param(plan)] = str(max_features + 1)
            page = self._get_page(plan, params, geometry_property, "GetFeature")
            if len(page.features) > max_features:
                raise RetrievalLimitExceeded(f"more than {max_features} features returned", operation="GetFeature")
            # A server-side cap (CountDefault) cuts the answer short; catch it
            # here too, for services that give no resultType=hits count.
            if page.number_matched is not None and page.number_matched > len(page.features):
                raise IncompleteRetrievalError(
                    f"numberMatched={page.number_matched} but only {len(page.features)} features were returned",
                    operation="GetFeature",
                )
            yield page
            return

        start = 0
        for page_number in range(1, max_pages + 1):
            params = self._getfeature_params(plan)
            params["COUNT"] = str(plan.page_size)
            params["STARTINDEX"] = str(start)
            operation = f"GetFeature[startIndex={start}]"
            page = self._get_page(plan, params, geometry_property, operation)
            returned = len(page.features)

            if page.number_returned is not None and page.number_returned != returned:
                raise IncompleteRetrievalError(
                    f"numberReturned={page.number_returned} but {returned} members parsed",
                    operation=operation,
                )
            if total is not None and page.number_matched is not None and page.number_matched != total:
                raise IncompleteRetrievalError(
                    f"numberMatched changed from {total} to {page.number_matched} during paging",
                    operation=operation,
                )

            yield page
            start += returned
            if start > max_features:
                raise RetrievalLimitExceeded(f"more than {max_features} features retrieved", operation=operation)
            if returned == 0:
                return
            if total is not None and start >= total:
                return
            if page_number == max_pages:
                raise RetrievalLimitExceeded(
                    f"page limit of {max_pages} reached before the last page", operation=operation
                )
        # With an unknown total, paging stops only at an empty page; a
        # short page is not trusted as the end because servers may cap
        # page sizes silently.

    def _get_page(
        self,
        plan: RetrievalPlan,
        params: dict[str, str],
        geometry_property: str,
        operation: str,
    ) -> FeaturePage:
        response = self.http.get(self.base_url, params, operation=operation)
        page = parse_feature_collection(
            response.content,
            layer_local_name=plan.feature_type.local_name,
            geometry_property=geometry_property,
            operation=operation,
            url=response.url,
        )
        logger.info(
            "WFS page received",
            extra={"operation": operation, "features": len(page.features), "bytes": len(response.content)},
        )
        return page

    # =====================================================
    # PARAMETERS
    # =====================================================

    def _base_params(
        self,
        capabilities: WfsCapabilities,
        request: str,
        feature_type: FeatureTypeInfo,
    ) -> dict[str, str]:
        params = {
            "SERVICE": "WFS",
            "VERSION": capabilities.version,
            "REQUEST": request,
        }
        if capabilities.is_wfs2:
            params["TYPENAMES"] = feature_type.name
            if feature_type.prefix and feature_type.namespace_uri:
                params["NAMESPACES"] = f"xmlns({feature_type.prefix},{feature_type.namespace_uri})"
        else:
            params["TYPENAME"] = feature_type.name
        return params

    def _getfeature_params(self, plan: RetrievalPlan) -> dict[str, str]:
        params = self._base_params(plan.capabilities, "GetFeature", plan.feature_type)
        if plan.output_format:
            params["OUTPUTFORMAT"] = plan.output_format
        # No SRSNAME: features are requested in the layer's declared
        # default CRS and transformed locally, so the transformation is
        # explicit, auditable and independent of server-side reprojection.
        if plan.sort_by:
            direction = "ASC" if plan.capabilities.is_wfs2 else "A"
            params["SORTBY"] = f"{plan.sort_by} {direction}"
        return params

    @staticmethod
    def _count_param(plan: RetrievalPlan) -> str:
        return "COUNT" if plan.capabilities.is_wfs2 else "MAXFEATURES"
