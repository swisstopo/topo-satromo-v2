"""
main_terrain_parallel.py
========================
Parallel terrain processing pipeline for Switzerland DSM data.

Computes per-pixel solar incidence angle and shadow mask for one or more
Sentinel-2 orbit perimeters (or full Switzerland) and writes a single combined
GeoTIFF per run.
Developed initially by @stflury and DikshaAcharya as in https://github.com/swisstopo/topo-landschaftsgradient/tree/parallelism based on https://github.com/ChristianSteger/HORAYZON
Depends on main_terrain_module.py

Combined output encoding (uint8, EPSG:2056, 10 m resolution):
  0 - 180 : solar incidence angle in degrees (illuminated pixels)
  200     : shadow (cast shadow or self-shadow)
  255     : nodata (outside DSM extent or nodata in source)

Output filename convention: terrain_{orbit}_{timedate}.tif
  Example: terrain_08_2025-01-18t103351.tif

Can be used as a command-line script or called as a Python function:
  from main_terrain_parallel import main_terrain_parallel
  success = main_terrain_parallel("108", "2025-01-18t103351","output.tif")

Dependencies: rasterio, numpy, GDAL (osgeo), pyproj, fiona, skyfield,
              horayzon, subprocess, multiprocessing
"""


import datetime
import logging
import os
import sys
import subprocess
import tempfile
import numpy as np
import math
import fiona
import multiprocessing as mp
import rasterio
from rasterio.windows import from_bounds as window_from_bounds
from rasterio.merge import merge
from pathlib import Path
from argparse import ArgumentParser
from pyproj import CRS as PyprojCRS, Transformer
import configuration as config
from .main_terrain_module import HelperFunctions, InzidenWinkel, SonnenWinkel
#from main_functions.main_terrain_module import HelperFunctions, InzidenWinkel, SonnenWinkel
#from main_terrain_module import HelperFunctions, InzidenWinkel, SonnenWinkel


# ===========================================================================
# Configuration
# All paths and processing parameters are defined here.
# When calling main_terrain_parallel() programmatically, these values are used
# as defaults unless overridden by function arguments.
# ===========================================================================


CFG = {
    # --- Logging ---
    # Set to False to suppress INFO messages (WARNING and above are always shown)
    "log_info": False,

    # --- Input DSM ---
    # Full-Switzerland DSM in LV95 (EPSG:2056), 10 m resolution, Float32
    #Test
    #"dsm_path": r"D:\temp\github\topo-satromo-v2\local_assets\DSM_10m_EPSG2056_CH_clipped_10km_extended_9999.tif",
    #"dsm_path": os.path.join("/mnt/c/Users/Localadmin/Documents/SATROMO/topo-satromo-v2/topo-satromo-v2", "local_assets", "DSM_10m_EPSG2056_CH_clipped_10km_extended_9999.tif"),

    #PROD
    "dsm_path":config.DSM_FILE,

    # --- Skyfield ephemeris ---
    "planets": {
        #path: r"D:\temp\github\topo-satromo-v2\assets\planets",
        "path":     os.path.join("assets", "planets"),
        "bsp_file": "de421.bsp",
    },

    # --- EGM96 geoid data directory (for hray.geoid.undulation) ---
    #"egm_path": r"D:\temp\github\topo-satromo-v2\local_assets\EGM/",
    "egm_path": os.path.join("local_assets", "EGM") + os.sep,

    # --- Shadow search radius [m] ---
    # Must be >= maximum expected cast-shadow distance.
    # At sun elevation 18° a 2285 m peak casts a shadow ~5000 m away.
    # 20000 m provides a generous margin for winter conditions.
    "search_dist": 20000,

    # --- CH grid origin (LV95) ---
    # Southwest corner of the standard Swiss processing grid.
    # All tile origins are multiples of grid_size away from this point.

    #testing NIESEN
    #"ch_origin_east":  2604000,   # [m LV95]
    #"ch_origin_north": 1160000,   # [m LV95]

    #PROD
    "ch_origin_east":  2480000,   # [m LV95]
    "ch_origin_north": 1060000,   # [m LV95]

    # --- Grid dimensions for full Switzerland coverage ---
    #testing NIESEN
    #"n_e": 1,   # number of tiles in East direction  (1 x 20 km = 20 km)
    #"n_n": 1,   # number of tiles in North direction (1 x 20 km = 20 km)

    #PROD
    "n_e": 18,   # number of tiles in East direction  (18 x 20 km = 360 km)
    "n_n": 12,   # number of tiles in North direction (12 x 20 km = 240 km)

    # --- Tile parameters ---
    "grid_size": 20000,  # tile side length [m]
    "grid_step": 10,     # pixel resolution [m]

    # --- Multiprocessing ---
    #"n_proc": 8,   # number of parallel worker processes TODO: #min(len(tasks), os.cpu_count() - 1)
    "n_proc": min(os.cpu_count() - 1, 16),  # number of parallel worker processes, maxmum 16 to avoid overloading with HORAYZON/Embree

    # --- Perimeter GPKG files (one per Sentinel-2 orbit) ---
    "perimeters": {
        "CH":  None,
        "108": os.path.join("assets", "swissboundary_buffer_5000m_108.gpkg"),
        "22":  os.path.join("assets", "swissboundary_buffer_5000m_22.gpkg"),
        "65":  os.path.join("assets", "swissboundary_buffer_5000m_65.gpkg"),
        "8":   os.path.join("assets", "swissboundary_buffer_5000m_8.gpkg"),
    },
}

LOGLEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# ===========================================================================
# stdout suppression helper
# HORAYZON prints directly via C-level stdout (not Python logging), so the
# only way to silence it is to redirect the OS-level file descriptor 1.
# ===========================================================================

import contextlib

@contextlib.contextmanager
def _suppress_stdout():
    """
    Suppress HORAYZON stdout output for the duration of the block.

    HORAYZON prints via Python's sys.stdout (Cython print calls).
    On Windows, os.dup2 to NUL breaks debugpy's stdout wrapper
    (OSError WinError 1), so we only replace sys.stdout there.
    On Linux/Mac we also redirect the OS-level fd 1 to catch any
    C-level output from Embree.
    Active only when CFG['log_info'] is False.
    """
    if not CFG.get("log_info", True):
        old_stdout = sys.stdout
        null_file  = open(os.devnull, "w")
        sys.stdout = null_file
        # fd-level redirect for C extensions (Linux/Mac only)
        _old_fd = None
        if sys.platform != "win32":
            try:
                _old_fd = os.dup(1)
                os.dup2(null_file.fileno(), 1)
            except OSError:
                _old_fd = None
        try:
            yield
        finally:
            sys.stdout = old_stdout
            if _old_fd is not None:
                os.dup2(_old_fd, 1)
                os.close(_old_fd)
            null_file.close()
    else:
        yield


# ===========================================================================
# Logging helpers
# ===========================================================================

def setup_logging(level=logging.INFO, fmt="%(asctime)s [%(levelname)s] %(message)s",
                  logfolder=None):
    """
    Configure root logger with console handler and optional file handler.
    If CFG['log_info'] is False, the effective level is raised to WARNING,
    suppressing all INFO messages on console and in the log file.

    Args:
        level     : Logging level (e.g. logging.INFO)
        fmt       : Log message format string
        logfolder : Path object; if given, a timestamped .log file is created there
    """
    # Override level if INFO logging is disabled in config
    if not CFG.get("log_info", True) and level == logging.INFO:
        level = logging.WARNING

    # Bestehende Handler entfernen (wichtig wenn bereits konfiguriert)
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handlers = [logging.StreamHandler()]
    if logfolder:
        Path(logfolder).mkdir(parents=True, exist_ok=True)
        log_file = "{}_{}.log".format(
            os.path.splitext(os.path.basename(__file__))[0],
            datetime.datetime.now().strftime("%Y%m%d-%H%M%S"),
        )
        fh = logging.FileHandler(os.path.join(logfolder, log_file))
        fh.setFormatter(logging.Formatter(fmt))
        handlers.append(fh)

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)  # force=True erzwingt Neusetzen
    root.setLevel(level)  # explizit setzen

    logging.getLogger("rasterio").setLevel(logging.WARNING)
    logging.getLogger("pyproj").setLevel(logging.WARNING)
    logging.getLogger("skyfield").setLevel(logging.WARNING)
    # HORAYZON gibt "No rotation matrices" direkt via print() aus,
    # nicht via logging -> kann nicht mit logging unterdrueckt werden.
    # GDAL-Warnungen via gdal.PushErrorHandler unterdruecken:
    from osgeo import gdal
    gdal.PushErrorHandler("CPLQuietErrorHandler")


