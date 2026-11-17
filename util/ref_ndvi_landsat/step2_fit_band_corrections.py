# Step 2 of the sensor comparison: measures the difference between Landsat and swissEO S2-SR
# and fits one band correction per Landsat sensor: corrected_landsat = intercept + slope * landsat
#
# Outputs in output/results:
# - band_corrections.csv   slope and intercept per sensor and band, with 95% confidence intervals
# - ndvi_bias_by_bin.csv   S2-SR NDVI minus Landsat NDVI per NDVI bin, before and after the correction
# - bias_summary.csv       median difference per sensor, class and season
# - ndvi_bias_by_bin.png and band_scatter.png
#
# Confidence intervals come from resampling whole scene pairs, because the pixels of one scene are not independent.
#
# Usage: python util/ref_ndvi_landsat/step2_fit_band_corrections.py

import glob
import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import vi_reference_config as config

BANDS = ["red", "nir"]
BIAS_PLOT_LIMIT = 0.15
RAW_COLOR = "#2a78d6"
CORRECTED_COLOR = "#eb6834"
TEXT_COLOR = "#52514e"
GRID_COLOR = "#e8e7e3"


##############################
# LOAD DATA
def load_matchups():
    files = glob.glob(os.path.join(config.MATCHUP_DIR, "*.parquet"))
    table = pd.concat([pd.read_parquet(file) for file in files], ignore_index=True)
    print(f"Loaded {len(table)} pixels from {len(files)} scene pairs")

    all_positive = (table[["landsat_red", "landsat_nir", "s2_red", "s2_nir"]] > 0).all(axis=1)
    table = table[all_positive].copy()

    homogeneous = np.ones(len(table), dtype=bool)
    for class_name, max_cv in config.MAX_NIR_CV.items():
        if max_cv is None:
            continue
        in_class = (table["class_name"] == class_name).to_numpy()
        passes = (table["cv_s2_nir"] <= max_cv) & (table["cv_landsat_nir"] <= max_cv)
        homogeneous[in_class & ~passes.to_numpy()] = False
    table = table[homogeneous].copy()
    print(f"{len(table)} pixels left after the positive reflectance and homogeneity filters")
    print(table.groupby(["sensor", "class_name"]).size().to_string())
    return table


def calculate_ndvi(red, nir):
    return (nir - red) / (nir + red)


##############################
# FITTING
def get_class_weights(class_names):
    """Every class gets the same total weight, so that vegetation pixels do not dominate the fit."""
    counts = pd.Series(class_names).map(pd.Series(class_names).value_counts()).to_numpy()
    return 1.0 / counts


def fit_orthogonal(x, y, weights):
    """Orthogonal regression of y on x. Unlike normal regression, it allows noise in both sensors."""
    mean_x = np.average(x, weights=weights)
    mean_y = np.average(y, weights=weights)
    var_x = np.average((x - mean_x) ** 2, weights=weights)
    var_y = np.average((y - mean_y) ** 2, weights=weights)
    cov_xy = np.average((x - mean_x) * (y - mean_y), weights=weights)
    slope = (var_y - var_x + np.sqrt((var_y - var_x) ** 2 + 4 * cov_xy ** 2)) / (2 * cov_xy)
    intercept = mean_y - slope * mean_x
    return slope, intercept


def fit_bands(table):
    weights = get_class_weights(table["class_name"].to_numpy())
    corrections = {}
    for band in BANDS:
        corrections[band] = fit_orthogonal(table[f"landsat_{band}"].to_numpy(), table[f"s2_{band}"].to_numpy(), weights)
    return corrections


def apply_corrections(table, corrections):
    red_slope, red_intercept = corrections["red"]
    nir_slope, nir_intercept = corrections["nir"]
    corrected_red = red_intercept + red_slope * table["landsat_red"].to_numpy()
    corrected_nir = nir_intercept + nir_slope * table["landsat_nir"].to_numpy()
    return calculate_ndvi(corrected_red, corrected_nir)


##############################
# NDVI BINS
def get_bin_edges():
    return np.round(np.arange(-1, 1 + config.NDVI_BIN_WIDTH, config.NDVI_BIN_WIDTH), 2)


def median_by_bin(ndvi_a, ndvi_b):
    """Median of (a - b) per bin. The mean of both sensors picks the bin, so neither one is treated as the truth."""
    edges = get_bin_edges()
    bin_numbers = np.digitize((ndvi_a + ndvi_b) / 2, edges) - 1
    differences = ndvi_a - ndvi_b
    medians = np.full(len(edges) - 1, np.nan)
    for number in range(len(medians)):
        values = differences[bin_numbers == number]
        if values.size > 0:
            medians[number] = np.median(values)
    return medians


def count_by_bin(table, ndvi_a, ndvi_b):
    edges = get_bin_edges()
    bin_numbers = np.digitize((ndvi_a + ndvi_b) / 2, edges) - 1
    counts = pd.DataFrame({"bin": bin_numbers, "pair_id": table["pair_id"].to_numpy()})
    pixels = counts.groupby("bin").size()
    dates = counts.groupby("bin")["pair_id"].nunique()
    return pixels, dates


