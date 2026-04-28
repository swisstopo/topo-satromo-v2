"""
main_terrain_module.py
======================
Terrain illumination and incidence angle computation for Switzerland DSM data.
Developed initially by @stflury and DikshaAcharya as in https://github.com/swisstopo/topo-landschaftsgradient/tree/parallelism based on https://github.com/ChristianSteger/HORAYZON inspired by https://github.com/ChristianSteger/HORAYZON/blob/main/examples/shadow/gridded_curved_DEM_SRTM.py

Provides four classes:
  - HelperFunctions : Static coordinate conversion, sun position and incidence angle utilities
  - DOM_sw          : Loads and reprojects DSM for shadow computation (used by SonnenWinkel)
  - SonnenWinkel    : Computes binary shadow/illumination mask using HORAYZON ray casting
  - DOM_iw          : Loads DSM metadata for incidence angle computation (used by InzidenWinkel)
  - InzidenWinkel   : Computes per-pixel solar incidence angle from slope/aspect and sun position

And one standalone function:
  - manual_curved_grid : Replaces hray.domain.curved_grid (broken in HORAYZON 1.2),
                         computes the outer DEM domain with correct ellipsoidal curvature.

Known issues fixed vs. original iluina_module.py:
  1. curved_grid (HORAYZON 1.2) returns a global domain -> replaced by manual_curved_grid
  2. gdal.Warp xRes/yRes used identical degree value for lon and lat -> corrected with cos(lat)
  3. float32 precision loss in sun_position vector when using astronomical distance d.m
     (~1.5e11 m) -> replaced by normalised direction vector scaled to search_dist * 10
  4. ENU origin was set to centre of outer domain -> fixed to centre of inner domain
  5. EGM path was hardcoded -> now passed as parameter egm_path to SonnenWinkel

Coordinate systems:
  Input DSM : EPSG:2056 (CH1903+ / LV95)
  Internal  : WGS84 (EPSG:4326) for HORAYZON
  Output    : EPSG:2056

Dependencies: rasterio, numpy, horayzon, osgeo (GDAL), pyproj, skyfield, math
"""

import logging
import os
import math
import time

import numpy as np
import rasterio
import horayzon as hray
from rasterio.windows import from_bounds as window_from_bounds
from rasterio.transform import Affine
from osgeo import gdal, osr
from pyproj import CRS, Transformer
from datetime import datetime, timezone
from skyfield.api import load, wgs84


# ---------------------------------------------------------------------------
# Standalone helper function: curved grid replacement
# ---------------------------------------------------------------------------

def manual_curved_grid(domain, search_dist, ellps="WGS84"):
    """
    Replacement for hray.domain.curved_grid (broken in HORAYZON 1.2).

    Computes the outer WGS84 bounding box for HORAYZON ray casting, taking
    into account the curvature of the WGS84 ellipsoid. Correct E-W and N-S
    buffer sizes are computed separately using the Meridian and Normal radii
    of curvature at the domain centre latitude.

    Args:
        domain      : dict with keys lon_min, lon_max, lat_min, lat_max [degrees WGS84]
        search_dist : maximum shadow search radius [m] (= HORAYZON search_dist)
        ellps       : ellipsoid identifier, only 'WGS84' is implemented

    Returns:
        domain_outer : dict with keys lon_min, lon_max, lat_min, lat_max [degrees WGS84]

    Raises:
        ValueError if the computed domain is larger than 5 degrees in any direction
                   (safety check against numerical errors).
    """
    # WGS84 ellipsoid parameters
    a = 6378137.0           # semi-major axis [m]
    f = 1.0 / 298.257223563 # flattening
    b = a * (1.0 - f)       # semi-minor axis [m]

    lat_center = (domain["lat_min"] + domain["lat_max"]) / 2.0
    lat_rad    = math.radians(lat_center)
    e2         = 1.0 - (b / a) ** 2

    # Meridian radius of curvature M (N-S direction)
    M_rad = a * (1.0 - e2) / (1.0 - e2 * math.sin(lat_rad) ** 2) ** 1.5

    # Normal radius of curvature N (E-W direction)
    N_rad = a / math.sqrt(1.0 - e2 * math.sin(lat_rad) ** 2)

    # Angular buffer in degrees for the flat-earth part of search_dist
    delta_lat_deg = math.degrees(search_dist / M_rad)
    delta_lon_deg = math.degrees(search_dist / (N_rad * math.cos(lat_rad)))

    # Additional curvature correction: sagitta = d^2 / (2*R)
    # At 20000 m search_dist this adds ~0.9 m, kept as conservative safety margin.
    curvature_m       = (search_dist / 1000.0) ** 2 * 0.5   # [m]
    curvature_lat_deg = math.degrees(curvature_m / M_rad)
    curvature_lon_deg = math.degrees(curvature_m / (N_rad * math.cos(lat_rad)))

    domain_outer = {
        "lon_min": domain["lon_min"] - delta_lon_deg - curvature_lon_deg,
        "lon_max": domain["lon_max"] + delta_lon_deg + curvature_lon_deg,
        "lat_min": domain["lat_min"] - delta_lat_deg - curvature_lat_deg,
        "lat_max": domain["lat_max"] + delta_lat_deg + curvature_lat_deg,
    }

    lon_span = domain_outer["lon_max"] - domain_outer["lon_min"]
    lat_span = domain_outer["lat_max"] - domain_outer["lat_min"]
    if lon_span > 5.0 or lat_span > 5.0:
        raise ValueError(
            f"manual_curved_grid: unrealistic domain "
            f"(lon_span={lon_span:.2f}, lat_span={lat_span:.2f})"
        )

    return domain_outer


