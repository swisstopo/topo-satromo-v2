# CLAUDE.md

## Project Purpose
Operational Python  and GDAL ETL pipeline generating Analysis-Ready Data (ARD) and vegetation/drought indices from Sentinel-2 satellite imagery over Switzerland. Produces two main products:
- **S2-SR**: Co-registered, cloud-masked, terrain-corrected Sentinel-2 L2A ARD
- **VHI**: Vegetation Health Index for drought stress monitoring

Published to swisstopo STAC catalog and AWS S3.

## Repository Architecture

```
satromo_processor.py     # Main orchestration (daily; step0 → step1 dispatch: S2-SR, then VHI)
check_s2_sr.py           # Lightweight Copernicus STAC availability check (hourly, CI stage 1)
check_vhi.py             # Lightweight VHI check: retry 'LST missing' / 'No S2-SR scene in time window', find VHI to reprocess (CI stage 1)
rerun.py                 # Reprocessing engine (GPU EC2; reads tools/step0_empty_assets.csv and VHI_REPROCESS_DATES)
hourly_runner.sh         # Local cron alternative to run_rerun.yml
step0_functions.py       # Availability checking and STAC/S3 querying logic
main_functions/          # Shared modules: auth, coregistration, terrain, STAC publish, mosaic, warn regions
util/                    # Manually run tools: util_*.py (STAC/S3 maintenance, reprocessing) + one folder per
                         #   reference/derived dataset (ref_lst, ref_ndvi_landsat, ref_ndvi_s2_forest,
                         #   vhi_gapfill_forest, vhi_historic_landsat_monthly, vhi_percentiles); no deeper subfolders, output/ is git-ignored
step0_processors/        # Shared step0 helpers (step0_utils.py), not per-product generators
step1_processors/        # step1_processor_s2_sr.py: full S2 ARD (download, mosaic, cloud mask, co-register, terrain)
                         # step1_processor_vhi.py: VHI from S2-SR NDVI and MeteoSwiss LST (forest + vegetation, 10 m)
configuration/           # dev_config.py / prod_config.py / __init__.py (config loader)
tools/                   # Runtime tracking files (CSV tile status, committed to git)
assets/                  # Geospatial reference vectors (boundaries, lakes, rivers)
local_assets/            # Large static files (DSM 4.5 GB, reference imagery 3 GB, EGM96)
secrets/                 # Credentials — git-ignored; injected as env vars in CI
```

## Development Workflow

**Local run:**
```bash
python satromo_processor.py                              # dev_config, today
python satromo_processor.py dev_config.py 2024-06-12    # specific date
python satromo_processor.py dev_config.py 2024-06-12 --force      # force reprocess S2-SR and VHI
python satromo_processor.py dev_config.py 2024-06-12 --force-vhi  # force reprocess VHI only
```

**Debug mode** (in `satromo_processor.py`): run without a date argument; the hardcoded `current_date_str`, `force_reprocess` and `force_vhi` in the `if debug_mode:` block are used.

**CI/CD (GitHub Actions):**
- `run_rerun.yml` — `workflow_dispatch` only, triggered hourly by an external scheduler (cron-job.org), roughly 23–11 UTC. Stage 1 (ubuntu-24.04): `util_get_acquisition_plans.py`, `check_s2_sr.py`, `check_vhi.py`, publish CSVs, push `tools/`. Stage 2a: start GPU EC2 runner only if `needs-processing` or `vhi-needs-processing`. Stage 2b: `rerun.py` on EC2 (420-minute timeout). Stage 2c: always stop EC2 (cost protection).
- `hourly_runner.sh` — same steps as a local cron job; do not run it and the workflow on the same branch at the same time.

**Config selection:** Pass config name as first CLI arg; defaults to `dev_config.py`. Production uses `prod_config.py`.

## Coding Conventions
- Python 3.11/3.12, 64-bit only
- `numpy < 2.0.0` — hard constraint; numpy 2.x breaks compatibility with geospatial stack
- Config values come from the imported config module (e.g., `config.S3_BUCKET_NAME`) — never hardcode environment-specific paths or endpoints
- Secrets are loaded from `secrets/` folder (dev) or environment variables (prod); never commit secrets
- `tools/step0_empty_assets.csv` is the live tile-status tracking file; it is committed to git by CI after each run
- gdal is used via subprocess module


Preferred style:

- Simple functions
- Linear execution flow
- Easy debugging
- Clear variable names
- Explicit code over clever abstractions

The primary maintainer is a Python beginner and prefers straightforward code.

## Key Dependencies
| Package | Purpose | Notes |
|---------|---------|-------|
| GDAL / rasterio | Raster I/O | Windows: pre-compiled wheel required |
| AROSICS 1.13.2 | Sub-pixel co-registration | Requires GDAL C++ bindings |
| OmniCloudMask 1.7.0 | Cloud/shadow masking | Deep learning based |
| HORAYZON | Topographic correction | C++ native |
| pystac 1.14.1 + pystac-client | STAC catalog access/publish | |
| boto3 / s3fs | AWS S3 I/O | |
| geopandas ≥ 1.1.2 | Vector operations | |
| xarray + rioxarray | N-dimensional arrays | |

## Performance Considerations
- GPU processing (`rerun.py`) runs on an AWS GPU EC2 instance (`EC2_INSTANCE_TYPE`) — only triggered when S2-SR tiles are ready or VHI is ready to process
- `local_assets/` contains large static files (DSM, reference images); never regenerate these on the fly — they are symlinked on EC2 from `/home/ubuntu`
- Parallel terrain processing available via `main_terrain_parallel.py`
- AROSICS window size and co-registration thresholds are tunable in config; poor reference coverage degrades quality

## Things to Avoid
- Do NOT install numpy 2.x — breaks GDAL/rasterio/AROSICS stack
- Do NOT commit `secrets/`, `processing/`, `local_assets/`, or `*.bak` files
- Do NOT hardcode S3 bucket names, STAC endpoints, or API credentials — always use config
- Avoid modifying `tools/step0_empty_assets.csv` manually unless correcting tile state
- Complex class hierarchies
- Heavy design patterns
- Unnecessary abstractions
- Additional dependencies unless required
- using gdal binding in python

## Proposing Changes
1. Work on a the current active feature branch
2. Verify with `dev_config.py` locally 
3. Config changes (S3 paths, endpoints, thresholds) require matching updates in `dev_config.py`
4. No formal test suite — include a manually-verified example run in the description
6. Large binary assets belong in `local_assets/` (gitignored); never add them to git