def analyse_sensor(sensor, table, rng):
    print(f"\nAnalysing {sensor}")
    table = table[table["sensor"] == sensor].reset_index(drop=True)
    s2_ndvi = calculate_ndvi(table["s2_red"].to_numpy(), table["s2_nir"].to_numpy())
    landsat_ndvi = calculate_ndvi(table["landsat_red"].to_numpy(), table["landsat_nir"].to_numpy())

    corrections = fit_bands(table)
    corrected_ndvi = apply_corrections(table, corrections)
    raw_medians = median_by_bin(s2_ndvi, landsat_ndvi)
    corrected_medians = median_by_bin(s2_ndvi, corrected_ndvi)

    # Resample whole scene pairs
    pair_ids = table["pair_id"].unique()
    pair_rows = {pair_id: np.nonzero(table["pair_id"].to_numpy() == pair_id)[0] for pair_id in pair_ids}
    boot_fits = {band: [] for band in BANDS}
    boot_raw = []
    boot_corrected = []
    for run in range(config.BOOTSTRAP_RUNS):
        chosen = rng.choice(pair_ids, len(pair_ids), replace=True)
        rows = np.concatenate([pair_rows[pair_id] for pair_id in chosen])
        sample = table.iloc[rows]
        sample_fit = fit_bands(sample)
        for band in BANDS:
            boot_fits[band].append(sample_fit[band])
        boot_raw.append(median_by_bin(s2_ndvi[rows], landsat_ndvi[rows]))
        boot_corrected.append(median_by_bin(s2_ndvi[rows], apply_corrections(sample, sample_fit)))
    boot_raw = np.array(boot_raw)
    boot_corrected = np.array(boot_corrected)

    correction_rows = []
    for band in BANDS:
        fits = np.array(boot_fits[band])
        correction_rows.append({
            "sensor": sensor, "band": band, "slope": corrections[band][0], "intercept": corrections[band][1],
            "slope_low": np.percentile(fits[:, 0], 2.5), "slope_high": np.percentile(fits[:, 0], 97.5),
            "intercept_low": np.percentile(fits[:, 1], 2.5), "intercept_high": np.percentile(fits[:, 1], 97.5),
            "scene_pairs": len(pair_ids), "pixels": len(table),
        })

    pixels, dates = count_by_bin(table, s2_ndvi, landsat_ndvi)
    edges = get_bin_edges()
    bin_rows = []
    for number in range(len(edges) - 1):
        n_pixels = int(pixels.get(number, 0))
        n_dates = int(dates.get(number, 0))
        verified = n_pixels >= config.MIN_PIXELS_PER_BIN and n_dates >= config.MIN_DATES_PER_BIN
        bin_rows.append({
            "sensor": sensor, "bin_low": edges[number], "bin_high": edges[number + 1],
            "pixels": n_pixels, "scene_pairs": n_dates, "verified": verified,
            "bias_raw": raw_medians[number],
            "bias_raw_low": np.nanpercentile(boot_raw[:, number], 2.5) if n_pixels else np.nan,
            "bias_raw_high": np.nanpercentile(boot_raw[:, number], 97.5) if n_pixels else np.nan,
            "bias_corrected": corrected_medians[number],
            "bias_corrected_low": np.nanpercentile(boot_corrected[:, number], 2.5) if n_pixels else np.nan,
            "bias_corrected_high": np.nanpercentile(boot_corrected[:, number], 97.5) if n_pixels else np.nan,
        })

    table["bias_raw"] = s2_ndvi - landsat_ndvi
    table["bias_corrected"] = s2_ndvi - corrected_ndvi
    summary = table.groupby(["sensor", "class_name", "season"]).agg(
        pixels=("bias_raw", "size"), scene_pairs=("pair_id", "nunique"),
        bias_raw=("bias_raw", "median"), bias_corrected=("bias_corrected", "median")).reset_index()
    return correction_rows, bin_rows, summary


##############################
# PLOTS
def style_axis(axis):
    axis.grid(True, color=GRID_COLOR, linewidth=0.8)
    axis.set_axisbelow(True)
    axis.tick_params(colors=TEXT_COLOR, length=0)
    for side in axis.spines:
        axis.spines[side].set_visible(False)