# ---------------------------------------------------------------------------
# HelperFunctions
# ---------------------------------------------------------------------------

class HelperFunctions:
    """
    Static utility methods for coordinate conversion, datetime parsing,
    sun position computation and incidence angle calculation.
    All methods are static; no instantiation required.
    """

    def lv95_to_wgs84(e_lv95, n_lv95):
        """
        Convert LV95 (EPSG:2056) coordinates to WGS84 (EPSG:4326).

        Args:
            e_lv95 : Easting in LV95 [m]
            n_lv95 : Northing in LV95 [m]

        Returns:
            (lon, lat) in degrees WGS84
        """
        transformer = Transformer.from_crs(
            CRS.from_epsg(2056), CRS.from_epsg(4326), always_xy=True
        )
        return transformer.transform(e_lv95, n_lv95)

    def wgs84_to_lv95(lon, lat):
        """
        Convert WGS84 (EPSG:4326) coordinates to LV95 (EPSG:2056).

        Args:
            lon : Longitude in degrees WGS84
            lat : Latitude in degrees WGS84

        Returns:
            (easting, northing) in LV95 [m]
        """
        transformer = Transformer.from_crs(
            CRS.from_epsg(4326), CRS.from_epsg(2056), always_xy=True
        )
        return transformer.transform(lon, lat)

    def parse_datetime(dateoi, timeoi):
        """
        Parse date and time strings to a timezone-aware UTC datetime object.

        Accepted date formats : DD.MM.YYYY or YYYY-MM-DD
        Accepted time formats : HH:MM:SS or HH:MM
        The input is assumed to be UTC.

        Args:
            dateoi : Date string
            timeoi : Time string

        Returns:
            datetime object with tzinfo=UTC

        Raises:
            ValueError if no supported format matches.
        """
        dt_str = f"{dateoi} {timeoi}"
        for fmt in [
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]:
            try:
                dt_local = datetime.strptime(dt_str, fmt)
                return dt_local.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        raise ValueError(f"Could not parse date/time: {dt_str}")

    def calc_sunpos(lat_loc, lon_loc, dt_utc, planets_path, planets_file):
        """
        Compute apparent solar altitude and azimuth at a given location and time.

        Uses the Skyfield library with the JPL DE421 ephemeris file.

        Args:
            lat_loc      : Observer latitude [degrees WGS84]
            lon_loc      : Observer longitude [degrees WGS84]
            dt_utc       : Observation time as UTC-aware datetime
            planets_path : Directory containing the .bsp ephemeris file
            planets_file : Filename of the .bsp ephemeris file (e.g. 'de421.bsp')

        Returns:
            (altitude_deg, azimuth_deg) : Solar altitude and azimuth in degrees.
            Azimuth is measured clockwise from North (0=N, 90=E, 180=S, 270=W).
        """
        logging.info(f"{os.getpid()} Computing sun position...")
        load.directory = planets_path
        planets  = load(planets_file)
        loc_or   = planets["earth"] + wgs84.latlon(lat_loc, lon_loc)
        ts       = load.timescale()
        t        = ts.from_datetime(dt_utc)
        astrometric = loc_or.at(t).observe(planets["sun"])
        alt, az, _  = astrometric.apparent().altaz()
        return alt.degrees, az.degrees

    def calculate_incidence_angle(slope_deg, aspect_deg, sun_elev_deg, sun_az_deg):
        """
        Calculate the solar incidence angle (theta) on a tilted surface.

        The incidence angle is the angle between the incoming sun ray and the
        surface normal vector.

        Interpretation:
          0 deg  : sun perpendicular to surface (maximum direct irradiance)
          90 deg : sun parallel to surface (grazing incidence, no direct irradiance)
          >90 deg: sun is behind the surface (self-shading, back-illumination)

        Args:
            slope_deg    : Terrain slope [degrees], 0=flat, 90=vertical cliff
            aspect_deg   : Terrain aspect [degrees from North, clockwise]:
                           0=N, 90=E, 180=S, 270=W
            sun_elev_deg : Solar elevation angle above horizon [degrees]
            sun_az_deg   : Solar azimuth [degrees from North, clockwise]

        Returns:
            theta : Incidence angle [degrees], numpy array same shape as inputs
        """
        beta  = np.radians(slope_deg)
        gamma = np.radians(aspect_deg)
        alpha = np.radians(sun_elev_deg)
        A     = np.radians(sun_az_deg)

        cos_theta = np.sin(alpha) * np.cos(beta)
        cos_theta += np.cos(alpha) * np.sin(beta) * np.cos(A - gamma)
        cos_theta = np.clip(cos_theta, -1.0, 1.0)
        return np.degrees(np.arccos(cos_theta))


