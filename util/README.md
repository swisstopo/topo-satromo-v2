# util

Scripts that are run by hand (or by CI as a separate step). They are not part of the daily
`satromo_processor.py` run. Run every script from the repository root, for example
`python util/util_stac_download.py`.

## Folder rules

- The `util_*.py` scripts sit directly in this folder.
- Each reference or derived dataset gets its own folder here. These folders have no subfolders.
- The only exception is a local `output/` folder for results. It is git-ignored (`util/*/output/`).

## Scripts in this folder

| Script | Purpose |
|---|---|
| `util_get_acquisition_plans.py` | Downloads the Sentinel-2 acquisition plans and writes `tools/acquisitionplan.csv` (CI stage 1) |
| `util_extract_acquisition_plans_s2.py` | Helper of the script above: keeps the plan polygons inside the AOI |
| `util_upload_collection_asset.py` | Uploads collection-level assets (acquisition plan, dates with no imagery) (CI) |
| `util_reprocess_mosaic.py` | Batch run of the monthly cloud-free mosaics |
| `util_reprocess_mosaic_csde.py` | Batch run of the two-month cloud-free mosaics |
| `util_reprocess_mosaic_thumbnails.py` | A4 year-overview PNGs from the mosaics |
| `util_reprocess_tci_tap.py` | Re-publishes the TCI assets with the `-tap` alignment fix |
| `util_reprocess_terrain_tap.py` | Re-generates the terrain mask assets for a date range |
| `util_terrain_backfill.py` | Adds missing terrain mask assets |
| `util_stac_check_metadata.py` | Filters STAC items by values in their metadata asset |
| `util_stac_delete.py` | Deletes STAC assets and items (interactive) |
| `util_stac_download.py` | Downloads all STAC assets with a given title |
| `util_validate_stac_collection.py` | Checks the S2-SR collection against `tools/step0_empty_assets.csv` |

## Folders

| Folder | Content | Status |
|---|---|---|
| `ref_lst/` | Land surface temperature (LST) reference statistics | in use |
| `ref_ndvi_landsat/` | NDVI reference from Landsat, corrected to S2-SR | in use |
| `ref_ndvi_s2_forest/` | NDVI reference from Sentinel-2 for forests, input of `vhi_gapfill_forest/` | planned |
| `vhi_gapfill_forest/` | Gap-filled VHI for forests | planned |
| `vhi_historic_landsat_monthly/` | Historic monthly VHI from Landsat, 1991-2015 | in use |

A gap-filled VHI for all vegetation will get its own folder (`vhi_gapfill_vegetation/`) next to
`vhi_gapfill_forest/`.
