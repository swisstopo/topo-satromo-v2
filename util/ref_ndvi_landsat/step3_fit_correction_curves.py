# Step 3 of the sensor comparison: fits smooth NDVI correction curves.
#
# For each Landsat sensor group and season, the script measures how much the S2-SR NDVI differs from the
# Landsat NDVI, as a function of the Landsat NDVI. Only vegetation and forest pixels are used.
# A polynomial is then fitted through these differences. The curve is what step 4 adds to the Landsat NDVI:
#
#     corrected Landsat NDVI = Landsat NDVI + correction(Landsat NDVI, sensor group, season)
#
# The Landsat NDVI (and not the mean of both sensors) is used on the x axis, because the Landsat NDVI is the only
# value that exists for the years before Sentinel-2.
#
# How a curve is made:
# 1. The Landsat NDVI is cut into bins (CURVE_BIN_WIDTH wide).
# 2. Each scene pair gives one median difference per bin. Pixels of one scene are not independent,
#    so the scene pair is the unit that counts, not the pixel.
# 3. The median over the scene pairs is the value of the bin.
# 4. A polynomial is fitted through the bin values. Bins with more scene pairs weigh more.
# 5. Outside the NDVI range that has data, the curve stays at its last value (no extrapolation).
#
# Outputs in output/results:
# - ndvi_correction_curves.csv   the curves, one row per sensor group, season and NDVI step
# - ndvi_correction_bins.csv     the bin values the curves were fitted to
# - correction_curves_landsat5_7.png and correction_curves_landsat8.png
#
# Usage: python util/ref_ndvi_landsat/step3_fit_correction_curves.py

import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import vi_reference_config as config
from step2_fit_band_corrections import calculate_ndvi, load_matchups

BINS_FILE = os.path.join(config.RESULT_DIR, "ndvi_correction_bins.csv")
SEASON_COLORS = {"DJF": "#0072B2", "MAM": "#009E73", "JJA": "#E69F00", "SON": "#D55E00"}
TEXT_COLOR = "#52514e"
GRID_COLOR = "#e8e7e3"
PLOT_LIMIT = 0.12  # y axis of the figures, in NDVI
FIGURES = {
    "landsat-7": ("correction_curves_landsat5_7.png", "Landsat 5 and 7 (fitted on Landsat 7)"),
    "landsat-8": ("correction_curves_landsat8.png", "Landsat 8"),
}


##############################
# PREPARE DATA
def load_vegetation_pixels():
    """Return the vegetation and forest pixels with the Landsat NDVI and the difference S2-SR minus Landsat."""
    table = load_matchups()
    table = table[table["class_name"].isin(config.CURVE_CLASSES)].copy()
    table["landsat_ndvi"] = calculate_ndvi(table["landsat_red"].to_numpy(), table["landsat_nir"].to_numpy())
    table["s2_ndvi"] = calculate_ndvi(table["s2_red"].to_numpy(), table["s2_nir"].to_numpy())
    table["difference"] = table["s2_ndvi"] - table["landsat_ndvi"]
    print(f"\n{len(table)} vegetation and forest pixels are used for the curves")
    return table


def get_bin_values(table):
    """Return one row per NDVI bin: the median over the scene pairs of the median difference in that bin."""
    edges = np.arange(0, 1 + config.CURVE_BIN_WIDTH / 2, config.CURVE_BIN_WIDTH)
    table = table.assign(bin_number=np.digitize(table["landsat_ndvi"], edges) - 1)
    table = table[(table["bin_number"] >= 0) & (table["bin_number"] < len(edges) - 1)]

    # Step 2: one median per scene pair and bin
    per_scene = table.groupby(["bin_number", "pair_id"])["difference"].agg(["size", "median"]).reset_index()
    per_scene = per_scene[per_scene["size"] >= config.CURVE_MIN_PIXELS_PER_SCENE_BIN]

    # Step 3: median over the scene pairs
    per_bin = per_scene.groupby("bin_number").agg(scene_pairs=("median", "size"), pixels=("size", "sum"),
                                                  difference=("median", "median")).reset_index()
    per_bin = per_bin[per_bin["scene_pairs"] >= config.CURVE_MIN_SCENES_PER_BIN].copy()
    per_bin["ndvi"] = edges[per_bin["bin_number"]] + config.CURVE_BIN_WIDTH / 2
    return per_bin[["ndvi", "difference", "scene_pairs", "pixels"]]


##############################
# FIT
def fit_curve(bin_values, wanted_degree):
    """Fit a polynomial through the bin values. Returns the coefficients and the degree that was used."""
    # A polynomial needs at least degree + 1 points
    degree = min(wanted_degree, len(bin_values) - 1)
    # polyfit squares the weights, so the square root of the scene pair count gives weights proportional to the count
    weights = np.sqrt(bin_values["scene_pairs"].to_numpy())
    coefficients = np.polyfit(bin_values["ndvi"].to_numpy(), bin_values["difference"].to_numpy(), degree, w=weights)
    return coefficients, degree


def calculate_curve(coefficients, bin_values):
    """Evaluate the polynomial from CURVE_MIN_NDVI to CURVE_MAX_NDVI.
    Outside of the NDVI range with data the curve keeps the value of the nearest bin that has data."""
    ndvi = np.round(np.arange(config.CURVE_MIN_NDVI, config.CURVE_MAX_NDVI + config.CURVE_STEP / 2, config.CURVE_STEP), 4)
    first_ndvi = bin_values["ndvi"].min()
    last_ndvi = bin_values["ndvi"].max()
    half_bin = config.CURVE_BIN_WIDTH / 2
    supported = (ndvi >= first_ndvi - half_bin) & (ndvi <= last_ndvi + half_bin)
    correction = np.polyval(coefficients, np.clip(ndvi, first_ndvi, last_ndvi))
    return pd.DataFrame({"ndvi": ndvi, "correction": correction, "supported": supported})


