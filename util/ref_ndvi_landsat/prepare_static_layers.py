# Prepares the layers that do not change between dates, all in EPSG:2056:
# - slope from swissALTI3D, calculated at 10 m and then written to the 30 m grid
# - share of each 30 m pixel covered by the VHI vegetation and forest masks
# - hard surfaces from swissTLM3D (paved runways and aprons, large roofs)
#
# The swissTLM3D shapefile archive must already be in output/static (see README.md).
# The files are written to output/static and only need to be created once.
#
# Usage: python util/ref_ndvi_landsat/prepare_static_layers.py

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.windows import Window

import vi_reference_config as config

DEM_RESOLUTION = 10
DEM_NODATA = -9999
TILE_SIZE_METERS = 1000
TILE_SIZE_PIXELS = TILE_SIZE_METERS // DEM_RESOLUTION
DEM_WORKERS = 16

# Without a timeout, a stalled download waits forever and the whole run hangs
os.environ["GDAL_HTTP_TIMEOUT"] = "60"
os.environ["GDAL_HTTP_CONNECTTIMEOUT"] = "30"
os.environ["GDAL_HTTP_MAX_RETRY"] = "5"
os.environ["GDAL_HTTP_RETRY_DELAY"] = "5"
os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"


def run_command(command, extra_env=None):
    print(" ".join(command))
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    subprocess.run(command, check=True, env=env)


def get_swissalti3d_tiles():
    """Return a dictionary {(east_km, north_km): url of the newest 2 m tile}."""
    tiles = {}
    tile_years = {}
    url = config.SWISSALTI3D_ITEMS_URL + "?limit=100"
    pages = 0
    while url:
        page = requests.get(url, timeout=120).json()
        pages += 1
        if pages % 100 == 0:
            print(f"Read {pages} STAC pages, {len(tiles)} tiles so far")
        for item in page["features"]:
            match = re.match(r"swissalti3d_(\d{4})_(\d{4})-(\d{4})", item["id"])
            year, east_km, north_km = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if year <= tile_years.get((east_km, north_km), 0):
                continue
            for name, asset in item["assets"].items():
                if name.endswith("_2_2056_5728.tif"):
                    tiles[(east_km, north_km)] = asset["href"]
                    tile_years[(east_km, north_km)] = year
        url = next((link["href"] for link in page["links"] if link["rel"] == "next"), None)
    print(f"Found {len(tiles)} swissALTI3D tiles")
    return tiles


def read_tile_at_10m(tile):
    (east_km, north_km), url = tile
    # The tile overviews are used, so only a small part of each file is downloaded
    with rasterio.open(url) as src:
        data = src.read(1, out_shape=(TILE_SIZE_PIXELS, TILE_SIZE_PIXELS), resampling=Resampling.average)
    return east_km, north_km, data.astype(np.float32)


def make_dem():
    dem_file = os.path.join(config.STATIC_DIR, "swissalti3d_2056_10m.tif")
    if os.path.exists(dem_file):
        return dem_file

    width = (config.GRID_RIGHT - config.GRID_LEFT) // DEM_RESOLUTION
    height = (config.GRID_TOP - config.GRID_BOTTOM) // DEM_RESOLUTION
    transform = from_origin(config.GRID_LEFT, config.GRID_TOP, DEM_RESOLUTION, DEM_RESOLUTION)
    tiles = get_swissalti3d_tiles()

    temp_file = dem_file + ".part"
    with rasterio.open(temp_file, "w", driver="GTiff", width=width, height=height, count=1, dtype="float32",
                       crs=config.GRID_CRS, transform=transform, nodata=DEM_NODATA, tiled=True,
                       compress="deflate", predictor=3, bigtiff="yes") as dst:
        with ThreadPoolExecutor(max_workers=DEM_WORKERS) as executor:
            for number, (east_km, north_km, data) in enumerate(executor.map(read_tile_at_10m, tiles.items()), start=1):
                col = (east_km * TILE_SIZE_METERS - config.GRID_LEFT) // DEM_RESOLUTION
                row = (config.GRID_TOP - (north_km + 1) * TILE_SIZE_METERS) // DEM_RESOLUTION
                # Tiles at the border can reach past the grid, so only the part inside is written
                first_col, last_col = max(col, 0), min(col + TILE_SIZE_PIXELS, width)
                first_row, last_row = max(row, 0), min(row + TILE_SIZE_PIXELS, height)
                if first_col < last_col and first_row < last_row:
                    tile_part = data[first_row - row:last_row - row, first_col - col:last_col - col]
                    window = Window(first_col, first_row, last_col - first_col, last_row - first_row)
                    dst.write(tile_part, 1, window=window)
                if number % 2000 == 0:
                    print(f"Wrote {number} of {len(tiles)} tiles")
    os.replace(temp_file, dem_file)
    return dem_file


