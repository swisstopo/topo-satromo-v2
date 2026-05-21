import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple, Dict, Union
import fiona
import numpy as np
import rasterio
from shapely.geometry import shape
import configuration as config

THUMBNAIL_SIZE = 256
BUFFER_SIZE = 1024
OUTPUT_FORMAT = "PNG"
THUMBNAIL_FILENAME = "thumbnail.png"
TEMP_PREFIX = "output_thumbnail"
gpkg_path= config.BUFFER
layer_name="swissCauliflower3DRegio"

# Color maps for different products
COLOR_MAPS = {
    "vhi": {
        (0, 10): (181, 106, 41),      # extremely dry - dark brown
        (11, 20): (206, 133, 64),     # severely dry - brown
        (21, 30): (245, 205, 133),    # moderately dry - beige
        (31, 40): (255, 245, 186),    # mild dry - yellow
        (41, 50): (203, 255, 202),    # normal - light green
        (51, 60): (82, 189, 159),     # good - green
        (61, 100): (4, 112, 176),     # excellent - blue
        (110, 110): (255, 255, 255),  # missing data - gray (128, 128, 128)
        (255, 255): (255, 255, 255),  # no data - white
    },
    "ndvi_z": {  # scaling factor 100
        (-550, -450): (125, 102, 8),   # dark brown
        (-449, -350): (169, 137, 11),
        (-349, -250): (212, 172, 13),
        (-249, -150): (230, 196, 62),
        (-149, -50): (247, 220, 111),  # light yellow
        (-49, 0.5): (245, 245, 245),   # light grey
        (49, 150): (125, 206, 160),    # light green
        (149, 250): (80, 180, 122),
        (249, 350): (34, 153, 84),
        (349, 450): (27, 122, 67),
        (449, 550): (20, 90, 50),      # dark green
    },
    "ndvi_diff": {  # scaling factor 1000
        (-300, -1000): (183, 28, 28),   # dark red
        (-225, -299): (205, 90, 72),
        (-150, -224): (228, 153, 116),
        (-75, -149): (250, 215, 160),   # light orange
        (-74, 74): (245, 245, 245),     # light grey
        (75, 149): (128, 203, 196),     # light turquoise
        (150, 224): (85, 161, 152),
        (225, 299): (43, 119, 108),
        (300, 1000): (0, 77, 64),       # dark turquoise
    },
    "ndmi_z": {  # scaling factor 100
        (-550, -450): (120, 66, 18),    # dark brown
        (-449, -350): (161, 88, 24),
        (-349, -250): (202, 111, 30),
        (-249, -150): (221, 144, 76),
        (-149, -50): (240, 178, 122),   # light brown
        (-49, 0.5): (245, 245, 245),    # light grey
        (49, 150): (133, 193, 233),     # light blue
        (149, 250): (89, 163, 213),
        (249, 350): (46, 134, 193),
        (349, 450): (36, 106, 153),
        (449, 550): (27, 79, 114),      # dark blue
    },
}

def run_gdal_command(command: list) -> bool:
    """
    Execute a GDAL command in a subprocess with error handling.

    Args:
        command: List of command arguments to pass to subprocess.

    Returns:
        True if the command returns a zero exit code; False otherwise.

    Raises:
        None. Function prints errors instead of raising.
    """
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"GDAL Error: {e}")
        print(f"Command: {' '.join(command)}")
        return False

def cleanup_temp_files(prefix: str = TEMP_PREFIX) -> None:
    """
    Remove all temporary files in the working directory with a given prefix.

    Args:
        prefix: Prefix identifying temporary files to remove.

    Returns:
        None.
    """
    for file in Path.cwd().glob(f"{prefix}*"):
        file.unlink()

def get_geometry_from_gpkg(gpkg_path: Union[str, Path], layer_name: Optional[str] = None) -> shape:
    """
    Read the first geometry from a GeoPackage vector layer.

    Args:
        gpkg_path: Path to the GeoPackage file.
        layer_name: Name of the layer within the GeoPackage.

    Returns:
        Shapely geometry of the first feature in the layer.

    Raises:
        StopIteration: If the file is empty.
    """
    with fiona.open(gpkg_path, layer=layer_name) as src:
        first_feature = next(iter(src))
        return shape(first_feature["geometry"])