# ---------------------------------------------------------------------------
# DOM_sw  –  DSM handling for shadow computation
# ---------------------------------------------------------------------------

class DOM_sw:
    """
    Loads and reprojects the DSM for use by SonnenWinkel (shadow computation).

    The DSM is stored in LV95 (EPSG:2056). For HORAYZON ray casting it must
    be reprojected to WGS84 with square pixels and an outer buffer large enough
    to capture all shadow-casting terrain within search_dist.
    """

    def __init__(self, dom, search_dist, output_path):
        """
        Args:
            dom         : Full path to the DSM GeoTIFF (EPSG:2056)
            search_dist : Maximum shadow search radius [m]
            output_path : Directory for output files (existence is checked)
        """
        self.__dom         = dom
        self.__search_dist = search_dist
        self.__output_path = output_path
        self.__checkinput()

    def __checkinput(self):
        if not os.path.isfile(self.__dom):
            raise AttributeError(f"DSM file not found: {self.__dom}")
        logging.debug(f"DSM found: {self.__dom}")

    def reproject_dom(self, e_lv95, n_lv95, grid_size, grid_step, search_dist):
        """
        Reproject a DSM tile from LV95 to WGS84 with an outer search buffer.

        The inner domain corresponds to the requested tile (e_lv95..e_lv95+grid_size).
        The outer domain adds search_dist metres of buffer in all directions, computed
        with correct ellipsoidal geometry (separate N-S and E-W degree sizes).

        HORAYZON requires the DSM in WGS84 with square pixel sizes in metres.
        The degree resolution is therefore computed separately for lon and lat using
        the cosine of the centre latitude.

        Args:
            e_lv95      : Tile origin easting [m, LV95]
            n_lv95      : Tile origin northing [m, LV95]
            grid_size   : Tile side length [m]
            grid_step   : Target pixel resolution [m]
            search_dist : Shadow search radius [m]

        Returns:
            elevation   : numpy float32 array [ny, nx] with terrain heights [m]
            lon         : 1-D longitude array [degrees WGS84], ascending
            lat         : 1-D latitude array  [degrees WGS84], descending (N->S)
            domain      : dict with inner domain bounds (WGS84 degrees)
            domain_lv95 : dict with inner domain bounds (LV95 metres)
            srs_wgs84   : osr.SpatialReference object for WGS84
        """
        domain_lv95 = {
            "x_min": e_lv95,
            "x_max": e_lv95 + grid_size,
            "y_min": n_lv95,
            "y_max": n_lv95 + grid_size,
        }
        ellps = "WGS84"

        # Build coordinate transformation LV95 -> WGS84
        srs_lv95 = osr.SpatialReference()
        srs_lv95.ImportFromEPSG(2056)
        srs_lv95.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        srs_wgs84 = osr.SpatialReference()
        srs_wgs84.ImportFromEPSG(4326)
        srs_wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ct = osr.CoordinateTransformation(srs_lv95, srs_wgs84)

        # Convert inner domain corners to WGS84
        corners_lv95  = [
            (domain_lv95["x_min"], domain_lv95["y_min"]),
            (domain_lv95["x_max"], domain_lv95["y_min"]),
            (domain_lv95["x_max"], domain_lv95["y_max"]),
            (domain_lv95["x_min"], domain_lv95["y_max"]),
        ]
        corners_wgs84 = [ct.TransformPoint(px, py) for px, py in corners_lv95]
        domain = {
            "lon_min": min(c[0] for c in corners_wgs84),
            "lon_max": max(c[0] for c in corners_wgs84),
            "lat_min": min(c[1] for c in corners_wgs84),
            "lat_max": max(c[1] for c in corners_wgs84),
        }
        # Clamp to valid geographic range
        domain["lat_min"] = max(-89.9, domain["lat_min"])
        domain["lat_max"] = min(89.9,  domain["lat_max"])
        domain["lon_min"] = max(-179.9, domain["lon_min"])
        domain["lon_max"] = min(179.9,  domain["lon_max"])
        logging.info(f"DOMAIN (inner): {domain}")

        # Latitude-corrected degree sizes for N-S and E-W (Bug fix: was identical before)
        lat_center      = (domain["lat_min"] + domain["lat_max"]) / 2.0
        cos_lat         = math.cos(math.radians(lat_center))
        buffer_lat_deg  = search_dist / 111320.0
        buffer_lon_deg  = search_dist / (111320.0 * cos_lat)
        dem_res_lat_deg = grid_step   / 111320.0
        dem_res_lon_deg = grid_step   / (111320.0 * cos_lat)

        logging.info(
            f"lat_center={lat_center:.3f}°, cos_lat={cos_lat:.4f} | "
            f"buffer_ns={buffer_lat_deg*111320:.0f}m, "
            f"buffer_ew={buffer_lon_deg*111320.0*cos_lat:.0f}m | "
            f"pixel_ns={dem_res_lat_deg*111320:.1f}m, "
            f"pixel_ew={dem_res_lon_deg*111320.0*cos_lat:.1f}m"
        )

        # Compute outer domain using curvature-aware replacement for curved_grid
        try:
            domain_outer = manual_curved_grid(domain, search_dist, ellps)
            logging.info(
                f"{os.getpid()} manual_curved_grid OK: "
                f"lon [{domain_outer['lon_min']:.4f}, {domain_outer['lon_max']:.4f}], "
                f"lat [{domain_outer['lat_min']:.4f}, {domain_outer['lat_max']:.4f}]"
            )
        except Exception as ex:
            logging.warning(f"{os.getpid()} manual_curved_grid failed: {ex} -> fallback buffer")
            domain_outer = {
                "lon_min": domain["lon_min"] - buffer_lon_deg,
                "lon_max": domain["lon_max"] + buffer_lon_deg,
                "lat_min": domain["lat_min"] - buffer_lat_deg,
                "lat_max": domain["lat_max"] + buffer_lat_deg,
            }
        logging.info(f"DOMAIN OUTER: {domain_outer}")

        # Reproject DSM from LV95 to WGS84 at outer domain extent (in memory)
        ds_src    = gdal.Open(self.__dom)
        nodata_src = ds_src.GetRasterBand(1).GetNoDataValue()
        ds_src    = None

        ds_warp = gdal.Warp(
            "", self.__dom,
            format="MEM",
            dstSRS="EPSG:4326",
            outputBounds=(
                domain_outer["lon_min"], domain_outer["lat_min"],
                domain_outer["lon_max"], domain_outer["lat_max"],
            ),
            xRes=dem_res_lon_deg,  # latitude-corrected E-W resolution
            yRes=dem_res_lat_deg,  # N-S resolution
            resampleAlg=gdal.GRA_Bilinear,
            srcNodata=nodata_src,
            dstNodata=-9999.0,
        )

        elevation = ds_warp.GetRasterBand(1).ReadAsArray().astype(np.float32)
        gt        = ds_warp.GetGeoTransform()
        nx, ny    = ds_warp.RasterXSize, ds_warp.RasterYSize
        ds_warp   = None

        lon = np.linspace(gt[0] + gt[1] / 2.0, gt[0] + gt[1] * (nx - 0.5), nx)
        lat = np.linspace(gt[3] + gt[5] / 2.0, gt[3] + gt[5] * (ny - 0.5), ny)

        elevation[elevation == -9999.0] = 0.0
        logging.info("DSM reprojected to WGS84, shape: " + str(elevation.shape))
        logging.info(
            "DSM elevation range: %.1f" % elevation.min()
            + " - %.1f" % elevation.max() + " m"
        )

        return elevation, lon, lat, domain, domain_lv95, srs_wgs84


