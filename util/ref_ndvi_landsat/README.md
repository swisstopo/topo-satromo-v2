# VI reference

Tools to compare Landsat with swissEO S2-SR, correct the Landsat bands, and rebuild the reference
statistics for a vegetation index. They are separate from the operational pipeline. VHI only uses
the finished reference files, through `NDVI_reference_data` in the configuration.

## Decisions

| Topic | Decision |
|---|---|
| Purpose | A reliable NDVI reference for VHI, after the move from the GEE harmonised product to direct CDSE downloads |
| Grid | All outputs in EPSG:2056, on the 30 m grid of the current reference. Coordinates are divisible by 10, so each 30 m pixel covers exactly 9 S2-SR pixels |
| Landsat source | Collection 2 Level 2 from Microsoft Planetary Computer |
| Sensor test | Same-day pairs from 2015 onwards with Landsat 7 (up to 2020) and Landsat 8 |
| Landsat 5 | Cannot be tested, as it ended before Sentinel-2 started. The Landsat 7 corrections are used for it |
| Landsat 9 | Not in the 1991-2020 reference. Revisit when the climate reference period is shifted |
| S2 NIR band | B08, because VHI uses it |
| Flat pixels | Slope up to 5 degrees, where the GEE topographic correction was not applied |
| Correction | A smooth curve (polynomial) of the index difference S2-SR minus Landsat, as a function of the Landsat index, per sensor group and season. Fitted on vegetation and forest pixels. The season is taken from the month of each scene (DJF, MAM, JJA, SON), without blending between seasons, to keep it simple. A time window across a season boundary therefore mixes two curves. A straight band correction was tested and rejected. For another index, repeat steps 4 and 6 for that index |
| Masks | As tested in step 4: Landsat QA bits for cloud, shadow and snow, snow also by NDSI above 0 (water exempt), flat pixels only (slope up to 5 degrees). No blue band filter |
| Threshold | Correct if a verified NDVI bin differs by more than 0.01 |
| Reference format | COG, `uint8`, band order `min, max, mean, median, std, p05, p95`, with a sample count added as band 8. M and MM files keep the band order of the current ones: `min, max, mean, median, std, meanAD, medianAD, p05, p95`, count as band 10 (meanAD = mean of \|value - mean\|, medianAD = median of \|value - median\|). Overviews NEAREST. Scene list in the `scene_list` tag |
| Swiss data | swissALTI3D for slope and swissTLM3D for hard surfaces |

Output files follow the current naming, for example `NDVI_Stats_DOY180.tif`, in a new S3 folder.
The current reference files stay untouched until VHI is switched over.

## Changes compared to the GEE script

- Values are rounded to the nearest step instead of cut off, which removes a bias of about -0.005 NDVI
- Scenes from the same day and overpass are merged first, so they no longer count twice
- The DOY window is 15 days (DOY -7 to +7), not 16
- The terrain shadow mask is applied. It was calculated before but never used
- The filter that removed all pixels with blue reflectance above 0.1 is replaced by a snow mask with NDSI above 0, as in VHI

## Steps

Install the packages with `pip install -r util/ref_ndvi_landsat/requirements.txt`.
The GDAL command line tools must be on the PATH.

1. Download the swissTLM3D shapefile archive (`swisstlm3d_2026-02_2056_5728.shp.zip`) to
   `util/ref_ndvi_landsat/output/static/swisstlm3d_2026-02_shp.zip`.
2. `python util/ref_ndvi_landsat/prepare_static_layers.py` creates slope, vegetation classes and hard surfaces.
3. `python util/ref_ndvi_landsat/step0_check_s2_scaling.py` checks that S2-SR uses the same offset in the whole archive.
4. `python util/ref_ndvi_landsat/step1_collect_matchups.py pilot` collects pixel pairs. Use `all` for the full comparison.
5. `python util/ref_ndvi_landsat/step2_fit_band_corrections.py` writes the band corrections and the plots to `output/results`.
   This was the analysis that showed a straight band correction does not work. The reference does not use it.