def create_swiss_buffer_raster(
    gpkg_path: Union[str, Path],
    layer_name: str,
    target_width: int,
    output_file: Union[str, Path]
) -> bool:
    """
    Create a buffer raster (Switzerland extent) from a GeoPackage.

    Args:
        gpkg_path: Path to the GeoPackage file.
        layer_name: Layer name in the GeoPackage.
        target_width: Desired output raster width in pixels.
        output_file: Path for the output raster file.

    Returns:
        True if successful; False if GDAL fails.

    Raises:
        None.
    """
    geom = get_geometry_from_gpkg(gpkg_path, layer_name)
    minx, miny, maxx, maxy = geom.bounds
    orig_width = maxx - minx
    orig_height = maxy - miny
    target_height = int(orig_height / orig_width * target_width)

    command = [
        "gdal_rasterize",
        "-burn", "220", "-burn", "220", "-burn", "220",
        "-init", "255",
        "-l", layer_name,
        str(gpkg_path),
        "-ot", "Byte",
        "-of", "GTiff",
        "-ts", str(target_width), str(target_height),
        str(output_file),
    ]
    return run_gdal_command(command)

def burn_vector_overlay(
    input_raster: Union[str, Path], vector_path: Union[str, Path], layer_name: str
) -> bool:
    """
    Burn a vector overlay (e.g., rivers or lakes) onto each band of a raster.

    Args:
        input_raster: Path to the input raster.
        vector_path: Path to the vector data.
        layer_name: Name of the vector layer.

    Returns:
        True if successful; False otherwise.

    Raises:
        None.
    """
    command = [
        "gdal_rasterize",
        "-b", "1", "-b", "2", "-b", "3",
        "-burn", "255", "-burn", "255", "-burn", "255",
        "-l", layer_name,
        str(vector_path),
        str(input_raster)
    ]
    return run_gdal_command(command)

def apply_overlays_and_export(
    input_file: Union[str, Path],
    output_file: Union[str, Path]
) -> Optional[str]:
    """
    Overlay rivers and lakes, then export the raster to PNG.

    Args:
        input_file: Path to an intermediate raster for overlay.
        output_file: Path for the final PNG thumbnail.
    Returns:
        Output file path as string on success, or None on failure.

    Raises:
        None.
    """
    input_stem = Path(input_file).stem
    tif_file = f"{input_stem}.tif"

    if not burn_vector_overlay(tif_file, config.OVERVIEW_RIVERS, "overview_rivers_2056"):
        return None
    if not burn_vector_overlay(tif_file, config.OVERVIEW_LAKES, "overview_lakes_2056"):
        return None

    command = [
        "gdal_translate",
        "-of", OUTPUT_FORMAT,
        "--config", "GDAL_PAM_ENABLED", "NO",
        tif_file,
        str(output_file),
    ]

    if run_gdal_command(command):
        cleanup_temp_files()
        return str(output_file)
    return None

def apply_color_map(
    data: np.ndarray,
    color_map: Dict,
    special_values: Dict = None
) -> np.ndarray:
    """
    Map a single-band integer array to RGB using a custom color map.

    Args:
        data: 2D raster data array.
        color_map: Mapping of value ranges (tuple) to (R,G,B) tuples.
        special_values: Mapping of exact nodata/missing values to RGB.

    Returns:
        3D uint8 numpy array (shape: 3 x height x width) ready for writing as RGB.

    Raises:
        None.
    """
    rgb = np.full((3, data.shape[0], data.shape[1]), 255, dtype=np.uint8)
    if special_values:
        for value, color in special_values.items():
            mask = data == value
            if np.any(mask):
                rgb[:, mask] = np.array(color)[:, np.newaxis]
    valid_mask = ~np.isin(data, list(special_values.keys()) if special_values else [])
    for value_range, color in color_map.items():
        range_mask = np.logical_and(
            np.logical_and(data >= value_range[0], data <= value_range[1]),
            valid_mask,
        )
        if np.any(range_mask):
            rgb[:, range_mask] = np.array(color)[:, np.newaxis]
    return rgb

def get_raster_stats(raster_path: str) -> Tuple[int, int]:
    """Get min/max values from raster."""
    with rasterio.open(raster_path) as src:
        data = src.read()
        return int(round(data.min())), int(round(data.max()))


