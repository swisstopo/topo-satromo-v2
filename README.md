# topo-satromo-v2
Erdbeobachtungs-Satellitendaten fürs Trockenheitsmonitoring (SATROMO) Version 2


# CloudScore+ Export / Upload

## Processor 

Adapt the config to your needs and then run in a command prompt (`cmd`) to start the export for several months:
```
main_functions\batch_csplus_processor.bat prod_config.py 2024 01 && main_functions\batch_csplus_processor.bat prod_config.py 2024 02 && main_functions\batch_csplus_processor.bat prod_config.py 2024 03
```

If you need to run a single day use the following command:

```
main_functions\batch_csplus_single.bat prod_config.py 2017-12-20
```

## Publisher

Parallel to the processor, you can run the "publisher" in a separate `cmd` window:

```
main_functions\batch_csplus_publish_loop.bat prod_ebp_config.py
```

This process runs in a loop as long as there are entries in `processing/running_tasks.csv`. The publisher runs every 30min.