# ---------------------------------------------------------------------------
# SonnenWinkel  –  shadow / illumination mask
# ---------------------------------------------------------------------------

class SonnenWinkel:
    """
    Computes a binary shadow/illumination mask for a DSM tile using HORAYZON.

    For each pixel in the inner domain, the mask is 1 (illuminated) or 0 (shadow).
    NoData pixels are set to 255.

    The shadow computation uses HORAYZON's BVH-based ray casting. Three bugs
    compared to the original code have been fixed here:
      - ENU origin is set to the centre of the inner domain (not the outer domain)
      - Sun position vector is a normalised direction vector (not the astronomical
        distance which causes float32 precision loss of ~18 km)
      - DSM pixel size is latitude-corrected for E-W (via DOM_sw.reproject_dom)
    """

    gdal.UseExceptions()

    def __init__(self, dom, planets, search_dist, output_path):
        """
        Args:
            dom         : Full path to the DSM GeoTIFF (EPSG:2056)
            planets     : dict with keys 'path' (directory) and 'bsp_file' (filename)
            search_dist : Maximum shadow search radius [m]
            output_path : Directory for output files

        """
        self.__dom         = DOM_sw(dom, search_dist, output_path)
        self.__planets     = planets
        self.__search_dist = search_dist
        self.__output_path = output_path
        self.__checkinput_planets()
        self.__checkinput_output()

    def __checkinput_planets(self):
        bsp_path = os.path.join(self.__planets["path"], self.__planets["bsp_file"])
        if not os.path.isfile(bsp_path):
            raise AttributeError(f"Ephemeris file not found: {bsp_path}")
        logging.debug(f"Ephemeris file found: {bsp_path}")

    def __checkinput_output(self):
        if not os.path.isdir(self.__output_path):
            raise AttributeError(f"Output directory not found: {self.__output_path}")
        logging.debug(f"Output directory found: {self.__output_path}")

    def calc_illuminate_grid(self, e_lv95, n_lv95, grid_size, grid_step, timeoi, dateoi):
        """
        Compute the binary illumination mask for one DSM tile.

        Steps:
          1. Reproject DSM to WGS84 with outer buffer (DOM_sw.reproject_dom)
          2. Compute ECEF and ENU coordinates for all DSM pixels
          3. Initialise HORAYZON Terrain object with surface normals and BVH
          4. Compute sun position with Skyfield (normalised direction vector)
          5. Run HORAYZON shadow casting
          6. Reproject result from WGS84 back to LV95 via nearest-neighbour Warp

        Args:
            e_lv95    : Tile origin easting [m, LV95]
            n_lv95    : Tile origin northing [m, LV95]
            grid_size : Tile side length [m]
            grid_step : Pixel resolution [m]
            timeoi    : UTC time string (HH:MM:SS or HH:MM)
            dateoi    : UTC date string (DD.MM.YYYY or YYYY-MM-DD)

        Returns:
            illuminated_lv95 : uint8 numpy array [H, W], 0=shadow, 1=illuminated, 255=nodata
            transform_lv95   : rasterio Affine transform for the output array
            NODATA_ILU       : nodata value (255)
        """
        (
            self.__elevation,
            self.__lon,
            self.__lat,
            self.__domain,
            self.__domain_lv95,
            self.__srs_wgs84,
        ) = self.__dom.reproject_dom(e_lv95, n_lv95, grid_size, grid_step, self.__search_dist)

        ellps = "WGS84"

        # Indices of inner domain within the full outer-domain array
        slice_in = (
            slice(
                np.where(self.__lat >= self.__domain["lat_max"])[0][-1],
                np.where(self.__lat <= self.__domain["lat_min"])[0][0] + 1,
            ),
            slice(
                np.where(self.__lon <= self.__domain["lon_min"])[0][-1],
                np.where(self.__lon >= self.__domain["lon_max"])[0][0] + 1,
            ),
        )
        offset_0 = slice_in[0].start
        offset_1 = slice_in[1].start
        logging.info(f"{os.getpid()} Inner domain size: {self.__elevation[slice_in].shape}")

        # Orthometric heights for inner domain (before geoid addition)
        elevation_ortho_inner = np.ascontiguousarray(self.__elevation[slice_in])

        # Add EGM96 geoid undulation to convert orthometric -> ellipsoidal heights
        # self.__elevation += hray.geoid.undulation(
        #     self.__lon, self.__lat,
        #     geoid="EGM96",
        #     path_to_aux_data=self.__egm_path,
        # )
        self.__elevation += hray.geoid.undulation(
            self.__lon, self.__lat,
            geoid="EGM96",
        )


        # ECEF coordinates for all DSM pixels
        x_ecef, y_ecef, z_ecef = hray.transform.lonlat2ecef(
            *np.meshgrid(self.__lon, self.__lat),
            self.__elevation,
            ellps=ellps,
        )
        dem_dim_0, dem_dim_1 = self.__elevation.shape

        # ENU origin: centre of the INNER domain (not outer domain centre)
        # Bug fix: using outer domain centre caused a lateral ENU offset that
        # shifted shadow edges by ~50-70 m at 5000 m cast-shadow distances.
        lon_inner_center = (self.__domain["lon_min"] + self.__domain["lon_max"]) / 2.0
        lat_inner_center = (self.__domain["lat_min"] + self.__domain["lat_max"]) / 2.0
        logging.info(
            f"{os.getpid()} ENU origin: lon={lon_inner_center:.6f}, lat={lat_inner_center:.6f}"
        )

        trans_ecef2enu = hray.transform.TransformerEcef2enu(
            lon_or=lon_inner_center,
            lat_or=lat_inner_center,
            ellps=ellps,
        )
        x_enu, y_enu, z_enu = hray.transform.ecef2enu(
            x_ecef, y_ecef, z_ecef, trans_ecef2enu
        )

        # Surface normal and north direction vectors for inner domain
        vec_norm_ecef  = hray.direction.surf_norm(
            *np.meshgrid(self.__lon[slice_in[1]], self.__lat[slice_in[0]])
        )
        vec_north_ecef = hray.direction.north_dir(
            x_ecef[slice_in], y_ecef[slice_in], z_ecef[slice_in],
            vec_norm_ecef, ellps=ellps,
        )
        del x_ecef, y_ecef, z_ecef

        vec_norm_enu  = hray.transform.ecef2enu_vector(vec_norm_ecef,  trans_ecef2enu)
        vec_north_enu = hray.transform.ecef2enu_vector(vec_north_ecef, trans_ecef2enu)
        del vec_norm_ecef, vec_north_ecef

        # BVH vertex buffer (includes 1-pixel padding)
        vert_grid = hray.auxiliary.rearrange_pad_buffer(x_enu, y_enu, z_enu)

        # Rotation matrix global ENU -> local ENU
        rot_mat_glob2loc = hray.transform.rotation_matrix_glob2loc(
            vec_north_enu, vec_norm_enu
        )
        del vec_north_enu

        # Slope vectors (1-pixel border required by slope_plane_meth)
        slice_in_a1 = (
            slice(slice_in[0].start - 1, slice_in[0].stop + 1),
            slice(slice_in[1].start - 1, slice_in[1].stop + 1),
        )
        vec_tilt_enu = np.ascontiguousarray(
            hray.topo_param.slope_plane_meth(
                x_enu[slice_in_a1], y_enu[slice_in_a1], z_enu[slice_in_a1],
                rot_mat=rot_mat_glob2loc, output_rot=False,
            )[1:-1, 1:-1]
        )

        # Surface enlargement factor (ratio of actual to projected cell area)
        surf_enl_fac = 1.0 / (vec_norm_enu * vec_tilt_enu).sum(axis=2)
        logging.info(
            f"{os.getpid()} Surface enlargement factor (min/max): "
            f"{surf_enl_fac.min():.3f}, {surf_enl_fac.max():.3f}"
        )

        # Initialise HORAYZON terrain (builds BVH)
        mask    = np.ones(vec_tilt_enu.shape[:2], dtype=np.uint8)
        terrain = hray.shadow.Terrain()
        terrain.initialise(
            vert_grid, dem_dim_0, dem_dim_1,
            offset_0, offset_1,
            vec_tilt_enu, vec_norm_enu,
            surf_enl_fac,
            mask=mask,
            elevation=elevation_ortho_inner,
            refrac_cor=True,
        )

        # Load Skyfield ephemeris
        load.directory = self.__planets["path"]
        planets = load(self.__planets["bsp_file"])
        loc_or  = planets["earth"] + wgs84.latlon(
            trans_ecef2enu.lat_or, trans_ecef2enu.lon_or
        )

        # Compute sun position
        t_beg = time.time()
        ts    = load.timescale()
        dt_utc = HelperFunctions.parse_datetime(dateoi, timeoi)
        t      = ts.from_datetime(dt_utc)
        astrometric    = loc_or.at(t).observe(planets["sun"])
        alt, az, _dist = astrometric.apparent().altaz()

        # Bug fix: original code used d.m (~1.5e11 m) which causes float32
        # rounding errors of ~18 km per component. HORAYZON only needs the
        # direction, so we use a normalised vector scaled to search_dist * 10.
        sun_dir_x = float(np.cos(alt.radians) * np.sin(az.radians))
        sun_dir_y = float(np.cos(alt.radians) * np.cos(az.radians))
        sun_dir_z = float(np.sin(alt.radians))
        scale      = self.__search_dist * 10.0
        sun_position = np.array(
            [sun_dir_x * scale, sun_dir_y * scale, sun_dir_z * scale],
            dtype=np.float32,
        )
        logging.info(
            f"{os.getpid()} Sun altitude={alt.degrees:.2f}°, azimuth={az.degrees:.2f}°, "
            f"scale={scale:.0f} m"
        )

        # Run HORAYZON shadow casting
        shadow_buffer = np.zeros(vec_tilt_enu.shape[:2], dtype=np.uint8)
        terrain.shadow(sun_position, shadow_buffer)
        logging.info(
            f"{os.getpid()} Shadow computation: {time.time() - t_beg:.2f} s"
        )

        # shadow_buffer: 0 = shadow, 1 = illuminated (HORAYZON convention)
        illuminated = (shadow_buffer == 0).astype(np.uint8)

        # Reproject illumination mask from WGS84 back to LV95 via GDAL Warp
        lon_in  = self.__lon[slice_in[1]]
        lat_in  = self.__lat[slice_in[0]]
        lon_res = float(lon_in[1] - lon_in[0])
        lat_res = float(lat_in[1] - lat_in[0])

        gt_inner = (
            float(lon_in[0]) - lon_res / 2.0,  lon_res, 0,
            float(lat_in[0]) - lat_res / 2.0,  0,       lat_res,
        )

        NODATA_ILU  = 255
        driver_mem  = gdal.GetDriverByName("MEM")
        ds_mem      = driver_mem.Create(
            "", illuminated.shape[1], illuminated.shape[0], 1, gdal.GDT_Byte
        )
        ds_mem.SetGeoTransform(gt_inner)
        ds_mem.SetProjection(self.__srs_wgs84.ExportToWkt())
        band_mem = ds_mem.GetRasterBand(1)
        band_mem.SetNoDataValue(NODATA_ILU)
        band_mem.WriteArray(illuminated)

        ds_lv95 = gdal.Warp(
            "", ds_mem,
            format="MEM",
            dstSRS="EPSG:2056",
            outputBounds=(
                self.__domain_lv95["x_min"], self.__domain_lv95["y_min"],
                self.__domain_lv95["x_max"], self.__domain_lv95["y_max"],
            ),
            xRes=grid_step, yRes=grid_step,
            resampleAlg=gdal.GRA_NearestNeighbour,
            srcNodata=NODATA_ILU,
            dstNodata=NODATA_ILU,
        )
        ds_mem = None

        illuminated_lv95 = ds_lv95.GetRasterBand(1).ReadAsArray()
        transform_mem    = ds_lv95.GetGeoTransform()
        ds_lv95          = None

        transform_lv95 = Affine(
            transform_mem[1], transform_mem[2], transform_mem[0],
            transform_mem[4], transform_mem[5], transform_mem[3],
        )

        total_px       = illuminated_lv95.size
        illuminated_px = int(np.sum(illuminated_lv95 == 1))
        logging.info(
            f"{os.getpid()} Illuminated: {illuminated_px} / {total_px} "
            f"({100.0*illuminated_px/total_px:.1f} %)"
        )

        return illuminated_lv95, transform_lv95, NODATA_ILU

    def close(self):
        """Release any resources held by this instance (currently none)."""
        pass


