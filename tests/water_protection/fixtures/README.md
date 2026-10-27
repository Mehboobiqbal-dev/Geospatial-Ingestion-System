# Test fixtures (synthetic)

Every file in this directory is **synthetic**. None of it was retrieved from
the Thuringia WFS: the endpoint `https://geoportal.thueringen.de/wfs/wsg`
answered scripted requests with an HTML bot-protection page and browsers
with HTTP 404 when it was inspected on 2026-10-27.

The documents follow the structure of standard OGC WFS 2.0.0 / 1.1.0
responses (GeoServer-style capabilities, XML Schema from
DescribeFeatureType, GML 3.2 feature collections). Property names (`zone_id`,
`wsg_nr`, `zone`, ...) match the **unverified** assumptions in
`app/water_protection/mappings/th_wsg_zonen.toml`. Identifiers use the
`TEST-` prefix and names start with "Synthetisches Testgebiet" so that no
fixture record can be mistaken for an official zone.

Coordinates are in EPSG:25832 near Erfurt (E 640 000, N 5 650 000) unless a
file states otherwise.

| File | Purpose |
|---|---|
| `capabilities_wfs200.xml` | WFS 2.0.0 capabilities with paging, sorting and GML 3.2 output |
| `capabilities_wfs110.xml` | WFS 1.1.0 capabilities (no standard paging) |
| `describe_feature_type.xsd` | Layer schema for `th_wsg_zonen` |
| `getfeature_hits.xml` | `resultType=hits` answer (numberMatched=5) |
| `getfeature_page_0.xml`, `_2`, `_4` | Three pages (2 + 2 + 1 features) |
| `getfeature_empty.xml` | Valid empty result |
| `exception_report.xml` | OWS 1.1 exception report |
| `html_challenge.html` | HTML page in place of XML (models the observed bot protection) |
| `getfeature_edge_cases.xml` | One collection with valid, repairable and rejectable features, used for the example report |