def log_listener_process(log_queue, log_path, loglvl=logging.INFO):
    """
    Standalone listener process that receives log records from worker processes
    via a multiprocessing Queue and writes them to the root logger.

    Terminates when it receives None from the queue (sentinel value).

    Args:
        log_queue : mp.Queue fed by worker processes
        log_path  : Path for logfile (passed to setup_logging)
        loglvl    : Logging level
    """
    setup_logging(level=loglvl, logfolder=log_path)
    while True:
        record = log_queue.get()
        if record is None:
            break
        logging.getLogger(record.name).handle(record)


# ===========================================================================
# Datetime parsing
# ===========================================================================

def parse_timedate_string(timedate_str):
    """
    Parse a timedate string in the format YYYY-MM-DDtHHMMSS to separate
    date and time strings accepted by HelperFunctions.parse_datetime.

    Args:
        timedate_str : e.g. "2025-01-18t103351"

    Returns:
        (date_str, time_str) : e.g. ("2025-01-18", "10:33:51")

    Raises:
        ValueError if the string does not match the expected format.
    """
    try:
        date_part, time_part = timedate_str.lower().split("t")
        if len(time_part) == 6:
            time_str = f"{time_part[0:2]}:{time_part[2:4]}:{time_part[4:6]}"
        elif len(time_part) == 4:
            time_str = f"{time_part[0:2]}:{time_part[2:4]}"
        else:
            raise ValueError
        return date_part, time_str
    except Exception:
        raise ValueError(
            f"Cannot parse timedate '{timedate_str}'. "
            "Expected format: YYYY-MM-DDtHHMMSS, e.g. 2025-01-18t103351"
        )


# ===========================================================================
# Grid and perimeter helpers
# ===========================================================================

def load_perimeter_bbox(gpkg_path):
    """
    Read the bounding box of a GeoPackage perimeter file and convert to LV95.

    Handles both old fiona (dict CRS) and new fiona (CRS object) APIs.

    Args:
        gpkg_path : Path to the .gpkg file

    Returns:
        (e_min, n_min, e_max, n_max) in LV95 [m]
    """
    with fiona.open(gpkg_path, layer=0) as src:
        bounds  = src.bounds   # (minx, miny, maxx, maxy)
        crs_obj = src.crs
        try:
            epsg     = PyprojCRS.from_user_input(crs_obj).to_epsg()
            src_epsg = str(epsg) if epsg else "4326"
        except Exception:
            raw      = crs_obj.get("init") if isinstance(crs_obj, dict) else ""
            src_epsg = raw.lower().replace("epsg:", "").strip() if raw else "4326"

    if src_epsg != "2056":
        tr = Transformer.from_crs(f"EPSG:{src_epsg}", "EPSG:2056", always_xy=True)
        e_min, n_min = tr.transform(bounds[0], bounds[1])
        e_max, n_max = tr.transform(bounds[2], bounds[3])
    else:
        e_min, n_min, e_max, n_max = bounds

    logging.info(
        f"Perimeter bbox LV95: E={e_min:.0f}-{e_max:.0f}, N={n_min:.0f}-{n_max:.0f}"
    )
    return e_min, n_min, e_max, n_max

# HORAYZON 1.2 hat path_to_aux_data entfernt und liest den Pfad stattdessen
# aus path_aux_data.txt. Diese Datei wird hier einmalig geschrieben damit
# Worker-Prozesse nicht input() aufrufen (was in multiprocessing fehlschlaegt).
import horayzon as _hray_setup
_path_horayzon = os.path.join(
    os.path.split(os.path.dirname(_hray_setup.__file__))[0], "horayzon"
)
_path_file = os.path.join(_path_horayzon, "path_aux_data.txt")
with open(_path_file, "w") as _f:
    _f.write(CFG["egm_path"])
logging.info(f"HORAYZON aux path: {CFG['egm_path']} -> {_path_file}")

