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

Open point: the LST input for 1991-2003 (MFG). Until it is added, months before 2004 are skipped.
