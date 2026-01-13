import rasterio
import numpy as np
from pathlib import Path

def create_enhanced_rgb(b04_path, b03_path, b02_path, output_path,
                        cloud_mask_path=None,
                        nodata_value=0,
                        max_r=3.0,
                        mid_r=0.13,
                        sat=1.2,
                        gamma=1.8,
                        lower_percentile=2,
                        upper_percentile=98,
                        shadow_lift=0.25):
    """
    Create an enhanced RGB composite from Sentinel-2 L2A bands using the Sentinel Hub
    enhancement algorithm with percentile-based normalization.

    This function replicates the visual enhancement from the JavaScript code used in
    Sentinel Hub   https://custom-scripts.sentinel-hub.com/custom-scripts/sentinel-2/l2a_optimized/, adapted for SwissEO Surface Reflectance data.

    Parameters:
    -----------
    b04_path : str or Path
        Path to Band 4 (Red) GeoTIFF file
    b03_path : str or Path
        Path to Band 3 (Green) GeoTIFF file
    b02_path : str or Path
        Path to Band 2 (Blue) GeoTIFF file
    output_path : str or Path
        Path for output RGB GeoTIFF file
    cloud_mask_path : str or Path, optional
        Path to cloud mask raster. For SwissEO: 1=cloud, 3=shadow
    nodata_value : int, optional
        NoData value for output (default: 0)
    max_r : float, optional
        Maximum reflectance value for contrast enhancement (default: 3.0)
    mid_r : float, optional
        Mid-point reflectance for contrast adjustment (default: 0.13)
    sat : float, optional
        Saturation enhancement factor (default: 1.2)
    gamma : float, optional
        Gamma correction value (default: 1.8)
    lower_percentile : float, optional
        Lower percentile for stretch (default: 2)
    upper_percentile : float, optional
        Upper percentile for stretch (default: 98)
    shadow_lift : float, optional
        Amount to brighten shadows/dark areas like lakes (0.0-0.5, default: 0.25)

    Returns:
    --------
    str
        Path to the created output file

    Example:
    --------
    >>> create_enhanced_rgb(
    ...     b04_path="B04.tif",
    ...     b03_path="B03.tif",
    ...     b02_path="B02.tif",
    ...     output_path="rgb_enhanced.tif",
    ...     cloud_mask_path="cloudmask.tif"
    ... )
    """

    # Constants from JS code
    g_off = 0.01
    g_off_pow = g_off ** gamma
    g_off_range = (1 + g_off) ** gamma - g_off_pow

    def clip(s):
        """Clip values to 0-1 range"""
        return np.clip(s, 0, 1)

    def adj(a, tx, ty, max_c):
        """Contrast enhancement with highlight compression"""
        ar = clip(a / max_c)
        denominator = ar * (2 * tx / max_c - 1) - tx / max_c
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
            clip(avg_s + r * sat),
            clip(avg_s + g * sat),
            clip(avg_s + b * sat)
        )

    def srgb(c):
        """Convert linear RGB to sRGB"""
        return np.where(c <= 0.0031308,
                       12.92 * c,
                       1.055 * np.power(np.clip(c, 0, 1), 0.41666666666) - 0.055)

    # Read bands
    print("Reading input bands...")
    with rasterio.open(b04_path) as src_b04:
        b04_raw = src_b04.read(1).astype(np.float32)
        profile = src_b04.profile.copy()
        b04_nodata = src_b04.nodata if src_b04.nodata is not None else 0

    with rasterio.open(b03_path) as src_b03:
        b03_raw = src_b03.read(1).astype(np.float32)
        b03_nodata = src_b03.nodata if src_b03.nodata is not None else 0

    with rasterio.open(b02_path) as src_b02:
        b02_raw = src_b02.read(1).astype(np.float32)
        b02_nodata = src_b02.nodata if src_b02.nodata is not None else 0

    # Build basic NoData mask
    nodata_mask = (b04_raw == b04_nodata) | (b03_raw == b03_nodata) | (b02_raw == b02_nodata)
    nodata_mask |= (b04_raw < 100) | (b03_raw < 100) | (b02_raw < 100)

    # Read cloud mask if provided
    cloud_pixels = np.zeros(b04_raw.shape, dtype=bool)
    shadow_pixels = np.zeros(b04_raw.shape, dtype=bool)

    if cloud_mask_path:
        print(f"Reading cloud mask: {cloud_mask_path}")
        with rasterio.open(cloud_mask_path) as src_cloud:
            cloud_mask_raw = src_cloud.read(1)
            cloud_pixels = (cloud_mask_raw == 1)
            shadow_pixels = (cloud_mask_raw == 3)

            print(f"Clouds: {100*cloud_pixels.sum()/cloud_mask_raw.size:.1f}%, Shadows: {100*shadow_pixels.sum()/cloud_mask_raw.size:.1f}%")

    # Clear land pixels for percentile calculation (exclude clouds and shadows)
    clear_land_pixels = ~(nodata_mask | cloud_pixels | shadow_pixels)
    valid_pixels = ~nodata_mask

    print(f"Clear land pixels for statistics: {100*clear_land_pixels.sum()/nodata_mask.size:.1f}%")

    if clear_land_pixels.sum() < 100:
        print("WARNING: Very few clear pixels, using all valid pixels")
        clear_land_pixels = valid_pixels

    # Convert to reflectance (SwissEO uses scale factor 10000)
    b04 = b04_raw / 10000.0
    b03 = b03_raw / 10000.0
    b02 = b02_raw / 10000.0

    # Apply percentile stretch based on clear land only
    print(f"\nApplying percentile stretch ({lower_percentile}%-{upper_percentile}%) with shadow lift={shadow_lift}")

    # Calculate percentiles from clear land pixels
    b04_low = np.percentile(b04[clear_land_pixels], lower_percentile)
    b04_high = np.percentile(b04[clear_land_pixels], upper_percentile)

    b03_low = np.percentile(b03[clear_land_pixels], lower_percentile)
    b03_high = np.percentile(b03[clear_land_pixels], upper_percentile)

    b02_low = np.percentile(b02[clear_land_pixels], lower_percentile)
    b02_high = np.percentile(b02[clear_land_pixels], upper_percentile)

    # Apply shadow lift (reduces lower bound to brighten dark areas)
    if shadow_lift > 0:
        b04_low = b04_low * (1 - shadow_lift)
        b03_low = b03_low * (1 - shadow_lift)
        b02_low = b02_low * (1 - shadow_lift)

    # Apply stretch to all pixels
    b04 = (b04 - b04_low) / (b04_high - b04_low)
    b03 = (b03 - b03_low) / (b03_high - b03_low)
    b02 = (b02 - b02_low) / (b02_high - b02_low)

    # Clip to 0-1
    b04 = np.clip(b04, 0, 1)
    b03 = np.clip(b03, 0, 1)
    b02 = np.clip(b02, 0, 1)

    # Apply enhancement pipeline
    print(f"Applying enhancement: max_r={max_r}, mid_r={mid_r}, sat={sat}, gamma={gamma}")
    r_adj = s_adj(b04)
    g_adj = s_adj(b03)
    b_adj = s_adj(b02)

    # Saturation enhancement
    r_enh, g_enh, b_enh = sat_enh(r_adj, g_adj, b_adj)

    # Convert to sRGB
    r_final = srgb(r_enh)
    g_final = srgb(g_enh)
    b_final = srgb(b_enh)

    # Scale to 0-255
    r_byte = (np.clip(r_final, 0, 1) * 255).astype(np.uint8)
    g_byte = (np.clip(g_final, 0, 1) * 255).astype(np.uint8)
    b_byte = (np.clip(b_final, 0, 1) * 255).astype(np.uint8)

    print(f"Final bytes (clear land): R={r_byte[clear_land_pixels].mean():.1f}, G={g_byte[clear_land_pixels].mean():.1f}, B={b_byte[clear_land_pixels].mean():.1f}")

    # Apply nodata mask
    r_byte[nodata_mask] = nodata_value
    g_byte[nodata_mask] = nodata_value
    b_byte[nodata_mask] = nodata_value

    # Update profile for RGB output
    profile.update(
        dtype=rasterio.uint8,
        count=3,
        compress='lzw',
        nodata=nodata_value,
        photometric='rgb'
    )

    # Write output
    with rasterio.open(output_path, 'w', **profile) as dst:
        dst.write(r_byte, 1)
        dst.write(g_byte, 2)
        dst.write(b_byte, 3)
        dst.colorinterp = [rasterio.enums.ColorInterp.red,
                           rasterio.enums.ColorInterp.green,
                           rasterio.enums.ColorInterp.blue]

    print(f"\n✓ Enhanced RGB image created: {output_path}")
    return str(output_path)


if __name__ == "__main__":
    # Example usage with SwissEO data
    base_path = r"D:\temp\github\topo-satromo-v2"
    base_name = "swisseo_s2-sr_v200_mosaic_2025-06-01t101041"

    b04_file = f"{base_path}\\{base_name}_b04_10m.tif"
    b03_file = f"{base_path}\\{base_name}_b03_10m.tif"
    b02_file = f"{base_path}\\{base_name}_b02_10m.tif"
    cloud_mask_file = f"{base_path}\\{base_name}_cloudmask_10m.tif"
    output_file = f"{base_path}\\{base_name}_rgb_enhanced.tif"

    create_enhanced_rgb(
        b04_path=b04_file,
        b03_path=b03_file,
        b02_path=b02_file,
        output_path=output_file,
        cloud_mask_path=cloud_mask_file,
        nodata_value=0,
        max_r=3.0,
        mid_r=0.13,
        sat=1.2,
        gamma=1.8,
        lower_percentile=2,
        upper_percentile=98,
        shadow_lift=0.25
    )