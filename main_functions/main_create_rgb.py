import rasterio
import rasterio.mask
import rasterio.features
import numpy as np
from pathlib import Path
import subprocess
import geopandas as gpd
from rasterio.windows import from_bounds

def create_enhanced_rgb(b04_path, b03_path, b02_path, clip_orbit, output_path,
                        scale=0.0001,
                        offset=-0.1,
                        max_r=3.0,
                        mid_r=0.13,
                        sat=1.2,
                        gamma=1.8,
                        create_cog=True):
    """
    Create an enhanced RGB composite from Sentinel-2 L2A bands.
    Exact Python implementation of the Sentinel Hub JavaScript enhancement algorithm based on https://custom-scripts.sentinel-hub.com/custom-scripts/sentinel-2/l2a_optimized/ .

    Parameters:
    -----------
    b04_path : str or Path
        Path to Band 4 (Red) GeoTIFF file
    b03_path : str or Path
        Path to Band 3 (Green) GeoTIFF file
    b02_path : str or Path
        Path to Band 2 (Blue) GeoTIFF file
    clip_orbit : str or Path
        Path to orbit clipping GPKG/vector file with polygon defining valid pixels (like dataMask in JS)
    output_path : str or Path
        Path for output RGB GeoTIFF file
    scale : float, optional
        Scale factor for reflectance calculation (default: 0.0001 for SwissEO)
    offset : float, optional
        Offset for reflectance calculation (default: -0.1 for SwissEO)
    max_r : float, optional
        Maximum reflectance value for contrast enhancement (default: 3.0)
    mid_r : float, optional
        Mid-point reflectance for contrast adjustment (default: 0.13)
    sat : float, optional
        Saturation enhancement factor (default: 1.2)
    gamma : float, optional
        Gamma correction value (default: 1.8)
    create_cog : bool, optional
        If True, convert output to Cloud-Optimized GeoTIFF using gdal_translate (default: True)

    Returns:
    --------
    str
        Path to the created output file (COG if create_cog=True)

    Notes:
    ------
    Reflectance is calculated as: reflectance = (raw_value * scale) + offset
    For SwissEO: reflectance = (raw_value * 0.0001) - 0.1

    The clip_orbit polygon defines the data mask (valid pixels), similar to dataMask in the original JS code.
    All pixels outside this polygon are treated as NoData. Pixels with value 0 INSIDE the polygon remain valid.
    """

    # Constants from JS code
    g_off = 0.01
    g_off_pow = g_off ** gamma
    g_off_range = (1 + g_off) ** gamma - g_off_pow

    def clip_func(s):
        """Clip values to 0-1 range"""
        return np.clip(s, 0, 1)

    def adj(a, tx, ty, max_c):
        """Contrast enhancement with highlight compression"""
        ar = clip_func(a / max_c)
        denominator = ar * (2 * tx / max_c - 1) - tx / max_c
        # Avoid division by zero
        denominator = np.where(np.abs(denominator) < 1e-10, 1e-10, denominator)
        return ar * (ar * (tx / max_c + ty - 1) - ty) / denominator

    def adj_gamma(b):
        """Apply gamma correction"""
        return ((b + g_off) ** gamma - g_off_pow) / g_off_range

    def s_adj(a):
        """Combined adjustment and gamma correction"""
        return adj_gamma(adj(a, mid_r, 1, max_r))

    def sat_enh(r, g, b):
        """Saturation enhancement"""
        avg_s = (r + g + b) / 3.0 * (1 - sat)
        return (
            clip_func(avg_s + r * sat),
            clip_func(avg_s + g * sat),
            clip_func(avg_s + b * sat)
        )

    def srgb(c):
        """Convert linear RGB to sRGB"""
        return np.where(c <= 0.0031308,
                       12.92 * c,
                       1.055 * np.power(np.clip(c, 0, 1), 0.41666666666) - 0.055)

    # Read the clip orbit polygon (dataMask equivalent)
    print(f"Reading clip orbit polygon from: {clip_orbit}")
    gdf = gpd.read_file(clip_orbit)
    geometries = gdf.geometry.values
    print(f"Loaded {len(geometries)} polygon(s) for masking")

    # Read bands WITHOUT masking (read all data first)
    print(f"Reading input bands with scale={scale}, offset={offset}...")

    with rasterio.open(b04_path) as src:
        # Get bounds of polygon to crop
        bounds = gdf.total_bounds  # minx, miny, maxx, maxy
        window = from_bounds(*bounds, src.transform)

        # Read the cropped area
        b04_raw = src.read(1, window=window).astype(np.float32)

        # Get the transform for the cropped window
        crop_transform = src.window_transform(window)
        profile = src.profile.copy()

        # Create polygon mask for the cropped area
        polygon_mask = rasterio.features.geometry_mask(
            geometries,
            out_shape=b04_raw.shape,
            transform=crop_transform,
            invert=True,  # True = inside polygon (valid)
            all_touched=False
        )

    with rasterio.open(b03_path) as src:
        b03_raw = src.read(1, window=window).astype(np.float32)

    with rasterio.open(b02_path) as src:
        b02_raw = src.read(1, window=window).astype(np.float32)

    # Create nodata mask: True = outside polygon (NoData), False = inside polygon (valid)
    nodata_mask = ~polygon_mask
    valid_pixels = polygon_mask

    print(f"Image dimensions: {b04_raw.shape}")
    print(f"Valid pixels (inside polygon): {100*valid_pixels.sum()/nodata_mask.size:.1f}%")
    print(f"NoData pixels (outside polygon): {100*nodata_mask.sum()/nodata_mask.size:.1f}%")

    # Debug: Check raw values
    if valid_pixels.sum() > 0:
        print(f"\nRaw values (valid pixels only):")
        print(f"B04: min={b04_raw[valid_pixels].min():.1f}, max={b04_raw[valid_pixels].max():.1f}, mean={b04_raw[valid_pixels].mean():.1f}")
        print(f"B03: min={b03_raw[valid_pixels].min():.1f}, max={b03_raw[valid_pixels].max():.1f}, mean={b03_raw[valid_pixels].mean():.1f}")
        print(f"B02: min={b02_raw[valid_pixels].min():.1f}, max={b02_raw[valid_pixels].max():.1f}, mean={b02_raw[valid_pixels].mean():.1f}")

    # Convert to reflectance: reflectance = (raw * scale) + offset
    b04 = (b04_raw * scale) + offset
    b03 = (b03_raw * scale) + offset
    b02 = (b02_raw * scale) + offset

    if valid_pixels.sum() > 0:
        print(f"\nReflectance (valid pixels only):")
        print(f"B04: {b04[valid_pixels].min():.4f} - {b04[valid_pixels].max():.4f}")
        print(f"B03: {b03[valid_pixels].min():.4f} - {b03[valid_pixels].max():.4f}")
        print(f"B02: {b02[valid_pixels].min():.4f} - {b02[valid_pixels].max():.4f}")

    # Clip negative values to 0
    b04 = np.clip(b04, 0, None)
    b03 = np.clip(b03, 0, None)
    b02 = np.clip(b02, 0, None)

    # Apply enhancement pipeline
    print(f"\nApplying enhancement: max_r={max_r}, mid_r={mid_r}, sat={sat}, gamma={gamma}")

    r_adj = s_adj(b04)
    g_adj = s_adj(b03)
    b_adj = s_adj(b02)

    r_enh, g_enh, b_enh = sat_enh(r_adj, g_adj, b_adj)

    r_final = srgb(r_enh)
    g_final = srgb(g_enh)
    b_final = srgb(b_enh)

    # Scale to 0-255
    r_byte = (np.clip(r_final, 0, 1) * 255).astype(np.uint8)
    g_byte = (np.clip(g_final, 0, 1) * 255).astype(np.uint8)
    b_byte = (np.clip(b_final, 0, 1) * 255).astype(np.uint8)

    # WICHTIG: Setze sehr dunkle gültige Pixel auf mindestens 1,
    # damit sie nicht durch -dstalpha transparent werden
    min_value = 1
    if valid_pixels.sum() > 0:
        # Für gültige Pixel, die genau 0 sind, setze auf 1
        very_dark = valid_pixels & (r_byte == 0) & (g_byte == 0) & (b_byte == 0)
        if very_dark.sum() > 0:
            print(f"Adjusting {very_dark.sum()} very dark pixels from (0,0,0) to ({min_value},{min_value},{min_value}) to prevent transparency")
            r_byte[very_dark] = min_value
            g_byte[very_dark] = min_value
            b_byte[very_dark] = min_value

        print(f"Final byte values (valid pixels): R={r_byte[valid_pixels].mean():.1f}, G={g_byte[valid_pixels].mean():.1f}, B={b_byte[valid_pixels].mean():.1f}")

    # Apply nodata mask ONLY to pixels outside polygon - set to 0
    r_byte[nodata_mask] = 0
    g_byte[nodata_mask] = 0
    b_byte[nodata_mask] = 0
    # Determine temporary or final output path
    if create_cog:
        # Write to temporary file first
        temp_output = str(Path(output_path).with_suffix('.temp.tif'))
        final_output = str(output_path)
    else:
        temp_output = str(output_path)
        final_output = str(output_path)

    # Update profile for RGB output
    profile.update(
        dtype=rasterio.uint8,
        count=3,
        height=b04_raw.shape[0],
        width=b04_raw.shape[1],
        transform=crop_transform,
        compress='lzw',
        nodata=0,
        photometric='rgb'
    )

    # Write initial output
    print(f"\nWriting to: {temp_output}")
    with rasterio.open(temp_output, 'w', **profile) as dst:
        dst.write(r_byte, 1)
        dst.write(g_byte, 2)
        dst.write(b_byte, 3)
        dst.colorinterp = [rasterio.enums.ColorInterp.red,
                           rasterio.enums.ColorInterp.green,
                           rasterio.enums.ColorInterp.blue]

    # Convert to COG if requested
    if create_cog:
        print(f"\nConverting to Cloud-Optimized GeoTIFF...")

        gdal_cmd = [
            "gdalwarp",
            "-of", "COG",
            "-co", "BIGTIFF=YES",
            "-co", "NUM_THREADS=ALL_CPUS",
            "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
            "-co", "COMPRESS=JPEG",
            "-co", "QUALITY=85",
            "-cutline", str(clip_orbit),
            "-crop_to_cutline",
            "-dstalpha",
            "-tr", "10", "10",
            "-tap",
            "-overwrite",
            temp_output,
            final_output
        ]

        try:
            result = subprocess.run(
                gdal_cmd,
                check=True,
                capture_output=True,
                text=True
            )
            print("COG conversion successful!")
            if result.stdout:
                print(result.stdout)

            # Remove temporary file
            import os
            os.remove(temp_output)
            print(f"Removed temporary file: {temp_output}")

        except subprocess.CalledProcessError as e:
            print(f"ERROR: COG conversion failed!")
            print(f"Command: {' '.join(gdal_cmd)}")
            print(f"Return code: {e.returncode}")
            print(f"STDOUT: {e.stdout}")
            print(f"STDERR: {e.stderr}")
            print(f"\nKeeping non-COG file: {temp_output}")
            final_output = temp_output
        except FileNotFoundError:
            print("ERROR: gdalwarp not found. Is GDAL installed and in PATH?")
            print(f"Keeping non-COG file: {temp_output}")
            final_output = temp_output

    print(f"\n✓ Enhanced RGB image created: {final_output}")
    return final_output


if __name__ == "__main__":
    base_path = r"D:\temp\github\topo-satromo-v2"
    base_name = "swisseo_s2-sr_v200_mosaic_2025-06-22t104041"

    b04_file = f"{base_path}\\{base_name}_b04_10m.tif"
    b03_file = f"{base_path}\\{base_name}_b03_10m.tif"
    b02_file = f"{base_path}\\{base_name}_b02_10m.tif"
    clip_orbit_file = f"{base_path}\\assets\\swissboundary_buffer_5000m_8.gpkg"
    output_file = f"{base_path}\\{base_name}_rgb_cog_70.tif"

    # Create enhanced RGB with COG output
    print("=== Creating Enhanced RGB with COG ===")
    create_enhanced_rgb(
        b04_path=b04_file,
        b03_path=b03_file,
        b02_path=b02_file,
        clip_orbit=clip_orbit_file,
        output_path=output_file,
        scale=0.0001,
        offset=-0.1,
        max_r=3.0,
        mid_r=0.13,
        sat=1.2,
        gamma=1.8,
        create_cog=True
    )