"""
Safe XML parsing and OGC/OWS helpers shared by all WFS operations.

WFS servers report many failures as XML exception documents, often
with HTTP 200. Bot-protection layers and CMS front-ends answer with
HTML pages, also with HTTP 200 (this is what the Thuringia endpoint
did during inspection). Every response therefore passes through
``parse_xml_response`` before any parsing of its content.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from lxml import etree

from app.water_protection.errors import UnexpectedContentError
from app.water_protection.errors import WfsServiceException

GML_NAMESPACES = frozenset(
    {
        "http://www.opengis.net/gml",
        "http://www.opengis.net/gml/3.2",
    }
)
XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
XSD_NAMESPACE = "http://www.w3.org/2001/XMLSchema"

_EXCEPTION_ROOTS = frozenset({"ExceptionReport", "ServiceExceptionReport"})
_HTML_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _parser() -> etree.XMLParser:
    # No DTD loading, no entity expansion, no network access: responses
    # are untrusted input (XXE / billion-laughs protection).
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )


# ==========================================================
# Element helpers (namespace-agnostic by local name)
# ==========================================================


def local_name(element: etree._Element) -> str:
    return etree.QName(element).localname


def namespace(element: etree._Element) -> str | None:
    return etree.QName(element).namespace


def is_gml(element: etree._Element) -> bool:
    return namespace(element) in GML_NAMESPACES


def child_elements(element: etree._Element) -> Iterator[etree._Element]:
    for child in element:
        if isinstance(child.tag, str):
            yield child


def children(element: etree._Element, name: str) -> list[etree._Element]:
    return [c for c in child_elements(element) if local_name(c) == name]


def first_child(element: etree._Element, name: str) -> etree._Element | None:
    for child in child_elements(element):
        if local_name(child) == name:
            return child
    return None


def descendants(element: etree._Element, name: str) -> Iterator[etree._Element]:
    for node in element.iter():
        if isinstance(node.tag, str) and node is not element and local_name(node) == name:
            yield node


def text_of(element: etree._Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def is_nil(element: etree._Element) -> bool:
    return element.get(f"{{{XSI_NAMESPACE}}}nil", "").strip().lower() in ("true", "1")


# ==========================================================
# Response validation
# ==========================================================


def looks_like_html(content: bytes) -> bool:
    head = content[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def html_title(content: bytes) -> str | None:
    match = _HTML_TITLE.search(content[:8192])
    if not match:
        return None
    title = re.sub(rb"\s+", b" ", match.group(1)).strip()
    return title.decode("utf-8", errors="replace")[:120] or None


@dataclass(frozen=True)
class OwsExceptionInfo:
    code: str | None
    locator: str | None
    text: str


def exception_info(root: etree._Element) -> OwsExceptionInfo | None:
    """Extract the first exception from an OWS 1.x/2.0 or OGC report."""

    if local_name(root) not in _EXCEPTION_ROOTS:
        return None
    exceptions = [
        node for node in root.iter() if isinstance(node.tag, str) and local_name(node) in (
            "Exception",
            "ServiceException",
        )
    ]
    if not exceptions:
        return OwsExceptionInfo(code=None, locator=None, text="(empty exception report)")
    first = exceptions[0]
    texts = [t.strip() for t in first.itertext() if t.strip()]
    return OwsExceptionInfo(
        code=first.get("exceptionCode") or first.get("code"),
        locator=first.get("locator"),
        text=" ".join(texts)[:2000] or "(no exception text)",
    )


def try_exception_info(content: bytes) -> OwsExceptionInfo | None:
    """Best-effort exception detection for non-2xx bodies; never raises."""

    if not content or looks_like_html(content):
        return None
    try:
        root = etree.fromstring(content, _parser())
    except etree.XMLSyntaxError:
        return None
    return exception_info(root)


def parse_xml_response(
    content: bytes,
    *,
    operation: str,
    expected_roots: frozenset[str],
    url: str | None = None,
    status_code: int | None = None,
) -> etree._Element:
    """
    Parse a WFS response and verify it is the expected document.

    Raises ``UnexpectedContentError`` for empty, HTML or malformed
    bodies and for unexpected root elements, and
    ``WfsServiceException`` for OWS exception reports.
    """

    if not content or not content.strip():
        raise UnexpectedContentError("empty response body", operation=operation, url=url)

    if looks_like_html(content):
        title = html_title(content)
        raise UnexpectedContentError(
            "received an HTML page instead of WFS XML"
            + (f" (page title: {title!r})" if title else "")
            + "; the endpoint may be behind bot protection, require browser"
            " interaction, or the URL may not be a WFS endpoint",
            operation=operation,
            url=url,
        )

    try:
        root = etree.fromstring(content, _parser())
    except etree.XMLSyntaxError as exc:
        raise UnexpectedContentError(
            f"response is not well-formed XML: {exc}", operation=operation, url=url
        ) from exc

    info = exception_info(root)
    if info is not None:
        raise WfsServiceException(
            operation=operation,
            code=info.code,
            locator=info.locator,
            text=info.text,
            status_code=status_code,
            url=url,
        )

    if local_name(root) not in expected_roots:
        raise UnexpectedContentError(
            f"unexpected root element <{local_name(root)}>; expected one of "
            f"{sorted(expected_roots)}",
            operation=operation,
            url=url,
        )
    return root
