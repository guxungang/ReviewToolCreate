# Third-party sources

v0.1 contains no copied files from upstream projects. The design references in the
specification (Appendix D, S01-S15) informed requirements only.

| Upstream | Pinned commit (spec §4.1) | Reused files | Licence status |
| --- | --- | --- | --- |
| jam-cc/paper-review.skill | bb9900ff7a95e6a28092ab0a9e1d81098f41634e | none | MIT (keep notice if content is copied later) |
| QinghongLin/paperdoctor | b44221d35b4cfd1d99baf2256ffebb1d7b45effd | none | root licence file not found at that commit: verify before copying any code |
| amirkiarafiei/open-scholar-peer | 229370cbe34cde85499addd5246c95f1f1729f97 | none | MIT (keep notice if content is copied later) |

Runtime dependencies: pydantic (MIT), pypdf (BSD-3-Clause). Test: pytest (MIT).
External services contacted only when permitted per run: api.openalex.org, export.arxiv.org / arxiv.org.
