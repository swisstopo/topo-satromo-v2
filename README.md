[![GitHub commit](https://img.shields.io/github/last-commit/swisstopo/topo-satromo-v2)](https://github.com/swisstopo/topo-satromo-v2/commits/main)[![Python 3.12](https://img.shields.io/badge/python-3.12.13-blue?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-31213/)[![Python 3.11](https://img.shields.io/badge/python-3.11.3-blue?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3113/)[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/swisstopo/topo-satromo-v2)
# topo-satromo-v2

**Erdbeobachtungs-SAtellitendaten fürs TRockenheitsMOnitoring (SATROMO) — Version 2**

An operational Python ETL pipeline for generating and publishing Analysis-Ready Data (ARD) and vegetation/drought indices from Sentinel-2 satellite imagery, using CSDE, AWS S3, STAC catalogs, and GitHub Actions.

|                  | swissEO S2-SR | swissEO VHI |
|------------------|---------------|-------------|
| Data description | [Product site](https://www.swisstopo.admin.ch/en/satelliteimage-swisseo-s2-sr) | [Product site](https://www.swisstopo.admin.ch/en/satelliteimage-swisseo-vhi) |
| Access to data   | [STAC](https://data.geo.admin.ch/browser/index.html#/collections/ch.swisstopo.swisseo_s2-sr_v200) | *not yet operational* |

> **Note:** This project is currently in the commissioning phase and is not yet fully operational.



## Overview

SATROMO v2 is a serverless satellite data processing chain for Switzerland that:

1. **step0** — Checks availability of input data in STAC CSDE and STAC swisstopo catalogs (Sentinel-2 L2A)
2. **step1** — Generates ARD products and publishes to STAC  (co-registered, cloud/shadow masked, topographic corrected Sentinel-2 mosaics) and indices (VHI, NDVI, etc.)


Two deployment environments are supported:
- **DEV** — local machine with Python
- **PROD** — GitHub Actions, triggered hourly by an external scheduler, with GPU
  processing on a temporary AWS EC2 runner (see [Production Runtime](#production-runtime-github-actions--ec2))

---

## Architecture

```
satromo_processor.py              # Daily orchestration (step0 → step1)
    │
    ├── step0_functions.py        # Check STAC / S3, decide what is ready
    │       └── step0_processors/ # Shared step0 helpers (step0_utils.py)
    │
    ├── step1_processors/         # Product generation (S2-SR, VHI, ...)
    │
    ├── main_functions/           #  STAC utils, S3 helpers
    │
    └── configuration/            # dev_config.py, prod_config.py, ...

check_s2_sr.py                    # Lightweight availability check (CI stage 1)
rerun.py                          # Batch reprocessing driver (CI stage 2, GPU EC2)
tools/step0_empty_assets.csv      # Live per-date/tile status, committed by CI
```

The `step0_function` registered per collection in the config is the callable that
produces the data for a date. For S2-SR this is the step1 processor itself
(`step1_processor_s2_sr.process_product_s2_sr`); `step0_processors/` holds only
shared helper code, not per-collection generators.

Full architecture documentation: [deepwiki.com/swisstopo/topo-satromo-v2](https://deepwiki.com/swisstopo/topo-satromo-v2)

---

## Installation

### Prerequisites

- Ubuntu Linux 24.04 — the reference and production platform (GitHub runners and
  the EC2 GPU instance both run 24.04). Windows and macOS are experimental.
- GDAL 3.11 (command-line tools; the pipeline calls GDAL via `subprocess`)
- Python 3.11 or 3.12 (64-bit)
- `pip`
- Virtual environment (strongly recommended)
- A `secrets/` folder with credentials (see [Secrets Setup](#secrets-setup))

---

### 1.  AROSICS 

#### LINUX — AROSICS Setup 

##### 1. Install requirements

```bat
pip install -r requirements.txt
```

##### 2. Install AROSICS

```bat
pip install arosics
```

##### 3. Verify AROSICS

```bat
python -c "import arosics; print('AROSICS OK')"
python -c "from osgeo import gdal; print('GDAL OK:', gdal.__version__)"

```
#### Windows — AROSICS Setup (EXPERIMENTAL)

AROSICS requires a pre-compiled GDAL wheel on Windows due to C++ build dependencies. Follow the steps in order.

##### 1. Create and activate a virtual environment

```bat
python -m venv venv
venv\Scripts\activate
```

##### 2. Check your Python version

```bat
python --version
```

##### 3. Download and install the GDAL wheel

GDAL must be installed **before** `arosics` and **before** `requirements.txt`.

1. Go to: https://github.com/cgohlke/geospatial-wheels/releases
2. Download the wheel matching your Python version:
   - Python 3.11 → `GDAL-X.X.X-cp311-cp311-win_amd64.whl`
   - Python 3.12 → `GDAL-X.X.X-cp312-cp312-win_amd64.whl`
3. Install it:

```bat
pip install path\to\GDAL-X.X.X-cpXXX-cpXXX-win_amd64.whl
```

> The `requirements.txt` also references a local GDAL wheel at `secrets/gdal-*.whl` for reproducibility. Place your downloaded wheel there if preferred.

##### 4. Install requirements

```bat
pip install -r requirements.txt
```

##### 5. Install AROSICS

```bat
pip install arosics
```

##### 6. Verify AROSICS

```bat
python -c "import arosics; print('AROSICS OK')"
python -c "from osgeo import gdal; print('GDAL OK:', gdal.__version__)"

```
### 2. Install HORAYZON 

#### LINUX — HORAYZON Setup with CONDA
Follow the official [instructions on the HORAYZON repo](https://github.com/ChristianSteger/HORAYZON#package-dependencies)

**Troubleshooting:**

| Error | Cause | Fix |
|-------|-------|-----|
| `Microsoft Visual C++ 14.0 required` | GDAL wheel not installed | Complete step 3 first |
| `Wrong wheel` error | Python version mismatch | Recheck `python --version` and download the matching wheel |
| Import errors after `pip install arosics` | GDAL installed after arosics | Reinstall: uninstall both, reinstall GDAL wheel first |

---
#### LINUX — HORAYZON Setup with PIP

##### 1. Installing HORAYZON Native on Linux

```bat
bashsudo apt update

# Intel Embree
sudo apt install -y libembree-dev

# Threading Building Blocks (TBB)
sudo apt install -y libtbb-dev

export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH

```
the follow [Installation without Conda](https://github.com/ChristianSteger/HORAYZON#installation-without-conda)

```bat
# Activate your venv
source /path/to/your/venv/bin/activate

# Find Embree headers
find /usr -name "embree4" -type d 2>/dev/null
find /usr -name "rtcore.h" 2>/dev/null

# Find Embree library
find /usr -name "libembree*" 2>/dev/null

# Find TBB
find /usr -name "libtbb*" 2>/dev/null
find /usr -name "tbb" -type d 2>/dev/null

# Paths for Intel Embree and Threading Building Blocks (TBB) are for example
path_include = ["/usr/include/"]
path_lib = ["/usr/lib/x86_64-linux-gnu/libembree4"]  # without file ending

#We need clang++ . if it is missing. Install it:
sudo apt install -y clang

# Clone and enter HORAYZON
git clone https://github.com/ChristianSteger/HORAYZON.git
cd HORAYZON

# Edit setup_manual.py to set the paths above
# path_include = ["/usr/include/"]
# path_lib = ["/usr/lib/x86_64-linux-gnu/libembree4"]
nano setup_manual.py

# Rename and install into the active venv
mv setup_manual.py setup.py
python -m pip install .

```

After successful isntallation: remove the HORAYZON directory

##### 2. EGM96 Geoid Data Setup

HORAYZON requires EGM96 geoid data for ellipsoidal height correction. By default,
HORAYZON attempts to download this data automatically from an external server.
To avoid network blocking (e.g. since we donaloda it quite often), copy the
data manually into your local assets folder before running the pipeline.

**Required file structure:**
```
local_assets/
└── EGM/
    └── EGM96/
        └── WW15MGH.GRD
```

Copy the EGM96 data into the `local_assets/EGM/EGM96/` folder, then register
the path with HORAYZON by writing it to its configuration file. Run once in
a command prompt (adapt the path to your installation):

Copy the EGM96 data into `local_assets/EGM/EGM96/`, then register the path:

```bash
echo "local_assets/EGM/" > \
  "$(python -c 'import horayzon, os; print(os.path.join(os.path.split(os.path.dirname(horayzon.__file__))[0], "horayzon"))')/path_aux_data.txt"
```

Example:
```bash
echo "local_assets/EGM/" > \
  "/home/user/topo-satromo-v2/.venv/lib/python3.11/site-packages/horayzon/path_aux_data.txt"
```

Verify the content:
```bash
cat "$(python -c 'import horayzon, os; print(os.path.join(os.path.split(os.path.dirname(horayzon.__file__))[0], "horayzon"))')/path_aux_data.txt"
```

> **Note:** This step is required only once per virtual environment. The path is
> stored permanently in `path_aux_data.txt` inside the HORAYZON package directory.
> If you recreate the virtual environment or move the project, repeat this step.

#### Windows — HORAYZON Setup (EXPERIMENTAL)

##### 1. Compiling and Installing HORAYZON Native on Windows
Create a wheel file and install it, follow [this installation guide](https://github.com/davidoesch/HORAYZON/blob/main/WINDOWS_Install_Native.md#compiling-and-installing-horayzon-native-on-windows)
Mind the [Step Windows DLL](https://github.com/davidoesch/HORAYZON/blob/main/WINDOWS_Install_Native.md#step-4-the-windows-dll-fix-crucial)

##### 2. EGM96 Geoid Data Setup

HORAYZON requires EGM96 geoid data for ellipsoidal height correction. By default,
HORAYZON attempts to download this data automatically from an external server.
To avoid network blocking (e.g. since we donaloda it quite often), copy the
data manually into your local assets folder before running the pipeline.

**Required file structure:**
```
local_assets/
└── EGM/
    └── EGM96/
        └── WW15MGH.GRD
```

Copy the EGM96 data into the `local_assets/EGM/EGM96/` folder, then register
the path with HORAYZON by writing it to its configuration file. Run once in
a command prompt (adapt the path to your installation):

```dos
echo local_assets\EGM\ > "%VIRTUAL_ENV%\Lib\site-packages\horayzon\path_aux_data.txt"
```

Example:
```dos
echo local_assets\EGM\ > "D:\temp\github\topo-satromo-v2\.venv\Lib\site-packages\horayzon\path_aux_data.txt"
```

Verify the content:
```dos
type "%VIRTUAL_ENV%\Lib\site-packages\horayzon\path_aux_data.txt"
```

> **Note:** This step is required only once per virtual environment. The path is
> stored permanently in `path_aux_data.txt` inside the HORAYZON package directory.
> If you recreate the virtual environment or move the project, repeat this step.

### 3. WIN / Linux / macOS

LINUX macOS
```bash
python -m venv venv
source venv/bin/activate 
pip install -r requirements.txt
```
WIN
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```


---

### 4. Secrets Setup

**secrets** (for int):

Create a `secrets/` folder in the project root containing (make sure you add this folder to .gitignore):

The exact filenames are defined in the config you run — see `FSDI_SECRETS`,
`S3_SECRETS` and `COPERNICUS_SECRETS` at the top of `configuration/dev_config.py`
and `configuration/prod_config.py`:

| Purpose | `dev_config.py` | `prod_config.py` |
|---------|-----------------|------------------|
| FSDI STAC API credentials | `stac_fsdi-int.json` | `stac_fsdi-prod.json` |
| AWS S3 credentials | `s3_int.json` | `s3_prod.json` |
| Copernicus Open EO credentials | `copernicus_oed.json` | `copernicus_reprocess.json` |
| *(Windows only)* Pre-compiled GDAL wheel | `gdal-X.X.X-cpXXX-win_amd64.whl` | — |




**GitHub Actions secrets** (for PROD):

| Secret name | Content |
|-------------|---------|
| `FSDI_STAC_USER` | FSDI STAC API user |
| `FSDI_STAC_PASSWORD` | FSDI STAC API password |
| `S3_SECRETS` | Contents of the AWS S3 credentials JSON |
| `COPERNICUS_S3_SECRETS` | Contents of the Copernicus S3 credentials JSON |
| `AWS_ACCESS_KEY_ID` | AWS credentials used to start/stop the EC2 runner |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials used to start/stop the EC2 runner |
| `GH_PAT` | Personal access token for the `machulav/ec2-github-runner` action |

**GitHub Actions variables** (for PROD, repository *Variables*, not secrets):

| Variable name | Content |
|---------------|---------|
| `AWS_REGION` | e.g. `eu-central-1` |
| `EC2_AMI_ID` | Pre-configured AMI with GDAL, the venv and `local_assets/` |
| `EC2_INSTANCE_TYPE` | e.g. `g4dn.xlarge` (GPU instance) |
| `EC2_SUBNET_ID` | VPC subnet for the runner |
| `EC2_SECURITY_GROUP_ID` | Security group allowing GitHub Actions traffic |

---

## Configuration

All configuration files live in the `configuration/` folder. The default is `dev_config.py`.

To use a different config:

```bash
python satromo_processor.py my_config.py
# or with a date:
python satromo_processor.py my_config.py 2024-06-12
# or with a date and enforce overwrite:
python satromo_processor.py my_config.py 2024-06-12 --force
```


### Adding a new product

1. Add a new entry under `# A) PRODUCTS, INDICES` in your config:

```python
PRODUCT_MY_NEW = {
    "image_collection": "...",
    "temporal_coverage": 1,
    "product_name": "ch.swisstopo.swisseo_my_product",
    "step0_collection": "https://...",  # STAC URL or s3://...
}
```

2. Register it in `step0`. The `step0_function` is the callable that generates the
   data for a single date; for S2-SR this is the step1 processor itself:

```python
step0 = {
    "https://.../my_collection": {
        "step0_function": "step1_processor_my_new.process_product_my_new"
    }
}
```

3. In `satromo_processor.py`, import and call your step1 function:

```python
from step1_processors import step1_processor_my_new

elif product_to_be_processed == 'PRODUCT_MY_NEW':
    result = step1_processor_my_new.process(current_date_str, collection_ready)
```

---

## Usage

### Normal Run

Runs for today's date  using `dev_config.py`:

```bash
python satromo_processor.py
```

### Processing a Specific Date

```bash
python satromo_processor.py dev_config.py 2024-06-12
```

### Force Reprocessing

Use `--force` (or `-force`) to reprocess a date that already has STAC items:

```bash
python satromo_processor.py dev_config.py 2024-06-12 --force
```

### Debug Mode

When no date argument is supplied, the script enters debug mode and uses a hardcoded date. Edit `satromo_processor.py` to change it:

```python
if debug_mode:
    current_date_str = "2016-04-28"
    force_reprocess = True   # <-- toggle manually
```

### Manual Reprocessing Workflow (local → PROD)

For dates that failed in production:

```bat
REM 1. Activate venv
venv\Scripts\activate

REM 2. Run processor for the target date
python satromo_processor.py prod_config.py 2024-06-12


```

Before reprocessing, also:
- Delete affected GEE assets from the `step0_collection`
- Remove the date entry from `tools/step0_empty_assets.csv` if present

---

## Production Runtime (GitHub Actions + EC2)

Production runs from a single workflow, `.github/workflows/run_rerun.yml`
("Run S2 Processor"). It is deliberately split into a cheap availability check on
a GitHub-hosted runner and an expensive GPU stage on a temporary AWS EC2 runner
that is started only when there is something to process and always stopped
afterwards.

### Trigger: external hourly scheduler (cron-job.org)

The workflow exposes only `workflow_dispatch`; the `schedule:` block in the YAML is
intentionally commented out. GitHub Actions cron does not fire reliably on the
hour, so the hourly trigger comes from an **external scheduler at
[https://cron-job.org/](https://cron-job.org/)**, which calls the GitHub API to
dispatch the workflow once per hour.

Background: Sentinel-2 often delivers two passes/orbits per day and both are
usually incomplete on first arrival, so the pipeline polls hourly rather than once
per day. The relevant processing window is roughly 23 UTC through 11 UTC.

A `concurrency` group (`repo-actions`) ensures runs do not overlap — a new trigger
queues behind the running one instead of cancelling it, because every stage
commits `tools/` back to `main`.

### Stages

| Stage | Job | Runner | What it does |
|-------|-----|--------|--------------|
| 1 | `check-and-update` | `ubuntu-24.04` (hosted) | `main_functions/util_get_acquisition_plans.py`, then `python check_s2_sr.py $(date +%Y-%m-%d) 30 prod_config.py`; publishes `acquisitionplan.csv` / `step0_empty_assets.csv` as collection assets and pushes `tools/` back to `main`. Sets the output `needs-processing`. |
| 2a | `start-ec2` | `ubuntu-24.04` (hosted) | Only if `needs-processing == 'true'`: starts a GPU EC2 self-hosted runner via `machulav/ec2-github-runner`. |
| 2b | `run-processing` | EC2 GPU runner | Symlinks `/home/ubuntu/local_assets`, activates `/home/ubuntu/.venv`, installs `requirements.txt`, runs `python rerun.py prod_config.py`, republishes the CSVs and pushes `tools/`. Hard timeout **420 minutes** for cost protection. |
| 2c | `stop-ec2` | `ubuntu-24.04` (hosted) | `if: always()` — terminates the EC2 runner even when stage 2b failed or timed out. |

### The two production scripts

- **`check_s2_sr.py`** — lightweight Copernicus STAC availability check. It queries
  the Copernicus STAC API for Switzerland and updates `tools/step0_empty_assets.csv`,
  marking dates as `Tiles ready awaiting GPU system run`. Besides the target date it
  re-checks historical CSV entries within the lookback window whose remark is
  `No candidate scene`, `Tile upload incomplete: [...]` or `Tile download incomplete`.
  Usage: `python check_s2_sr.py [YYYY-MM-DD] [days_back] [config.py]` (defaults:
  today, 30 days, `dev_config.py`).

- **`rerun.py`** — batch reprocessing driver for the GPU stage. It picks up all
  queued entries from `tools/step0_empty_assets.csv` (30 days back) and runs the
  full pipeline per date. Usage: `python rerun.py [config.py]` (default
  `dev_config.py`). It backs the CSV up to `.bak` and restores it on a fatal error,
  and exits non-zero only when its circuit breaker trips after consecutive upstream
  failures — individual failed dates simply stay queued for the next run.

`tools/step0_empty_assets.csv` is the live state file that couples the two stages;
CI commits it to `main` after every run, so avoid editing it by hand.

---

## Products

| Config key | Product name | Description |
|------------|-------------|-------------|
| `PRODUCT_S2_LEVEL_2A` | `ch.swisstopo.swisseo_s2-sr_v200` | Sentinel-2 L2A ARD — co-registered, cloud masked, all bands |
| `PRODUCT_VHI` | `ch.swisstopo.swisseo_vhi_v200` | Vegetation Health Index (drought stress) |


Sentinel-2 band groups:

| Resolution | Bands |
|-----------|-------|
| 10 m | B02 (Blue), B03 (Green), B04 (Red), B08 (NIR), CLOUDMASK |
| 20 m | B05–B07, B8A, B11, B12, SCL |
| 60 m | B01, B09, AOT |

---

## Roadmap

- [x] Sentinel-2 L2A ARD (co-registered, cloud/shadow masked)
- [x] STAC catalog integration (step0 + publish)
- [x] Co-registration via AROSICS
- [x] `--force` CLI flag for reprocessing
- [x] S3 + FSDI STAC publishing
- [x] Vegetation Health Index (VHI) — implemented, not yet operational
- [ ] NDVI anomalies (N1, N2)
- [ ] NDMI anomalies (M1)
- [ ] NBR natural disturbance index (B2)

---

## Contributing

Contributions are welcome! Please:

1. Fork the project
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m 'Add my feature'`
4. Push: `git push origin feature/my-feature`
5. Open a Pull Request

---

## License

Distributed under the BSD-3-Clause License. See `LICENSE.txt` for details.

---

## Credits

Special thanks to the developers and maintainers of the open-source projects that made this work possible:

* **[AROSICS](https://github.com/GFZ/arosics)**: An Automated and Robust Open-Source Image Co-Registration Software developed by the GFZ German Research Centre for Geosciences. We utilize AROSICS specifically for aligning our raw satellite datasets.
* **[OmniCloudMask](https://github.com/DPIRD-DMA/OmniCloudMask)**: An open-source tool developed by DPIRD-DMA. We utilize OmniCloudMask for  generating 
robust cloud and shadow masks for our raw satellite imagery.
* **[HORAYZON](https://github.com/ChristianSteger/HORAYZON)**: An open-source terrain analysis and horizon computation tool developed by Christian Steger. We utilize HORAYZON for calculating terrain incindence angles and topographic induced shadows to support accurate modelling of terrain-related effects in our geospatial analyses.


---

## Contact

David Oesch — david.oesch[at]swisstopo.ch  
Joan Sturm — joan.sturm[at]swisstopo.ch

Project: [github.com/swisstopo/topo-satromo-v2](https://github.com/swisstopo/topo-satromo-v2)