# ---------------------------------------------------------------------------
# DOM_iw  –  DSM handling for incidence angle computation
# ---------------------------------------------------------------------------

class DOM_iw:
    """
    Opens the DSM and exposes metadata required by InzidenWinkel.

    The dataset is kept open between calls to avoid repeated file I/O overhead
    when processing many tiles. Call close() when done.
    """

    def __init__(self, dom):
        """
        Args:
            dom : Full path to the DSM GeoTIFF (EPSG:2056)
        """
        self.__dom = dom
        self.__checkinput()
        self.__load_metadata()

    def __checkinput(self):
        if not os.path.isfile(self.__dom):
            raise AttributeError(f"DSM file not found: {self.__dom}")
        logging.debug(f"DSM found: {self.__dom}")

    def __load_metadata(self):
        """Open DSM and read transform and resolution metadata."""
        logging.info(f"{os.getpid()} Loading DSM metadata...")
        self._src      = rasterio.open(self.__dom)
        self._src_path = self.__dom
        self._transform = self._src.transform
        self._width     = self._src.width
        self._height    = self._src.height
        self._nodata    = self._src.nodata
        self._dx        = self._transform.a   # pixel width  [m], positive
        self._dy        = self._transform.e   # pixel height [m], negative
        self._x0        = self._transform.c + self._dx / 2.0  # first pixel centre x
        self._y0        = self._transform.f + self._dy / 2.0  # first pixel centre y
        logging.info(
            f"{os.getpid()} DSM metadata: {self._width} x {self._height} px, "
            f"dx={self._dx} m, dy={self._dy} m"
        )

    def close(self):
        """Close the rasterio dataset."""
        if self._src:
            self._src.close()
            logging.debug(f"{os.getpid()} DSM dataset closed.")


