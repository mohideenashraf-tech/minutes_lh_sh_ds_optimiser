# Data directory

| File | Purpose |
|------|---------|
| `sample.csv` | Bundled Live Grid Pendency Raw_1 sample for demo |
| `static_reference_cache.xlsx` | Source/destination lat-lon + landing window tabs |
| `uploads/` | Per-session uploads (created at runtime, not in git) |

Uploads are rebuilt into `static_reference_cache.xlsx` when a new CSV is uploaded via the API.
