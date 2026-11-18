# vhi_historic_landsat_monthly

Historic VHI, one file per calendar month, from Landsat 5, 7 and 8 for 1991-01 to 2015-12.
From 2016 onwards the daily Sentinel-2 VHI is used. Published in the same collection as the daily VHI.

`step1_processor_vhi_historic.py` processes the Landsat scenes exactly like the NDVI reference
(masks, terrain shadow, topographic correction, correction curves). It therefore needs:

- the code and the local `output/` folder of `util/ref_ndvi_landsat/` (static layers, correction curves,
  `reference_scenes.csv`), see `util/ref_ndvi_landsat/README.md`
- the monthly NDVI and LST reference files on S3 (`PRODUCT_VHI_HIST` in the configuration;
  LST reference from `util/ref_lst/`)

Run from the repository root:

    python util/vhi_historic_landsat_monthly/step1_processor_vhi_historic.py dev_config.py 2005-07-01               one month
    python util/vhi_historic_landsat_monthly/step1_processor_vhi_historic.py dev_config.py 2005-01-01 2005-12-01    all months in between

    python util/vhi_historic_landsat_monthly/step1_processor_vhi_historic.py dev_config.py 2005-07-01 --force       reprocess even if published

Where the results go is decided by `step1_collection` of `PRODUCT_VHI_HIST`: sys-data.int.bgdi.ch (int) or
data.geo.admin.ch (prod). The script uses the matching login (`secrets/stac_fsdi-int.json` or
`secrets/stac_fsdi-prod.json`) and prints the target at the start. There is no `step0_collection` on purpose:
it would make `satromo_processor.py` run this product.

Months that are already completely published (all 10 assets) are skipped, so a stopped run can simply be started
again. Incompletely published months are processed again. `--force` reprocesses every month.

LST input (MeteoSwiss, data.geo.admin.ch/ch.meteoschweiz.landoberflaechentemperatur, one file per month, SDL and SOL):

- 1991-2003: MFG, `MFG1991-2005/mfg.{SDL|SOL}.H_ch05h.lonlat_YYYYMM01000000.nc`, 0.05° grid, LST reference `LST_reference_ch05`
- 2004-2015: MSG, `MSG2004-2023/msg.{SDL|SOL}.H_ch02.lonlat_YYYYMM01000000.nc`, 0.02° grid, LST reference `LST_reference_ch02`
