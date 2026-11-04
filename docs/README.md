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

Runtime data is stored per device under `data/<library_id>/` (`iap2.db`,
`media-cache.db`, `media-artwork/`, `artwork-cache/`, `localPlaylist.csv`,
`history.log`). Development logs, captures, and early probe scripts are under
`artifacts/`. Both runtime data and development artifacts are excluded from
Git.
