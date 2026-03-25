@echo off
setlocal EnableDelayedExpansion

set "config_file=%~1"

:publish_loop
echo Start Publisher <nul
python main_functions/csplus_publish.py !config_file! 
echo %TIME% waiting 30min
ping -n 1800 localhost >nul

for /f %%A in ('find /c /v "" ^< processing/running_tasks.csv') do set "lines=%%A"
if !lines! GTR 1 goto publish_loop

echo *********************************************
echo Publish Loop abgeschlossen
echo *********************************************
echo *********************************************<nul