6. `python util/ref_ndvi_landsat/step3_fit_correction_curves.py` fits the NDVI correction curves and draws one figure per
   sensor group (`correction_curves_landsat5_7.png`, `correction_curves_landsat8.png`) in `output/results`.

7. `python util/ref_ndvi_landsat/step4_build_reference.py` builds the reference files in `output/reference`.
   Which files are built is set by `REFERENCE_PERIODS` and `TIME_STEP` in the config. It can be stopped and
   started again. One DOY took about 75 minutes in the test (333 scenes, 8 workers). Every following DOY only
   adds the scenes of one new day, so the full run of 366 DOYs takes roughly 2-3 days.
   The files are Cloud Optimized GeoTIFFs (COG), like the current ones on S3. The list of scenes used is in the
   metadata tag `scene_list` (CSV text, read it with `pd.read_csv(io.StringIO(tags["scene_list"]))`).
   The overviews use NEAREST, so they hold real pixel values, never averages. They are only for viewing:
   the statistics are calculated at full resolution before the COG is written, and VHI reads the full-resolution
   band. Each DOY file is about 320 MB (about 118 GB for all 366), and the scene cache needs about 4-6 GB.
   With `UPLOAD_TO_S3 = True` every finished file is uploaded to the folder in `S3_FOLDERS` (one per `TIME_STEP`)
   and deleted locally. A file that is already on S3 is not built again. DOY 366 is also uploaded as DOY 000,
   with the same content, as for the current reference files. The credentials are in `secrets/s3_prod.json`.
   This user can write but not delete files on S3. To replace a file, upload it again, or ask for it to be deleted.

   The time step can also be given on the command line, before the numbers: `... step4_build_reference.py MM 3`
   builds only MM03 (February and March). `... step4_build_reference.py MM M` builds all 12 MM and 12 M files in one
   run. They share the scene cache (at most 3 months, about 8 GB), so every scene is processed only once (about 27 hours).

HORAYZON (terrain shadow) needs the EGM96 geoid in `local_assets/EGM/EGM96/WW15MGH.GRD`. On EC2 it is already
there. Locally it was created from the PROJ copy of the same grid (`https://cdn.proj.org/us_nga_egm96_15.tif`),
because the NGA download site cannot be reached from the swisstopo network.

All results are written to `util/ref_ndvi_landsat/output`, which is not committed.

## Historic VHI (1991-2015)

`util/vhi_historic_landsat_monthly/step1_processor_vhi_historic.py` calculates the monthly VHI from Landsat for 1991-01 to 2015-12
(from 2016 onwards the daily Sentinel-2 VHI is used). It reuses step 4: the scenes are processed exactly like the
scenes of the reference (masks, terrain shadow, topographic correction, correction curves). So it needs from
`util/ref_ndvi_landsat/output`: the static layers (step 2), the correction curves (step 6) and the scene list
`reference_scenes.csv` (made by step 7). The monthly NDVI is the mean of all overpasses of the month, compared
with the M reference files. The settings are in `PRODUCT_VHI_HIST` of the configuration.

    python util/vhi_historic_landsat_monthly/step1_processor_vhi_historic.py dev_config.py 2005-07-01               one month
    python util/vhi_historic_landsat_monthly/step1_processor_vhi_historic.py dev_config.py 2005-01-01 2005-12-01    all months in between

The monthly LST reference (2004-2020) is `LST_reference_ch05` (0.05° grid) for 1991-2003 and `LST_reference_ch02`
(0.02° grid) for 2004-2015, see `PRODUCT_VHI_HIST` in the configuration.

Open point: the LST input for 1991-2003 (MFG). Until it is added, months before 2004 are skipped.

## Adding another index

1. Add the bands to `BANDS` and the index to `INDICES` in `vi_reference_config.py`.
2. Add the new bands to steps 1 and 2 and run them, to get the corrections for these bands.
3. Set `INDEX_NAME` and build the reference.