def calc_grid_for_perimeter(perimeter_key, cfg):
    """
    Build the list of tile origin coordinates (E, N) in LV95 that cover the
    requested perimeter.

    For perimeter_key == 'CH' the full Switzerland grid is returned
    (n_e x n_n tiles aligned to ch_origin_east / ch_origin_north).

    For orbit perimeters the GPKG bounding box is read and tile origins are
    snapped to the CH grid so that tile boundaries always align with full-CH runs.

    Args:
        perimeter_key : 'CH', '108', '22', '65', or '8'
        cfg           : configuration dict (CFG)

    Returns:
        List of (easting, northing) tuples in LV95 [m]
    """
    grid_size = cfg["grid_size"]
    ch_e0     = cfg["ch_origin_east"]
    ch_n0     = cfg["ch_origin_north"]

    if perimeter_key == "CH":
        grid = []
        for ei in range(cfg["n_e"]):
            for ni in range(cfg["n_n"]):
                grid.append((ch_e0 + ei * grid_size, ch_n0 + ni * grid_size))
        logging.info(f"CH grid: {len(grid)} tiles ({cfg['n_e']} E x {cfg['n_n']} N)")
        return grid

    # Orbit perimeter: load GPKG bbox and snap to CH grid
    gpkg_path = cfg["perimeters"].get(perimeter_key)
    if not gpkg_path or not os.path.isfile(gpkg_path):
        raise FileNotFoundError(
            f"GPKG for perimeter '{perimeter_key}' not found: {gpkg_path}"
        )

    e_min, n_min, e_max, n_max = load_perimeter_bbox(gpkg_path)

    start_e = ch_e0 + math.floor((e_min - ch_e0) / grid_size) * grid_size
    start_n = ch_n0 + math.floor((n_min - ch_n0) / grid_size) * grid_size

    grid = []
    e = start_e
    while e < e_max:
        n = start_n
        while n < n_max:
            grid.append((e, n))
            n += grid_size
        e += grid_size

    logging.info(
        f"Perimeter '{perimeter_key}': {len(grid)} candidate tiles (before nodata filter)"
    )
    return grid


def tile_contains_valid_data(dsm_path, e, n, grid_size):
    """
    Check whether a DSM tile contains at least one valid (non-nodata) pixel.
    Used to skip tiles outside the DSM extent (e.g. outside Switzerland).

    Args:
        dsm_path  : Full path to the DSM GeoTIFF
        e         : Tile origin easting [m, LV95]
        n         : Tile origin northing [m, LV95]
        grid_size : Tile side length [m]

    Returns:
        True if the tile contains valid data, False otherwise.
    """
    with rasterio.open(dsm_path) as src:
        window = window_from_bounds(e, n, e + grid_size, n + grid_size, src.transform)
        if window.width <= 0 or window.height <= 0:
            return False
        data   = src.read(1, window=window)
        nodata = src.nodata
        if nodata is not None:
            return not np.all(data == nodata)
        return not np.all(np.isnan(data))


# ===========================================================================
# Per-tile worker function
# ===========================================================================

def run_tile(args, cfg, coord_tuple):
    """
    Process one DSM tile: compute incidence angle and shadow mask.

    This function is called by the multiprocessing pool. It initialises
    InzidenWinkel and SonnenWinkel for each tile, runs both computations,
    logs per-tile statistics, and returns the raw arrays.

    Args:
        args        : dict with keys 'date', 'time', 'grid_size', 'grid_step'
        cfg         : configuration dict (CFG)
        coord_tuple : (easting, northing) tile origin in LV95 [m]

    Returns:
        (inc_stack, inc_transform, inc_crs, inc_nodata,
         ilu_stack, ilu_transform, ilu_crs, ilu_nodata)

        inc_stack : float32 numpy array [1, H, W] with incidence angles
        ilu_stack : uint8  numpy array [1, H, W] with illumination mask
                    (0=shadow, 1=illuminated, 255=nodata)
    """
    tile_e, tile_n = coord_tuple
    logging.info(f"{os.getpid()} === Tile E={tile_e:.0f} N={tile_n:.0f} ===")

    try:
        # --- Incidence angle ---
        with _suppress_stdout():
            iw = InzidenWinkel(
                dom=cfg["dsm_path"],
                planets=cfg["planets"],
                output_path=".",
            )
            inc_tile, inc_transform, inc_nodata = iw.calc_incidence_grid(
                e_lv95=tile_e,
                n_lv95=tile_n,
                dateoi=args["date"],
                timeoi=args["time"],
                grid_size=args["grid_size"],
                grid_step=args["grid_step"],
            )
            iw.close()

        # Log incidence statistics (exclude nodata)
        valid = inc_tile[inc_tile > inc_nodata + 1]
        if valid.size > 0:
            logging.info(
                f"{os.getpid()} Incidence: min={valid.min():.1f}°, "
                f"max={valid.max():.1f}°, mean={valid.mean():.1f}°"
            )
        else:
            logging.warning(f"{os.getpid()} Incidence: no valid values")

        # --- Shadow / illumination mask ---
        with _suppress_stdout():
            sw = SonnenWinkel(
                dom=cfg["dsm_path"],
                planets=cfg["planets"],
                search_dist=cfg["search_dist"],
                output_path=".",
            )
            ilu_tile, ilu_transform, ilu_nodata = sw.calc_illuminate_grid(
                e_lv95=tile_e,
                n_lv95=tile_n,
                dateoi=args["date"],
                timeoi=args["time"],
                grid_size=args["grid_size"],
                grid_step=args["grid_step"],
            )
            sw.close()

        # Stack to [1, H, W] (single time-step; keeps merge_tiles compatible
        # with potential multi-time-step extensions)
        inc_stack = np.stack([inc_tile], axis=0)
        ilu_stack = np.stack([ilu_tile], axis=0)

        inc_crs = "EPSG:2056"
        ilu_crs = "EPSG:2056"

    except Exception:
        logging.exception(f"{os.getpid()} Error processing tile E={tile_e} N={tile_n}")
        raise

    return (
        inc_stack, inc_transform, inc_crs, inc_nodata,
        ilu_stack, ilu_transform, ilu_crs, ilu_nodata,
    )


