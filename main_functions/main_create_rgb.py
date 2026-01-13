import rasterio
import numpy as np
from pathlib import Path

def create_enhanced_rgb(b04_path, b03_path, b02_path, output_path, nodata_value=0,
                        max_r=3.0, mid_r=0.13, sat=1.2, gamma=1.8):
    """
    Create an enhanced RGB composite from Sentinel-2 L2A bands with contrast enhancement
    and highlight compression. Based on https://custom-scripts.sentinel-hub.com/custom-scripts/sentinel-2/l2a_optimized/

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
    nodata_value : float, optional
        NoData value to use in output (default: 0)
    max_r : float, optional
        Maximum reflectance value (default: 3.0)
    mid_r : float, optional
        Mid-point reflectance for contrast adjustment (default: 0.13)
    sat : float, optional
        Saturation enhancement factor (default: 1.2)
    gamma : float, optional
        Gamma correction value (default: 1.8)

    Returns:
    --------
    str
        Path to the created output file
    """

    # Helper functions
    def clip(arr, min_val=0, max_val=1):
        """Clip array values between min and max"""
        return np.clip(arr, min_val, max_val)

    def adj(a, tx, ty, max_c):
        """Contrast enhancement with highlight compression"""
        ar = clip(a / max_c, 0, 1)
        return ar * (ar * (tx / max_c + ty - 1) - ty) / (ar * (2 * tx / max_c - 1) - tx / max_c)

    def adj_gamma(b, gamma_val, g_off=0.01):
        """Apply gamma correction"""
        g_off_pow = g_off ** gamma_val
        g_off_range = (1 + g_off) ** gamma_val - g_off_pow
        return ((b + g_off) ** gamma_val - g_off_pow) / g_off_range

    def s_adj(a, mid_r_val, max_r_val, gamma_val):
        """Combined adjustment and gamma correction"""
        adjusted = adj(a, mid_r_val, 1, max_r_val)
        return adj_gamma(adjusted, gamma_val)

    def sat_enh(r, g, b, sat_val):
        """Saturation enhancement"""
        avg_s = (r + g + b) / 3.0 * (1 - sat_val)
        return (
            clip(avg_s + r * sat_val),
            clip(avg_s + g * sat_val),
            clip(avg_s + b * sat_val)
        )

    def srgb(c):
        """Convert linear RGB to sRGB"""
        return np.where(c <= 0.0031308,
                       12.92 * c,
                       1.055 * np.power(c, 0.41666666666) - 0.055)

    # Read the input bands
    with rasterio.open(b04_path) as src_b04:
        b04 = src_b04.read(1).astype(np.float32)
        profile = src_b04.profile.copy()
        transform = src_b04.transform
        crs = src_b04.crs
        nodata_mask = (b04 == src_b04.nodata) if src_b04.nodata is not None else np.zeros_like(b04, dtype=bool)

    with rasterio.open(b03_path) as src_b03:
        b03 = src_b03.read(1).astype(np.float32)
        if src_b03.nodata is not None:
            nodata_mask |= (b03 == src_b03.nodata)

    with rasterio.open(b02_path) as src_b02:
        b02 = src_b02.read(1).astype(np.float32)
        if src_b02.nodata is not None:
            nodata_mask |= (b02 == src_b02.nodata)

    # Convert to reflectance if needed (assuming values are already 0-1 or will be normalized by max_r)
    # For Sentinel-2 L2A, values are typically scaled (e.g., divide by 10000)
    # Adjust this if your data has different scaling
    if b04.max() > 10:  # Likely scaled integers
        b04 = b04 / 10000.0
        b03 = b03 / 10000.0
        b02 = b02 / 10000.0

    # Apply the enhancement pipeline
    r_adj = s_adj(b04, mid_r, max_r, gamma)
    g_adj = s_adj(b03, mid_r, max_r, gamma)
    b_adj = s_adj(b02, mid_r, max_r, gamma)

    # Saturation enhancement
    r_enh, g_enh, b_enh = sat_enh(r_adj, g_adj, b_adj, sat)

    # Convert to sRGB
    r_final = srgb(r_enh)
    g_final = srgb(g_enh)
    b_final = srgb(b_enh)

    # Scale to 0-255 for 8-bit output
    r_byte = (r_final * 255).astype(np.uint8)
    g_byte = (g_final * 255).astype(np.uint8)
    b_byte = (b_final * 255).astype(np.uint8)

    # Apply nodata mask
    r_byte[nodata_mask] = nodata_value
    g_byte[nodata_mask] = nodata_value
    b_byte[nodata_mask] = nodata_value

    # Update profile for RGB output
    profile.update(
        dtype=rasterio.uint8,
        count=3,
        compress='lzw',
        nodata=nodata_value
    )

    # Write the output
    with rasterio.open(output_path, 'w', **profile) as dst:
        dst.write(r_byte, 1)
        dst.write(g_byte, 2)
        dst.write(b_byte, 3)
        dst.set_band_description(1, 'Red')
        dst.set_band_description(2, 'Green')
        dst.set_band_description(3, 'Blue')

    print(f"Enhanced RGB image created: {output_path}")
    return str(output_path)