# ---------------------------------------------------------------------------
# InzidenWinkel  –  solar incidence angle raster
# ---------------------------------------------------------------------------

class InzidenWinkel:
    """
    Computes a per-pixel solar incidence angle raster for a DSM tile.

    The incidence angle (theta) is the angle between the sun ray and the
    terrain surface normal. Values range from 0 to 180 degrees.
    NoData pixels are set to -9999.

    Processing per tile:
      1. Read DSM window for the tile extent
      2. Compute slope and aspect vectors using HORAYZON slope_plane_meth
      3. Compute sun position once for the tile centre
      4. Apply incidence angle formula for all pixels
    """

    def __init__(self, dom, planets, output_path):
        """
        Args:
            dom         : Full path to the DSM GeoTIFF (EPSG:2056)
            planets     : dict with keys 'path' (directory) and 'bsp_file' (filename)
            output_path : Directory for output files (existence is checked)
        """
        self.__dom         = DOM_iw(dom)
        self.__planets     = planets
        self.__output_path = output_path
        self.__checkinput()

    def __checkinput(self):
        if not os.path.isdir(self.__output_path):
            raise AttributeError(
                f"Output directory not found: {self.__output_path}"
            )
        logging.debug(f"Output directory found: {self.__output_path}")

    def calc_incidence_grid(self, e_lv95, dateoi, timeoi, n_lv95, grid_size, grid_step):
        """
        Compute the incidence angle raster for one DSM tile.

        The DSM window is read with 1-pixel border on all sides so that
        slope_plane_meth (which loses 1 pixel per edge via [1:-1, 1:-1])
        produces valid values at tile boundaries. Without this border,
        nodata strips 1-2 pixels wide appear at every tile edge (every 20 km).

        Args:
            e_lv95    : Tile origin easting  [m, LV95]
            dateoi    : UTC date string (DD.MM.YYYY or YYYY-MM-DD)
            timeoi    : UTC time string (HH:MM:SS or HH:MM)
            n_lv95    : Tile origin northing [m, LV95]
            grid_size : Tile side length [m]
            grid_step : Pixel resolution [m]

        Returns:
            grid_out      : float32 numpy array [H, W], incidence angles [degrees],
                            nodata pixels set to -9999
            transform_out : rasterio Affine transform for the output array
            NODATA_INC    : nodata value (-9999)
        """
        dt_utc     = HelperFunctions.parse_datetime(dateoi, timeoi)
        num_points = grid_size // grid_step
        xmin, xmax = e_lv95, e_lv95 + grid_size
        ymin, ymax = n_lv95, n_lv95 + grid_size

        # Read DSM window with 1-pixel border on all sides.
        # slope_plane_meth loses 1 pixel per edge ([1:-1, 1:-1]).
        # Without the border, nodata strips appear at every tile boundary.
        border = abs(self.__dom._dx)   # = grid_step [m], e.g. 10 m = 1 pixel

        src    = self.__dom._src
        window = window_from_bounds(
            xmin - border, ymin - border,
            xmax + border, ymax + border,
            src.transform,
        )
        window = window.round_offsets().round_lengths()

        elev_tile = src.read(1, window=window).astype(np.float32)
        nodata    = self.__dom._nodata
        if nodata is not None:
            elev_tile = np.where(elev_tile == nodata, np.nan, elev_tile)

        height, width = elev_tile.shape

        # Pixel-centre coordinates in LV95 for the extended window
        win_transform = rasterio.windows.transform(window, src.transform)
        x_window  = win_transform.c + (np.arange(width)  + 0.5) * win_transform.a
        y_window  = win_transform.f + (np.arange(height) + 0.5) * win_transform.e
        x_full_2d, y_full_2d = np.meshgrid(x_window, y_window)

        # Compute surface tilt vectors with HORAYZON
        logging.info(f"{os.getpid()} Computing slope/aspect grid...")
        vec_tilt = hray.topo_param.slope_plane_meth(
            x_full_2d.astype(np.float32),
            y_full_2d.astype(np.float32),
            np.nan_to_num(elev_tile).astype(np.float32),
        )

        # [1:-1, 1:-1] removes the border pixels added above.
        # Result now covers exactly the tile extent (num_points x num_points).
        slope_tile  = np.rad2deg(np.arccos(vec_tilt[1:-1, 1:-1, 2]))
        aspect_vec  = vec_tilt[1:-1, 1:-1, :2]
        aspect_tile = np.rad2deg(np.arctan2(aspect_vec[..., 0], aspect_vec[..., 1]))
        aspect_tile[aspect_tile < 0] += 360.0

        # Crop to num_points x num_points and store in output arrays
        slope  = np.full((num_points, num_points), np.nan, dtype=np.float32)
        aspect = np.full((num_points, num_points), np.nan, dtype=np.float32)
        rows = min(num_points, slope_tile.shape[0])
        cols = min(num_points, slope_tile.shape[1])
        slope[:rows, :cols]  = slope_tile[:rows, :cols]
        aspect[:rows, :cols] = aspect_tile[:rows, :cols]

        # Sun position at tile centre (one value per tile, sufficient at 20 km)
        center_x = e_lv95 + grid_size / 2.0
        center_y = n_lv95 + grid_size / 2.0
        lon_c, lat_c = HelperFunctions.lv95_to_wgs84(center_x, center_y)
        sun_elev, sun_az = HelperFunctions.calc_sunpos(
            lat_c, lon_c, dt_utc,
            self.__planets["path"],
            self.__planets["bsp_file"],
        )
        logging.info(
            f"{os.getpid()} Sun alt={sun_elev:.2f}°, az={sun_az:.2f}° "
            f"at tile centre ({lon_c:.5f}, {lat_c:.5f})"
        )

        # Incidence angle computation
        NODATA_INC = np.float32(-9999.0)
        logging.info(f"{os.getpid()} Computing incidence angle...")
        theta = HelperFunctions.calculate_incidence_angle(slope, aspect, sun_elev, sun_az)
        grid  = theta.astype(np.float32)
        grid[np.isnan(grid)] = NODATA_INC

        # Output array and transform (always num_points x num_points)
        grid_out = np.full((num_points, num_points), NODATA_INC, dtype=np.float32)
        grid_out[:rows, :cols] = grid[:rows, :cols]

        transform_out = Affine(self.__dom._dx, 0, xmin, 0, self.__dom._dy, ymax)
        logging.info(f"{os.getpid()} Incidence tile done, transform: {transform_out}")

        return grid_out, transform_out, NODATA_INC

    def close(self):
        """Close the DSM dataset held by DOM_iw."""
        self.__dom._src.close()