# ===========================================================================
# Merge and output
# ===========================================================================

def merge_tiles(tile_results, band_index, nodata_value):
    """
    Merge a list of per-tile raster results into a single mosaic using rasterio.

    Tiles are placed into in-memory rasterio datasets, then merged with
    rasterio.merge. Overlapping areas use the 'first' strategy (last written wins).

    Args:
        tile_results : list of (stack, transform, crs, nodata) tuples
        band_index   : 0-based band index to extract from each stack (stack shape [B, H, W])
        nodata_value : nodata value to pass to rasterio.merge

    Returns:
        (mosaic, out_transform)
        mosaic        : numpy array [1, H, W]
        out_transform : rasterio Affine transform for the mosaic
    """
    src_files = []
    memfiles  = []

    for stack, transform, crs, nodata in tile_results:
        band  = stack[band_index]  # [H, W]
        mf    = rasterio.io.MemoryFile()
        memfiles.append(mf)
        ds = mf.open(
            driver="GTiff",
            height=band.shape[0], width=band.shape[1],
            count=1, dtype=band.dtype,
            transform=transform, crs=crs, nodata=nodata,
        )
        ds.write(band, 1)
        src_files.append(ds)

    mosaic, out_transform = merge(src_files, nodata=nodata_value)

    for ds in src_files:
        ds.close()
    for mf in memfiles:
        mf.close()

    return mosaic, out_transform


def combine_incidence_and_shadow(inc_mosaic, ilu_mosaic, inc_nodata, ilu_nodata):
    """
    Combine the incidence angle mosaic and the illumination mosaic into a single
    uint8 array using the following encoding:

      0 - 180 : incidence angle in degrees (illuminated pixels, rounded to integer)
      200     : shadow (ilu_mosaic == 0)
      255     : nodata (ilu_mosaic == 255 OR incidence == nodata)

    Both mosaics must have shape [1, H, W] and the same spatial extent.

    Args:
        inc_mosaic  : float32 array [1, H, W], incidence angles [deg], nodata=inc_nodata
        ilu_mosaic  : uint8  array [1, H, W], 0=shadow / 1=illuminated / 255=nodata
        inc_nodata  : nodata sentinel for inc_mosaic (typically -9999)
        ilu_nodata  : nodata sentinel for ilu_mosaic (typically 255)

    Returns:
        combined : uint8 numpy array [H, W]
    """
    inc = inc_mosaic[0]   # [H, W]
    ilu = ilu_mosaic[0]   # [H, W]

    combined = np.full(inc.shape, 255, dtype=np.uint8)  # default: nodata

    # Illuminated pixels: write incidence angle clamped to 0-180
    illuminated_mask = (ilu == 1)
    inc_clipped = np.clip(np.round(inc), 0, 180).astype(np.uint8)
    combined[illuminated_mask] = inc_clipped[illuminated_mask]

    # Shadow pixels: write 200
    shadow_mask = (ilu == 0)
    combined[shadow_mask] = 200

    # Nodata: already 255 by default; also enforce where incidence is nodata
    inc_nodata_mask = (inc <= inc_nodata + 1.0)
    combined[inc_nodata_mask] = 255

    n_illuminated = int(illuminated_mask.sum())
    n_shadow      = int(shadow_mask.sum())
    n_nodata      = int(np.sum(combined == 255))
    total         = combined.size
    logging.info(
        f"Combined raster: illuminated={n_illuminated} ({100*n_illuminated/total:.1f}%), "
        f"shadow={n_shadow} ({100*n_shadow/total:.1f}%), "
        f"nodata={n_nodata} ({100*n_nodata/total:.1f}%)"
    )
    return combined


