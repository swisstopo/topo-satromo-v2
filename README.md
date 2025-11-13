# topo-satromo-v2
Erdbeobachtungs-Satellitendaten fürs Trockenheitsmonitoring (SATROMO) Version 2
# AROSICS Installation Guide (Windows)

## Prerequisites

- Python 3.11+ (64-bit recommended)
- Virtual environment (recommended)

## Installation Steps

### 1. Create and Activate Virtual Environment

```bash
python -m venv venv
venv\Scripts\activate
```

### 2. Install GDAL (Required Pre-compiled Wheel)

GDAL must be installed **before** installing arosics due to C++ compilation requirements on Windows.

**Download GDAL Wheel:**
1. Visit: https://github.com/cgohlke/geospatial-wheels/releases
2. Download the appropriate GDAL wheel for your Python version:
   - Python 3.11: `GDAL-X.X.X-cp311-cp311-win_amd64.whl`
   - Python 3.12: `GDAL-X.X.X-cp312-cp312-win_amd64.whl`

**Check your Python version:**
```bash
python --version
```

**Install the downloaded wheel:**
```bash
pip install path\to\GDAL-X.X.X-cpXXX-cpXXX-win_amd64.whl
```

### 3. Install Other Requirements

```bash
pip install -r requirements.txt
```

### 4. Install AROSICS

```bash
pip install arosics
```

## Verification

Test the installation:

```bash
python -c "import arosics; print('AROSICS installed successfully!')"
```

## Troubleshooting

- **Error: Microsoft Visual C++ 14.0 required**: You skipped step 2. Install GDAL wheel first.
- **Wrong wheel error**: Make sure the wheel matches your Python version exactly (check with `python --version`)
- **Import errors**: Ensure GDAL is installed before arosics