def get_raster_bounds(raster_path: str) -> Tuple[str, str, str, str]:
    """Get bounding box as strings for GDAL."""
    with rasterio.open(raster_path) as src:
        bounds = src.bounds
        return (
            str(int(bounds.left)),
            str(int(bounds.bottom)),
            str(int(bounds.right)),
            str(int(bounds.top)),
        )

def calculate_thumbnail_size(input_file: str, max_size: int) -> Tuple[int, int]:
    """
    Calculate thumbnail dimensions while preserving aspect ratio.

    Args:
        input_file: Path to input raster
        max_size: Maximum dimension (width or height)

    Returns:
        Tuple of (width, height) for thumbnail
    """
    with rasterio.open(input_file) as src:
        width = src.width
        height = src.height

        # Calculate aspect ratio
        aspect_ratio = width / height

        if width > height:
            # Landscape orientation
            thumb_width = max_size
            thumb_height = int(max_size / aspect_ratio)
        else:
            # Portrait orientation
            thumb_height = max_size
            thumb_width = int(max_size * aspect_ratio)

    return thumb_width, thumb_height

def create_thumbnail_s2_sr(input_file: str, gpkg_path: str, layer_name: str) -> Optional[str]:
    """Create thumbnail for Sentinel-2 Surface Reflectance 10m bands."""
    try:
        # Calculate size preserving aspect ratio
        thumb_width, thumb_height = calculate_thumbnail_size(input_file, THUMBNAIL_SIZE)

        # Resize to thumbnail size (input is already RGB 8-bit)
        run_gdal_command([
            "gdal_translate",
            "-of", "GTiff",
            "-outsize", str(thumb_width), str(thumb_height),
            "-co", "COMPRESS=DEFLATE",
            input_file,
            f"{TEMP_PREFIX}RGB_resized.tif",
        ])

        # Create Swiss buffer
        create_swiss_buffer_raster(gpkg_path, layer_name, BUFFER_SIZE, f"{TEMP_PREFIX}swissfill.tif")

        # Overlay on Switzerland buffer
        run_gdal_command([
            "gdalwarp",
            "-overwrite",
            "-dstnodata", "255",
            f"{TEMP_PREFIX}swissfill.tif",
            f"{TEMP_PREFIX}RGB_resized.tif",
            f"{TEMP_PREFIX}RGB_merged.tif",
        ])

        # Clip to original extent
        bbox = get_raster_bounds(input_file)
        run_gdal_command([
            "gdalwarp",
            "-overwrite",
            "-te", *bbox,
            f"{TEMP_PREFIX}RGB_merged.tif",
            f"{TEMP_PREFIX}RGB_clipped.tif",
        ])

        # Apply overlays and export
        return apply_overlays_and_export(
            f"{TEMP_PREFIX}RGB_clipped.tif",
            THUMBNAIL_FILENAME
        )
    except Exception as e:
        print(f"Error creating S2-SR thumbnail: {e}")
        return None


