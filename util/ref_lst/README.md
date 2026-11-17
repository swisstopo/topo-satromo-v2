# ref_lst

Land surface temperature (LST) reference statistics for the VHI.

`LST_reference.py` calculates LST from the MeteoSwiss all-sky longwave radiation (SDL and SOL,
data.geo.admin.ch), with the same calculation as the daily VHI, and aggregates it to reference
statistics (2004-2020, 11am UTC):

- DOY reference (+/- 3 days window), used by the daily VHI (`LST_reference_data` in the configuration)
- monthly reference, used by the historic VHI (`LST_reference_ch02` / `LST_reference_ch05` in `PRODUCT_VHI_HIST`)

Run from the repository root:

    python util/ref_lst/LST_reference.py

The DOYs and months to process are set at the top of the script.

Open point: the LST input for 1991-2003 (MFG). When it is sorted out, its scripts go into this folder too,
so that all LST reference data is produced from here.