def write_terrain_tif(combined, transform, output_tif, dsm_path):
    """
    Write the combined terrain raster to a GeoTIFF.

    Before export, pixels where the source DSM has nodata (-9999) are set to
    255 (nodata) in the combined raster. This ensures that areas outside the
    DSM extent are not falsely encoded as shadow (200) or incidence (0-180).

    Steps:
      1. Read DSM nodata mask for the combined raster extent
      2. Apply nodata mask to combined array
      3. Write intermediate uint8 GeoTIFF with rasterio (temp file)
      4. Run gdalwarp for DEFLATE compression, tiling and 10 m pixel alignment

    Args:
        combined   : uint8 numpy array [H, W]
        transform  : rasterio Affine transform (EPSG:2056)
        output_tif : Full path to the output GeoTIFF
        dsm_path   : Full path to the source DSM (used for nodata masking)
    """
    # --- Step 1: Apply DSM nodata mask ---
    # Read the DSM window that corresponds to the combined raster extent.
    # Where DSM == -9999 (nodata), force combined to 255 (nodata).
    h, w = combined.shape
    x_min = transform.c
    y_max = transform.f
    x_max = x_min + w * transform.a
    y_min = y_max + h * transform.e   # transform.e is negative

    with rasterio.open(dsm_path) as dsm_src:
        from rasterio.windows import from_bounds as wfb
        dsm_nodata = dsm_src.nodata if dsm_src.nodata is not None else -9999.0
        window = wfb(x_min, y_min, x_max, y_max, dsm_src.transform)
        window = window.round_offsets().round_lengths()

        dsm_tile = dsm_src.read(1, window=window, out_shape=(h, w),
                                resampling=rasterio.enums.Resampling.nearest)

    nodata_mask = (dsm_tile == dsm_nodata)
    n_masked = int(nodata_mask.sum())
    logging.info(f"DSM nodata mask applied: {n_masked} pixels set to 255")
    combined = combined.copy()
    combined[nodata_mask] = 255

    # --- Step 2: Write intermediate temp file ---
    tmp_dir = os.path.dirname(os.path.abspath(output_tif)) or "."
    tmp_fd, tmp_path = tempfile.mkstemp(suffix="_tmp.tif", dir=tmp_dir)
    os.close(tmp_fd)

    with rasterio.open(
        tmp_path, "w",
        driver="GTiff",
        height=combined.shape[0], width=combined.shape[1],
        count=1, dtype="uint8",
        crs="EPSG:2056",
        transform=transform,
        nodata=255,
    ) as dst:
        dst.write(combined, 1)
        dst.set_band_description(1, "terrain_incidence_shadow")
        dst.update_tags(1, ENCODING="0-180=incidence_deg, 200=shadow, 255=nodata")

    # --- Step 3: gdalwarp fuer Pixelausrichtung (Zwischendatei) ---
    tmp2_fd, tmp2_path = tempfile.mkstemp(suffix="_aligned.tif", dir=tmp_dir)
    os.close(tmp2_fd)

    cmd_warp = [
        "gdalwarp",
        "-of",      "GTiff",
        "-co",      "TILED=YES",
        "-co",      "BLOCKXSIZE=256",
        "-co",      "BLOCKYSIZE=256",
        "-tr",      "10", "10",
        "-tap",
        "-r",       "near",
        "-ot",      "Byte",
        "-overwrite",
        tmp_path,
        tmp2_path,
    ]
    logging.info(f"gdalwarp: {' '.join(cmd_warp)}")
    result = subprocess.run(cmd_warp, check=True, capture_output=True)
    if result.stderr:
        logging.debug(f"gdalwarp stderr: {result.stderr.decode()}")
     # Sicher loeschen: nur wenn Datei noch existiert
    if os.path.isfile(tmp_path):
        os.remove(tmp_path)
    else:
        logging.warning(f"tmp_path already gone: {tmp_path}")

    # --- Step 4: Overviews hinzufuegen (Pflicht fuer COG) ---
    cmd_ovr = [
        "gdaladdo",
        "-r",      "nearest",
        tmp2_path,
        "2", "4", "8", "16", "32",
    ]
    logging.info(f"gdaladdo: {' '.join(cmd_ovr)}")
    result = subprocess.run(cmd_ovr, check=True, capture_output=True)
    if result.stderr:
        logging.debug(f"gdaladdo stderr: {result.stderr.decode()}")

    # --- Step 5: COG schreiben ---
    if os.path.isfile(output_tif):
        os.remove(output_tif)

    cmd_cog = [
        "gdal_translate",
        "-of",      "COG",
        "-co",      "COMPRESS=DEFLATE",
        "-co",      "BLOCKSIZE=256",
        "-co",      "RESAMPLING=NEAREST",
        "-co",      "OVERVIEWS=IGNORE_EXISTING",
        "-co",      "BIGTIFF=YES",
        "-co",      "NUM_THREADS=ALL_CPUS",
        "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
        "-ot",      "Byte",
        tmp2_path,
        output_tif,
    ]
    logging.info(f"gdal_translate COG: {' '.join(cmd_cog)}")
    result = subprocess.run(cmd_cog, check=True, capture_output=True)
    if result.stderr:
        logging.debug(f"gdal_translate stderr: {result.stderr.decode()}")

    if os.path.isfile(tmp2_path):
        os.remove(tmp2_path)
    else:
        logging.warning(f"tmp2_path already gone: {tmp2_path}")

    logging.info(f"COG terrain GeoTIFF written: {output_tif}")

    if os.path.isfile(tmp_path):
        os.remove(tmp_path)
    logging.info(f"Terrain GeoTIFF written: {output_tif}")


