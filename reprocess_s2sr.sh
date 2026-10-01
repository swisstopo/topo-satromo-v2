for year in 2024 2023 
do
  for month in {01..12}; do
    # days in month (handles leap years)
    max_day=$(cal "$month" "$year" | awk 'NF {DAYS=$NF} END {print DAYS}')
    for day in $(seq -w 1 "$max_day"); do
      python3 satromo_processor.py dev_config.py "${year}-${month}-${day}"
      rm -rf temp sentinel-2-l2a
      rm -f *.tif *.pickle
    done
  done
done