##############################
# FIGURES
def style_axis(axis):
    axis.grid(True, color=GRID_COLOR, linewidth=0.8)
    axis.set_axisbelow(True)
    axis.tick_params(colors=TEXT_COLOR, length=0)
    for side in axis.spines:
        axis.spines[side].set_visible(False)


def plot_group(group, curves, bins):
    file_name, title = FIGURES[group]
    figure, axis = plt.subplots(figsize=(8, 5.5))
    style_axis(axis)
    axis.axhspan(-config.BIAS_THRESHOLD, config.BIAS_THRESHOLD, color="#f0efec", zorder=0,
                 label=f"±{config.BIAS_THRESHOLD}")
    for season in config.SEASON_NAMES:
        curve = curves[(curves["curve_group"] == group) & (curves["season"] == season)]
        points = bins[(bins["curve_group"] == group) & (bins["season"] == season)]
        if curve.empty:
            continue
        color = SEASON_COLORS[season]
        scene_pairs = int(points["scene_pairs"].max())
        degree = int(curve["degree"].iloc[0])
        shape_name = {0: "constant", 1: "straight line"}.get(degree, "curve")
        label = f"{season}: {shape_name}, up to {scene_pairs} scene pairs per bin"
        axis.scatter(points["ndvi"], points["difference"], color=color, s=18, alpha=0.45, linewidth=0)
        # Missing values (NaN) break the line, so the dotted part is only drawn where there is no data
        axis.plot(curve["ndvi"], curve["correction"].where(~curve["supported"]), color=color, linewidth=1.2, linestyle=":")
        axis.plot(curve["ndvi"], curve["correction"].where(curve["supported"]), color=color, linewidth=2.2, label=label)
    axis.axhline(0, color=TEXT_COLOR, linewidth=0.8)
    axis.set_ylim(-PLOT_LIMIT, PLOT_LIMIT)
    axis.set_xlim(config.CURVE_MIN_NDVI, config.CURVE_MAX_NDVI)
    axis.set_title(title, color=TEXT_COLOR, loc="left")
    axis.set_xlabel("Landsat NDVI", color=TEXT_COLOR)
    axis.set_ylabel("Correction = S2-SR NDVI minus Landsat NDVI", color=TEXT_COLOR)
    axis.legend(frameon=False, labelcolor=TEXT_COLOR, fontsize=9, loc="upper left")
    figure.text(0.01, 0.01, "Dots: median per NDVI bin (bins with at least "
                f"{config.CURVE_MIN_SCENES_PER_BIN} scene pairs). Dotted: no data, the curve keeps its last value. "
                "Grey band: threshold.", color=TEXT_COLOR, fontsize=8)
    figure.tight_layout(rect=(0, 0.03, 1, 1))
    figure.savefig(os.path.join(config.RESULT_DIR, file_name), dpi=150)
    plt.close(figure)


##############################
# MAIN
def main():
    os.makedirs(config.RESULT_DIR, exist_ok=True)
    table = load_vegetation_pixels()

    all_curves = []
    all_bins = []
    for group in config.CURVE_GROUPS:
        for season in config.SEASON_NAMES:
            data = table[(table["sensor"] == group) & (table["season"] == season)]
            bin_values = get_bin_values(data)
            if len(bin_values) < 2:
                print(f"{group} {season}: not enough data for a curve ({len(bin_values)} bins)")
                continue
            wanted_degree = config.CURVE_DEGREE_WINTER if season == "DJF" else config.CURVE_DEGREE
            coefficients, degree = fit_curve(bin_values, wanted_degree)
            curve = calculate_curve(coefficients, bin_values)
            residual = bin_values["difference"].to_numpy() - np.polyval(coefficients, bin_values["ndvi"].to_numpy())
            print(f"{group} {season}: {data['pair_id'].nunique()} scene pairs, {len(bin_values)} bins "
                  f"from NDVI {bin_values['ndvi'].min():.3f} to {bin_values['ndvi'].max():.3f}, "
                  f"polynomial degree {degree}, typical distance to the bin values {np.sqrt(np.mean(residual ** 2)):.4f}")
            all_curves.append(curve.assign(curve_group=group, season=season, degree=degree))
            all_bins.append(bin_values.assign(curve_group=group, season=season))

    curves = pd.concat(all_curves, ignore_index=True)
    bins = pd.concat(all_bins, ignore_index=True)
    curves = curves[["curve_group", "season", "ndvi", "correction", "supported", "degree"]]
    curves.to_csv(config.CORRECTION_CURVES_FILE, index=False, float_format="%.5f")
    bins.to_csv(BINS_FILE, index=False, float_format="%.5f")

    for group in config.CURVE_GROUPS:
        plot_group(group, curves, bins)

    print("\nCorrection at some NDVI values (S2-SR minus Landsat)")
    selected = curves[curves["ndvi"].isin([0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0])]
    print(selected.pivot_table(index=["curve_group", "season"], columns="ndvi", values="correction").round(3).to_string())
    print(f"\nWritten to {config.RESULT_DIR}")


if __name__ == "__main__":
    main()