def make_slope(dem_file):
    slope_30m_file = os.path.join(config.STATIC_DIR, "slope_max_2056_30m.tif")
    if os.path.exists(slope_30m_file):
        return

    slope_10m_file = os.path.join(config.STATIC_DIR, "slope_2056_10m.tif")
    run_command(["gdaldem", "slope", dem_file, slope_10m_file, "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES",
                 "-co", "BIGTIFF=YES"])
    # A 30 m pixel only counts as flat if all of its 10 m pixels are flat, so the maximum is used
    run_command(["gdalwarp", "-overwrite", "-tr", str(config.GRID_RESOLUTION), str(config.GRID_RESOLUTION),
                 "-te", str(config.GRID_LEFT), str(config.GRID_BOTTOM), str(config.GRID_RIGHT), str(config.GRID_TOP),
                 "-r", "max", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", slope_10m_file, slope_30m_file])


def make_terrain_30m(dem_file):
    """Slope and aspect on the 30 m grid, for the topographic correction in step 4.
    They are calculated from the DEM averaged to 30 m, so they describe the whole Landsat pixel."""
    dem_30m_file = os.path.join(config.STATIC_DIR, "swissalti3d_2056_30m.tif")
    slope_file = os.path.join(config.STATIC_DIR, "slope_2056_30m.tif")
    aspect_file = os.path.join(config.STATIC_DIR, "aspect_2056_30m.tif")
    if os.path.exists(slope_file) and os.path.exists(aspect_file):
        return

    run_command(["gdalwarp", "-overwrite", "-tr", str(config.GRID_RESOLUTION), str(config.GRID_RESOLUTION),
                 "-te", str(config.GRID_LEFT), str(config.GRID_BOTTOM), str(config.GRID_RIGHT), str(config.GRID_TOP),
                 "-r", "average", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", dem_file, dem_30m_file])
    run_command(["gdaldem", "slope", dem_30m_file, slope_file, "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES"])
    # -zero_for_flat: flat pixels get aspect 0 instead of nodata. Their aspect does not matter, as the slope is 0
    run_command(["gdaldem", "aspect", dem_30m_file, aspect_file, "-zero_for_flat",
                 "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES"])


def rasterize_on_grid(geometries, output_name):
    """Write a uint8 file on the 30 m grid: 1 inside the geometries, 0 outside."""
    output_file = os.path.join(config.STATIC_DIR, output_name)
    if os.path.exists(output_file):
        return
    transform = from_origin(config.GRID_LEFT, config.GRID_TOP, config.GRID_RESOLUTION, config.GRID_RESOLUTION)
    mask = rasterize(geometries, out_shape=(config.GRID_HEIGHT, config.GRID_WIDTH), transform=transform,
                     fill=0, default_value=1, dtype=np.uint8)
    with rasterio.open(output_file, "w", driver="GTiff", width=config.GRID_WIDTH, height=config.GRID_HEIGHT,
                       count=1, dtype="uint8", crs=config.GRID_CRS, transform=transform,
                       tiled=True, compress="deflate") as dst:
        dst.write(mask, 1)


def make_grid_masks():
    # Lakes: water is excluded from the NDSI snow mask, as in step 1
    lakes = gpd.read_file(config.LAKES_FILE).to_crs(config.GRID_CRS)
    rasterize_on_grid(lakes.geometry, "lakes_2056_30m.tif")
    # Switzerland with a 5 km buffer: the area of the reference files, as in the GEE script
    swiss_buffer = gpd.read_file(config.SWISS_BUFFER_FILE).to_crs(config.GRID_CRS)
    rasterize_on_grid(swiss_buffer.geometry, "swiss_buffer_2056_30m.tif")


def make_mask_fraction(mask_url, output_name):
    output_file = os.path.join(config.STATIC_DIR, output_name)
    if os.path.exists(output_file):
        return

    # -srcnodata None: 0 means "not in the mask" here, so it must count in the average
    run_command(["gdalwarp", "-overwrite", "-srcnodata", "None",
                 "-te", str(config.GRID_LEFT), str(config.GRID_BOTTOM), str(config.GRID_RIGHT), str(config.GRID_TOP),
                 "-tr", str(config.GRID_RESOLUTION), str(config.GRID_RESOLUTION), "-r", "average", "-ot", "Float32",
                 "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", mask_url, output_file],
                extra_env={"AWS_NO_SIGN_REQUEST": "YES", "AWS_REGION": "eu-central-2"})


def make_hard_surfaces():
    """Collect paved runways, paved aprons and large roofs from swissTLM3D."""
    output_file = os.path.join(config.STATIC_DIR, "hard_surfaces_2056.gpkg")
    if os.path.exists(output_file):
        return

    zip_path = "zip://" + config.SWISSTLM3D_ZIP_FILE + "!"
    traffic = gpd.read_file(zip_path + "TLM_BAUTEN/swissTLM3D_TLM_VERKEHRSBAUTE_PLY.shp", columns=["OBJEKTART"])
    traffic = traffic[traffic["OBJEKTART"].isin(["Hartbelagpiste", "Rollfeld Hartbelag"])]
    traffic = traffic.rename(columns={"OBJEKTART": "kind"})

    roofs = []
    for part in ["OST", "WEST"]:
        print(f"Reading swissTLM3D buildings {part}")
        roofs.append(gpd.read_file(zip_path + f"TLM_BAUTEN/swissTLM3D_TLM_GEBAEUDE_FOOTPRINT_{part}.shp",
                                   columns=[], where=f"OGR_GEOM_AREA >= {config.MIN_ROOF_AREA_M2}"))
    roofs = pd.concat(roofs, ignore_index=True)
    roofs["kind"] = "Dach"
    # Roofs shrink, so that no pixel touches their edge
    roofs["geometry"] = roofs.geometry.buffer(-config.ROOF_EDGE_BUFFER_METERS)

    surfaces = pd.concat([traffic[["kind", "geometry"]], roofs[["kind", "geometry"]]], ignore_index=True)
    # swissTLM3D is LV95 plus a height system. The x and y values are already EPSG:2056
    surfaces = gpd.GeoDataFrame(surfaces).set_crs(config.GRID_CRS, allow_override=True)
    surfaces = surfaces[~surfaces.geometry.is_empty]
    print(surfaces["kind"].value_counts())
    surfaces.to_file(output_file, driver="GPKG")


def main():
    os.makedirs(config.STATIC_DIR, exist_ok=True)
    dem_file = make_dem()
    make_slope(dem_file)
    make_terrain_30m(dem_file)
    make_grid_masks()
    make_mask_fraction(config.VEGETATION_MASK_URL, "vegetation_fraction_2056_30m.tif")
    make_mask_fraction(config.FOREST_MASK_URL, "forest_fraction_2056_30m.tif")
    make_hard_surfaces()
    print("Static layers are ready in", config.STATIC_DIR)


if __name__ == "__main__":
    main()
