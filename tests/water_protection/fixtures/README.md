# Test fixtures

Two kinds of files live here.

**Recorded** (file names start with `live_`, plus `describe_feature_type.xsd`):
responses of the TLUBN WFS
`https://www.geoproxy.geoportal-th.de/geoproxy/services/wsg_hqsg_wfs`, layer
`tmlnu:WASSER_UND_HEILQUELLENSCHUTZGEBIETE`, retrieved on 2026-10-28 (data
Stand 2026-10-13). Licence CC BY 4.0, attribution "© GDI-Th". Each file
says in a leading comment what was changed (only that comment, and for the
excerpt the `numberMatched`/`numberReturned` counts).

**Synthetic**: everything else. These documents use the real element and
property names but invented values: zone numbers `9900000nn`, protection
areas `99001`/`99002`, names starting with "Synthetisches Testgebiet", so no
fixture record can be mistaken for an official zone. Zone numbers are
written the way the service writes `xsd:double` (`9.90000001E8`).
Coordinates are in EPSG:25832 near Erfurt (E 640 000, N 5 650 000) unless a
file states otherwise.

| File | Kind | Purpose |
|---|---|---|
| `live_capabilities.xml` | recorded | Real capabilities: no standard paging, CountDefault 25 000, sorting |
| `describe_feature_type.xsd` | recorded | Real layer schema (15 properties) |
| `live_getfeature_excerpt.xml` | recorded | 11 of 3 090 real features: map symbol, zone codes IIC/IIIC, healing-spring zones, multipart, the one geometry that needs repair, a remark "Beschluss nichtig" |
| `capabilities_wfs200.xml` | synthetic | WFS 2.0.0 capabilities with paging, used to test the paging path |
| `capabilities_wfs110.xml` | synthetic | WFS 1.1.0 capabilities (no standard paging) |
| `getfeature_hits.xml` | synthetic | `resultType=hits` answer (numberMatched=5) |
| `getfeature_page_0.xml`, `_2`, `_4` | synthetic | Three pages (2 + 2 + 1 features) |
| `getfeature_empty.xml` | synthetic | Valid empty result |
| `exception_report.xml` | synthetic | OWS 1.1 exception report |
| `html_challenge.html` | synthetic | HTML page in place of XML (models the CAPTCHA served by the assignment's endpoint) |
| `getfeature_edge_cases.xml` | synthetic | 20 features: valid, repairable and rejectable cases, used for the example report |
