for month in 1 2 3
do
    for day in {01..31}
    do
        # Run the processor
        python3 satromo_processor.py prod_config.py 2026-0${month}-${day}

        # Clean up directories and specific file types
        rm -rf temp sentinel-2-l2a
        rm -f *.tif *.pickle
    done
done