"""
One-time script to bootstrap the 'current' STAC item for swisseo_vhi_v200.
Expects files to already be renamed to 'current' in the project root.

Usage:
In the local folder:
    rename all occurrences of 'YYYY-MM-DD' in the filenames to 'current'
In a bash terminal:
    python tools/bootstrap_current_vhi.py dev_config.py YYYY-MM-DD
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import configuration as config
from main_functions import main_publish_stac_fsdi


def bootstrap_current(date_str: str):
    timestamp = f'{date_str}t235959'
    product_name = config.PRODUCT_VHI['product_name']
    warnformats = ['.csv', '.geojson', '.parquet']

    base = product_name.replace('ch.swisstopo.', '')

    assets = [
        (f'{base}_mosaic_current_forest-10m.tif',              config.PRODUCT_VHI['geocat_id_forest'],      'FOREST-10M'),
        (f'{base}_current_forest-warnregions.csv',             config.PRODUCT_VHI['geocat_id_forest'],      'FOREST-WARNREGIONS-CSV'),
        (f'{base}_current_forest-warnregions.geojson',         config.PRODUCT_VHI['geocat_id_forest'],      'FOREST-WARNREGIONS-GEOJSON'),
        (f'{base}_current_forest-warnregions.parquet',         config.PRODUCT_VHI['geocat_id_forest'],      'FOREST-WARNREGIONS-PARQUET'),
        (f'{base}_mosaic_current_vegetation-10m.tif',          config.PRODUCT_VHI['geocat_id_vegetation'],  'VEGETATION-10M'),
        (f'{base}_current_vegetation-warnregions.csv',         config.PRODUCT_VHI['geocat_id_vegetation'],  'VEGETATION-WARNREGIONS-CSV'),
        (f'{base}_current_vegetation-warnregions.geojson',     config.PRODUCT_VHI['geocat_id_vegetation'],  'VEGETATION-WARNREGIONS-GEOJSON'),
        (f'{base}_current_vegetation-warnregions.parquet',     config.PRODUCT_VHI['geocat_id_vegetation'],  'VEGETATION-WARNREGIONS-PARQUET'),
        (f'{base}_mosaic_current_metadata.json',               config.PRODUCT_VHI['geocat_id_vegetation'],  'Metadata'),
        ('thumbnail.png',                                      config.PRODUCT_VHI['geocat_id_vegetation'],  'Thumbnail'),
]

    for filename, geocat_id, band in assets:
        if not Path(filename).exists():
            print(f'  SKIPPING (not found): {filename}')
            continue
        print(f'  Publishing: {filename} as "{band}"')
        main_publish_stac_fsdi.publish_to_stac(
            filename,
            timestamp,
            product_name,
            geocat_id,
            asset_title=band,
            current=True
        )

    print(f'\nDone. Current item bootstrapped from {timestamp}.')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print('Usage: python tools/bootstrap_current_vhi.py dev_config.py YYYY-MM-DD')
        sys.exit(1)
    bootstrap_current(sys.argv[2])