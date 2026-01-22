from pathlib import Path
import numpy as np
import rasterio
from rasterio.mask import mask
import geopandas as gpd


def cloudpercentage(mask_file, clip_orbit):
    """
    Calculate the percentage of cloud pixels within a polygon.

    Parameters
    ----------
    mask_file : str or Path
        Path to cloud mask GeoTIFF file. Pixels with values 1 or 2 are
        considered cloud pixels.
    clip_orbit : str or Path
        Path to orbit clipping GPKG/vector file with polygon defining
        valid pixels (like dataMask in JS).

    Returns
    -------
    float
        Percentage of cloud pixels within the polygon (0-100).
        Returns None if no valid data is found.

    Raises
    ------
    FileNotFoundError
        If either input file does not exist.
    ValueError
        If the vector file contains no valid geometries.

    Examples
    --------
    >>> cloud_pct = cloudpercentage(
    ...     'swisseo_s2-sr_v200_mosaic_2025-06-01t101041_cloudmask_10m.tif',
    ...     'orbit_clip.gpkg'
    ... )
    >>> print(f"Cloud coverage: {cloud_pct:.2f}%")
    """
    # Convert to Path objects
    mask_file = Path(mask_file)
    clip_orbit = Path(clip_orbit)

    # Validate input files exist
    if not mask_file.exists():
        raise FileNotFoundError(f"Mask file not found: {mask_file}")
    if not clip_orbit.exists():
        raise FileNotFoundError(f"Clip orbit file not found: {clip_orbit}")

    # Read the vector file (polygon)
    gdf = gpd.read_file(clip_orbit)

    if gdf.empty:
        raise ValueError(f"No geometries found in {clip_orbit}")

    # Open the raster file
    with rasterio.open(mask_file) as src:
        # Reproject the polygon to match the raster CRS if needed
        if gdf.crs != src.crs:
            gdf = gdf.to_crs(src.crs)

        # Extract geometries
        geometries = gdf.geometry.values

        # Mask the raster with the polygon
        # out_image: masked array
        # out_transform: affine transform of the masked area
        out_image, out_transform = mask(
            src,
            geometries,
            crop=True,
            nodata=src.nodata,
            filled=False  # Return a masked array
        )

        # out_image shape: (bands, height, width)
        # Get the first (and only) band
        masked_data = out_image[0]

        # Count valid (non-masked) pixels
        valid_pixels = ~masked_data.mask
        total_valid = np.sum(valid_pixels)

        if total_valid == 0:
            return None

        # Get the actual pixel values (only valid pixels)
        valid_values = masked_data.data[valid_pixels]

        # Count cloud pixels (values 1 or 2)
        cloud_pixels = np.sum((valid_values == 1) | (valid_values == 2))

        # Calculate percentage
        cloud_percentage = (cloud_pixels / total_valid) * 100

        return cloud_percentage


if __name__ == "__main__":
    # Example usage




    base_path = r"D:\temp\github\topo-satromo-v2"
    base_name = "swisseo_s2-sr_v200_mosaic_2025-06-01t101041"

    mask_file = f"{base_path}\\{base_name}_cloudmask_10m.tif"
    clip_orbit = f"{base_path}\\assets\\swissboundary_buffer_5000m_22.gpkg"
    try:
        result = cloudpercentage(mask_file, clip_orbit)

        if result is None:
            print("No valid data found within the polygon.")
        else:
            print(f"Cloud percentage: {result:.2f}%")
            print(f"Clear sky: {100 - result:.2f}%")

    except Exception as e:
        print(f"Error: {e}")
