@echo off
setlocal EnableDelayedExpansion

set "config_file=%~1"
set "year_month=%~2"


if "!config_file!"=="" (
    echo Bitte Konfigurationsfile und Datum als Parameter eingeben.
    echo Beispiel: batch_csplus_single.bat oed_prod_config.py 2024-04-02
    goto :eof
)

if "!year_month!"=="" (
    echo Bitte Jahr-Monat-Tag als Parameter eingeben.
    goto :eof
)


python satromo_processor.py !config_file! !year_month!