def create_thumbnail_indexed(
    input_file: str,
    gpkg_path: str,
    layer_name: str,
    color_map: Dict,
    special_values: Dict = None,
    nodata_value: Optional[int] = None,
) -> Optional[str]:
    """
    Create thumbnail for indexed products (VHI, NDVI-Z, NDVI-diff, NDMI-Z).

    Args:
        input_file: Input raster file
        gpkg_path: Path to GeoPackage with Swiss buffer
        layer_name: Layer name in GeoPackage
        color_map: Color mapping dictionary
        special_values: Special value to color mapping for nodata/missing
        nodata_value: NoData value to set in preprocessing
    """
    try:
        # Preprocess if nodata value specified
        if nodata_value:
            with rasterio.open(input_file) as src:
                data = src.read(1)
                profile = src.profile.copy()
                profile.update(nodata=None)

                with rasterio.open(f"{TEMP_PREFIX}_preprocessed.tif", "w", **profile) as dst:
                    dst.write(data)

            input_for_thumbnail = f"{TEMP_PREFIX}_preprocessed.tif"
            nodata_arg = ["-a_nodata", str(nodata_value)]
        else:
            input_for_thumbnail = input_file
            nodata_arg = []

        # Calculate size preserving aspect ratio
        thumb_width, thumb_height = calculate_thumbnail_size(input_file, THUMBNAIL_SIZE)

        # Create thumbnail
        run_gdal_command([
            "gdal_translate",
            "-b", "1",
            "-of", "GTiff",
            "-outsize", str(thumb_width), str(thumb_height),
            "-r", "near",
            *nodata_arg,
            input_for_thumbnail,
            f"{TEMP_PREFIX}.tif",
        ])

        # Apply color map
        with rasterio.open(f"{TEMP_PREFIX}.tif") as src:
            data = src.read(1)
            profile = src.profile.copy()
            profile.update(count=3, dtype=rasterio.uint8)
            if "nodata" in profile:
                del profile["nodata"]

        rgb_data = apply_color_map(data, color_map, special_values)

        # Write RGB
        profile.update(nodata=255)
        with rasterio.open(f"{TEMP_PREFIX}RGB.tif", "w", **profile) as dst:
            dst.write(rgb_data)

        # Create Swiss buffer
        create_swiss_buffer_raster(gpkg_path, layer_name, BUFFER_SIZE, f"{TEMP_PREFIX}swissfill.tif")

        # Overlay on Switzerland
        run_gdal_command([
            "gdalwarp",
            # "-s_srs", "EPSG:2056",
            "-overwrite",
            "-srcnodata", "255 255 255",
            f"{TEMP_PREFIX}swissfill.tif",
            f"{TEMP_PREFIX}RGB.tif",
            f"{TEMP_PREFIX}RGB_merged.tif",
        ])

        # Clip back to the original ROI extent
        bbox = get_raster_bounds(input_file)
        run_gdal_command([
            "gdalwarp",
            "-overwrite",
            "-te", *bbox,
            f"{TEMP_PREFIX}RGB_merged.tif",
            f"{TEMP_PREFIX}RGB_clipped.tif",
        ])

        # Apply overlays and export
        return apply_overlays_and_export(f"{TEMP_PREFIX}RGB_clipped.tif", THUMBNAIL_FILENAME)

    except Exception as e:
        print(f"Error creating indexed thumbnail: {e}")
        return None


def create_thumbnail(
    input_file: str,
    product: str
) -> Optional[str]:
    """
    Create thumbnail for Swiss geospatial product.

    Args:
        input_file: Input raster filename
        product: Product identifier (e.g., 'ch.swisstopo.swisseo_s2-sr')
        gpkg_path: Path to GeoPackage with Swiss buffer
        layer_name: Layer name in GeoPackage

    Returns:
        Thumbnail filename if successful, None otherwise
    """

    # S2 Surface Reflectance 10m bands
    if product.startswith("ch.swisstopo.swisseo_s2-sr") and input_file.endswith("tci_10m.tif"):
        return create_thumbnail_s2_sr(input_file, gpkg_path, layer_name)

    # VHI products
    elif product.startswith("ch.swisstopo.swisseo_vhi") and (
        input_file.endswith("forest-10m.tif") or input_file.endswith("forest-30m.tif") or input_file.endswith("vegetation-10m.tif") or input_file.endswith("vegetation-30m.tif")
    ):
        return create_thumbnail_indexed(
            input_file,
            gpkg_path,
            layer_name,
            COLOR_MAPS["vhi"],
            special_values={110: (255, 255, 255), 255: (255, 255, 255)}
        )

    # NDVI-Z products
    elif product.startswith("swisseo_ndvi_z") and input_file.endswith("forest-10m.tif"):
        return create_thumbnail_indexed(
            input_file,
            gpkg_path,
            layer_name,
            COLOR_MAPS["ndvi_z"],
            special_values={32700: (220, 220, 220)},
            nodata_value=32701,
        )

    # NDVI-diff products
    elif product.startswith("swisseo_ndvi_diff") and input_file.endswith("forest-10m.tif"):
        return create_thumbnail_indexed(
            input_file,
            gpkg_path,
            layer_name,
            COLOR_MAPS["ndvi_diff"],
            special_values={32700: (220, 220, 220)},
            nodata_value=32701,
        )

    # NDMI-Z products
    elif product.startswith("swisseo_ndmi_z") and input_file.endswith("forest-10m.tif"):
        return create_thumbnail_indexed(
            input_file,
            gpkg_path,
            layer_name,
            COLOR_MAPS["ndmi_z"],
            special_values={32700: (220, 220, 220)},
            nodata_value=32701,
        )

    else:
        print(f"Unsupported product or input file: {product}, {input_file}")
        return None