def plot_bins(bins):
    sensors = list(bins["sensor"].unique())
    figure, axes = plt.subplots(1, len(sensors), figsize=(6 * len(sensors), 4.5), sharey=True, squeeze=False)
    for axis, sensor in zip(axes[0], sensors):
        data = bins[(bins["sensor"] == sensor) & (bins["pixels"] > 0)]
        centers = (data["bin_low"] + data["bin_high"]) / 2
        style_axis(axis)
        axis.axhspan(-config.BIAS_THRESHOLD, config.BIAS_THRESHOLD, color="#f0efec", zorder=0,
                     label=f"Threshold ±{config.BIAS_THRESHOLD}")
        for name, label, color in [("bias_raw", "Before correction", RAW_COLOR),
                                   ("bias_corrected", "After correction", CORRECTED_COLOR)]:
            axis.fill_between(centers, data[name + "_low"], data[name + "_high"], color=color, alpha=0.15, linewidth=0)
            axis.plot(centers, data[name], color=color, linewidth=2, label=label)
            verified = data["verified"]
            axis.scatter(centers[verified], data[name][verified], color=color, s=36, zorder=3)
            axis.scatter(centers[~verified], data[name][~verified], facecolor="white", edgecolor=color, s=36, zorder=3)
        axis.set_ylim(-BIAS_PLOT_LIMIT, BIAS_PLOT_LIMIT)
        axis.set_title(sensor, color=TEXT_COLOR, loc="left")
        axis.set_xlabel("NDVI (mean of both sensors)", color=TEXT_COLOR)
    axes[0][0].set_ylabel("S2-SR NDVI minus Landsat NDVI", color=TEXT_COLOR)
    axes[0][0].legend(frameon=False, labelcolor=TEXT_COLOR)
    figure.text(0.01, 0.01, f"Filled markers: verified bins. Open markers: too little data. Shaded: 95% confidence interval. Y axis cut at ±{BIAS_PLOT_LIMIT}.",
                color=TEXT_COLOR, fontsize=9)
    figure.tight_layout(rect=(0, 0.04, 1, 1))
    figure.savefig(os.path.join(config.RESULT_DIR, "ndvi_bias_by_bin.png"), dpi=150)
    plt.close(figure)


def plot_scatter(table, corrections):
    sensors = list(table["sensor"].unique())
    figure, axes = plt.subplots(len(sensors), len(BANDS), figsize=(5.5 * len(BANDS), 5 * len(sensors)), squeeze=False)
    for row, sensor in enumerate(sensors):
        data = table[table["sensor"] == sensor]
        for col, band in enumerate(BANDS):
            axis = axes[row][col]
            x = data[f"landsat_{band}"].to_numpy()
            y = data[f"s2_{band}"].to_numpy()
            upper = np.percentile(np.concatenate([x, y]), 99.9)
            axis.hexbin(x, y, gridsize=120, extent=(0, upper, 0, upper), bins="log", cmap="Blues", mincnt=1)
            fit = corrections[(corrections["sensor"] == sensor) & (corrections["band"] == band)].iloc[0]
            axis.plot([0, upper], [0, upper], color=TEXT_COLOR, linewidth=1, linestyle="--", label="1:1")
            axis.plot([0, upper], [fit["intercept"], fit["intercept"] + fit["slope"] * upper], color=CORRECTED_COLOR,
                      linewidth=2, label=f"y = {fit['intercept']:.4f} + {fit['slope']:.4f} x")
            axis.set_xlim(0, upper)
            axis.set_ylim(0, upper)
            axis.set_title(f"{sensor}: {band}", color=TEXT_COLOR, loc="left")
            axis.set_xlabel("Landsat reflectance", color=TEXT_COLOR)
            axis.set_ylabel("S2-SR reflectance", color=TEXT_COLOR)
            axis.legend(frameon=False, labelcolor=TEXT_COLOR)
    figure.tight_layout()
    figure.savefig(os.path.join(config.RESULT_DIR, "band_scatter.png"), dpi=150)
    plt.close(figure)


def main():
    os.makedirs(config.RESULT_DIR, exist_ok=True)
    table = load_matchups()
    rng = np.random.default_rng(config.RANDOM_SEED)

    all_corrections = []
    all_bins = []
    all_summaries = []
    for sensor in sorted(table["sensor"].unique()):
        correction_rows, bin_rows, summary = analyse_sensor(sensor, table, rng)
        all_corrections.extend(correction_rows)
        all_bins.extend(bin_rows)
        all_summaries.append(summary)

    corrections = pd.DataFrame(all_corrections)
    bins = pd.DataFrame(all_bins)
    corrections.to_csv(config.BAND_CORRECTIONS_FILE, index=False)
    bins.to_csv(os.path.join(config.RESULT_DIR, "ndvi_bias_by_bin.csv"), index=False)
    pd.concat(all_summaries).to_csv(os.path.join(config.RESULT_DIR, "bias_summary.csv"), index=False)

    plot_bins(bins)
    plot_scatter(table, corrections)

    print("\nBand corrections")
    print(corrections.round(5).to_string(index=False))
    print("\nNDVI difference per bin")
    print(bins.round(4).to_string(index=False))
    over_threshold = bins[bins["verified"] & (bins["bias_raw"].abs() > config.BIAS_THRESHOLD)]
    print(f"\nVerified bins above the {config.BIAS_THRESHOLD} threshold before correction: {len(over_threshold)} of {int(bins['verified'].sum())}")
    still_over = bins[bins["verified"] & (bins["bias_corrected"].abs() > config.BIAS_THRESHOLD)]
    print(f"Verified bins above the threshold after correction: {len(still_over)}")


if __name__ == "__main__":
    main()
