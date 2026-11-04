# MediaCtrl Documentation

- [HANDOVER.md](HANDOVER.md) — current architecture, protocol findings, status, limitations
- [CACHE.md](CACHE.md) — runtime caches and persistence
- [PORTING_CPP.md](PORTING_CPP.md) — C++ / embedded port draft
- [MEDIA_DB_IMPORT.md](MEDIA_DB_IMPORT.md) — separate AFC/ifuse media DB + artwork importer
- [ft-file-transfer.md](ft-file-transfer.md) — file-transfer notes
- [tab5-afc-db-pull-feasibility.md](tab5-afc-db-pull-feasibility.md) — AFC DB pull feasibility
- [tab5-port-assessment.md](tab5-port-assessment.md) — Tab5 implementation guide

The Apple Accessory Interface Specification PDF is local reference material.
It is not distributed with this repository.

Runtime data stays at the repository root (`media-library.json`, `media-db-*.json`,
`media-artwork/`, caches). The raw SQLite snapshot, development logs, captures and
early probe scripts are under `artifacts/`. Both the runtime data and the
development artifacts are excluded from Git.