# ===========================================================================
# Main callable function
# ===========================================================================

def create_terrain_mask(orbit, timedate, outputfilename=None, sequential=False):
    """
    Run the full terrain illumination pipeline for one orbit and acquisition time.

    Computes incidence angle and shadow mask for all valid DSM tiles covering
    the requested orbit perimeter, merges the tiles into a Switzerland-wide mosaic,
    and writes a single combined GeoTIFF.

    Args:
        orbit          : Sentinel-2 orbit identifier as string: '108', '22', '65', '8'
                         or 'CH' for full Switzerland.
        timedate       : Acquisition date and time in UTC, format YYYY-MM-DDtHHMMSS.
                         Example: "2025-01-18t103351"
        outputfilename : Optional full path for the output GeoTIFF.
                         If None, the file is written to the current working directory
                         with the name terrain_{orbit}_{timedate}.tif
        sequential : if True, tiles are processed one by one without
                     multiprocessing. Use this when calling from another
                     script (e.g. util_terrain_backfill.py) to avoid
                     nested multiprocessing issues with HORAYZON/Embree.

    Returns:
        True  if the output file was created successfully.
        False if an error occurred (details are logged).

    Example:
        from main_terrain_parallel import main_terrain_parallel
        ok = main_terrain_parallel("108", "2023-12-25t103441")
        ok = main_terrain_parallel("108", "2023-12-25t103441", "my_output.tif")
    """
    try:
        # --- Parse timedate string ---
        date_str, time_str = parse_timedate_string(timedate)

        # --- Resolve output path ---
        if outputfilename is None:
            outputfilename = f"terrain_{orbit}_{timedate}.tif"
        # If outputfilename contains a directory path, create it if needed
        out_dir = os.path.dirname(outputfilename)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        # --- Set up logging (console only, no logfile) ---
        loglvl = logging.INFO
        setup_logging(level=loglvl, logfolder=None)

        # --- Build args dict for worker processes ---
        args = {
            "date":      date_str,
            "time":      time_str,
            "east":      CFG["ch_origin_east"],
            "north":     CFG["ch_origin_north"],
            "grid_size": CFG["grid_size"],
            "grid_step": CFG["grid_step"],
        }

        logging.info("=" * 60)
        logging.info("Terrain processing start")
        logging.info(f"  Orbit    : {orbit}")
        logging.info(f"  Date UTC : {date_str}  Time UTC : {time_str}")
        logging.info(f"  Output   : {outputfilename}")
        logging.info("=" * 60)

        # --- Build tile grid ---
        all_tiles   = calc_grid_for_perimeter(orbit, CFG)
        valid_tiles = [
            t for t in all_tiles
            if tile_contains_valid_data(CFG["dsm_path"], t[0], t[1], CFG["grid_size"])
        ]
        logging.info(
            f"Tiles total={len(all_tiles)}, valid={len(valid_tiles)}, "
            f"skipped (nodata)={len(all_tiles) - len(valid_tiles)}"
        )

        if not valid_tiles:
            logging.error("No valid tiles found for this perimeter. Aborting.")
            return False

        # --- Parallel oder sequenziell ---
        tasks  = [(args, CFG, t) for t in valid_tiles]
        n_proc = min(CFG["n_proc"], len(tasks))

        if sequential:
            # Kein Pool: tiles nacheinander verarbeiten
            logging.info(f"Processing {len(tasks)} tiles sequentially (no multiprocessing)...")
            results = [run_tile(*task) for task in tasks]
        else:
            logging.info(f"Starting {len(tasks)} tiles on {n_proc} workers...")
            # "fork" on Linux/Mac: workers inherit the parent's address space,
            # so native extensions like _gdal_array are already loaded and don't
            # need to be re-imported (avoids ImportError with venv GDAL builds).
            # "spawn" on Windows: fork is not available there.
            _ctx_method = "fork" if sys.platform != "win32" else "spawn"
            ctx = mp.get_context(_ctx_method)
            with ctx.Pool(
                processes=n_proc,
                initializer=setup_logging,
                initargs=(logging.INFO,),
            ) as pool:
                try:
                    results = pool.starmap(run_tile, tasks)
                except KeyboardInterrupt:
                    logging.warning("KeyboardInterrupt: terminating workers")
                    pool.terminate()
                    pool.join()
                    return False

        # --- Merge incidence tiles ---
        logging.info("Merging incidence angle tiles...")
        inc_results = [(r[0], r[1], r[2], r[3]) for r in results]
        inc_mosaic, inc_transform = merge_tiles(
            inc_results, band_index=0, nodata_value=float(results[0][3])
        )

        # --- Merge illumination tiles ---
        logging.info("Merging illumination mask tiles...")
        ilu_results = [(r[4], r[5], r[6], r[7]) for r in results]
        ilu_mosaic, ilu_transform = merge_tiles(
            ilu_results, band_index=0, nodata_value=255
        )

        # --- Combine into single encoded raster ---
        logging.info("Combining incidence and shadow into terrain raster...")
        combined = combine_incidence_and_shadow(
            inc_mosaic, ilu_mosaic,
            inc_nodata=float(results[0][3]),
            ilu_nodata=255,
        )

        # --- Write final GeoTIFF ---
        logging.info(f"Writing output: {outputfilename}")
        write_terrain_tif(combined, inc_transform, outputfilename, CFG["dsm_path"])

        # --- Verify output ---
        if not os.path.isfile(outputfilename):
            logging.error(f"Output file not found after writing: {outputfilename}")
            return False

        file_mb = os.path.getsize(outputfilename) / 1024 / 1024
        logging.info(f"Done. Output: {outputfilename} ({file_mb:.1f} MB)")
        return True

    except Exception as exc:
        logging.exception(f"create_terrain_mask failed: {exc}")
        return False


# ===========================================================================
# Command-line entry point
# ===========================================================================

def parse_args():
    parser = ArgumentParser(
        description="Terrain illumination pipeline (incidence angle + shadow mask)"
    )
    parser.add_argument(
        "--orbit", "-o",
        default="CH",
        choices=["CH", "8", "108", "65", "22"],
        help="Perimeter: 'CH' for full Switzerland, or Sentinel-2 orbit ID",
    )
    parser.add_argument(
        "--timedate", "-td",
        default="2023-12-25t103441",
        type=str,
        help="UTC date+time, format YYYY-MM-DDtHHMMSS",
    )
    parser.add_argument(
        "--output", "-out",
        default=None,
        type=str,
        help="Output GeoTIFF path (optional)",
    )
    parser.add_argument(
        "--loglevel",
        choices=LOGLEVELS,
        default="INFO",
    )
    return vars(parser.parse_args())


if __name__ == "__main__":
    __args = parse_args()
    setup_logging(level=getattr(logging, __args["loglevel"]))

    success = create_terrain_mask(
        orbit=__args["orbit"],
        timedate=__args["timedate"],
        outputfilename=__args["output"],
    )
    sys.exit(0 if success else 1)