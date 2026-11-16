# Shows how far step 4 is: the current file, the scene progress, the time per file and the files on S3.
# Usage: python util/ref_ndvi_landsat/check_progress.py [LOG_FILE]
# The default log file is util/ref_ndvi_landsat/output/step4_full_doy.log, for the M and MM run use
#        python util/ref_ndvi_landsat/check_progress.py util/ref_ndvi_landsat/output/step4_full_m_mm.log

import json
import os
import re
import sys
from datetime import datetime

import boto3

import vi_reference_config as config

log_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(config.OUTPUT_DIR, "step4_full_doy.log")
lines = open(log_file, encoding="utf-8", errors="replace").read().splitlines()

# Current file: the last line like "=== NDVI_Stats_DOY005.tif (5 of 366), slots ..."
current = None
for line in lines:
    match = re.match(r"=== (\S+) \((\d+) of (\d+)\)", line)
    if match:
        current = match
if current is None:
    print("No file started yet")
else:
    print(f"Current file: {current.group(1)}, {current.group(2)} of {current.group(3)}")

# Scene progress inside the current file: the last line like "  [17/197] LT05_..."
for line in reversed(lines):
    match = re.match(r"\s+\[(\d+)/(\d+)\]", line)
    if match:
        print(f"Scenes of the current file: {match.group(1)} of {match.group(2)}")
        break
    if line.startswith("==="):
        break

# Time per finished file
minutes = [int(m.group(1)) for m in (re.match(r"Done in (\d+) min", line) for line in lines) if m]
skipped = sum(1 for line in lines if line.startswith("Skipped:"))
print(f"Finished in this run: {len(minutes)} files, skipped (already on S3): {skipped}")
if minutes:
    # The first file of a run processes the whole window, so it is left out of the estimate if possible
    recent = minutes[1:][-10:] if len(minutes) > 1 else minutes
    print(f"Minutes per file: first {minutes[0]}, last {minutes[-1]}, mean of the last {len(recent)} (without the first): {sum(recent) / len(recent):.0f}")
    if current is not None:
        remaining = int(current.group(3)) - int(current.group(2))
        hours = remaining * sum(recent) / len(recent) / 60
        print(f"Estimated time left: {hours:.0f} hours")

failures = [line for line in lines if "failed" in line.lower() or "Traceback" in line]
print(f"Lines with 'failed' or 'Traceback': {len(failures)}")
for line in failures[-5:]:
    print("  " + line.strip())
print(f"Log last written: {datetime.fromtimestamp(os.path.getmtime(log_file)):%Y-%m-%d %H:%M}")

# Files on S3
with open(config.S3_SECRETS_FILE) as file:
    secrets = json.load(file)
s3 = boto3.client("s3", aws_access_key_id=secrets["aws_access_key_id"],
                  aws_secret_access_key=secrets["aws_secret_access_key"], region_name=secrets["aws_region_name"])
for time_step, prefix in config.S3_FOLDERS.items():
    names = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=config.S3_BUCKET, Prefix=prefix):
        names += [item["Key"][len(prefix):] for item in page.get("Contents", []) if item["Key"].endswith(".tif")]
    print(f"Files on S3 for {time_step} in {prefix}: {len(names)}" + (f", last: {sorted(names)[-1]}" if names else ""))
