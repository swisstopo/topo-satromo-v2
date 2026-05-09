#!/bin/bash

# Cloud-free mosaic batch processing

set -e

python main_functions/main_cloudfree_mosaic.py --date 2025-07-01 --days 15 --sort valid_data --method first
python main_functions/main_cloudfree_mosaic.py --date 2025-07-01 --days 15 --sort valid_data --method mean
python main_functions/main_cloudfree_mosaic.py --date 2025-07-01 --days 15 --sort newest --method first
python main_functions/main_cloudfree_mosaic.py --date 2025-07-01 --days 15 --sort newest --method mean

python main_functions/main_cloudfree_mosaic.py --date 2025-03-06 --days 15 --sort valid_data --method first
python main_functions/main_cloudfree_mosaic.py --date 2025-03-06 --days 15 --sort valid_data --method mean
python main_functions/main_cloudfree_mosaic.py --date 2025-03-06 --days 15 --sort newest --method first
python main_functions/main_cloudfree_mosaic.py --date 2025-03-06 --days 15 --sort newest --method mean

python main_functions/main_cloudfree_mosaic.py --date 2025-03-06 --days 30 --sort valid_data --method first
python main_functions/main_cloudfree_mosaic.py --date 2025-03-06 --days 30 --sort valid_data --method mean
python main_functions/main_cloudfree_mosaic.py --date 2025-03-06 --days 30 --sort newest --method first
python main_functions/main_cloudfree_mosaic.py --date 2025-03-06 --days 30 --sort newest --method mean

echo "Alle Prozesse erfolgreich abgeschlossen."
