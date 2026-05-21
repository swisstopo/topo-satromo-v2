# -*- coding: utf-8 -*-
import datetime

import configuration as config
from step0_functions import get_step0_dict, step0_main
from step1_processors import step1_processor_s2_sr, step1_processor_vhi #, step1_processor_l57_toa, step1_processor_l89_sr, step1_processor_l89_toa, step1_processor_s3_toa, step1_processor_vhi_hist
from main_functions import main_utils



if __name__ == "__main__":
    # Test if we are on Local DEV Run or if we are on PROD
    main_utils.determine_run_type()

    # Authenticate with GEE
    main_utils.initialize_gee()

    # Get current date
    current_date_str = datetime.datetime.today().strftime('%Y-%m-%d')

    # Get the current date
    current_date = datetime.datetime.today()

    # Subtract X day back from the current date to procoess not todays but the  date in the past: This is to overcome the delay
    delay = 0  # in days
    previous_date = current_date - datetime.timedelta(days=delay)

    # Convert the previous date to a string in the format 'YYYY-MM-DD' and set it to current date
    current_date_str = previous_date.strftime('%Y-%m-%d')

    # Check for command line argument (highest priority)
    from configuration import arg_date_str , arg_force
    if arg_date_str:
        current_date_str = arg_date_str
        print(f'Using command line date: {arg_date_str}')
        debug_mode = False
        force_reprocess = arg_force  # use CLI flag
        if force_reprocess:
            print("Force reprocess enabled via CLI flag")
    else:
        # Enable debug mode if no command line argument is given
        debug_mode = True
        force_reprocess = False  # set default here

    # Check for debug override (second priority)
    if debug_mode:
        current_date_str = "2026-02-02"
        force_reprocess = True  # <-- toggle this manually during debug
        print("*****************************")
        print("Using manually set date:", current_date_str)
        print(f"Force reprocess: {force_reprocess}")
        print("*****************************")


    # Define date to be used
    #current_date = ee.Date(current_date_str)

    #roi = ee.Geometry.Rectangle(config.ROI_RECTANGLE)

    # Retrieve the step0 information from the config object and store it in a dictionary
    step0_product_dict = get_step0_dict()
    # Print the dictionary containing collection names and their details
    print(step0_product_dict)

    # Process the step0 collections to determine which ones are ready for processing
    collections_ready_for_processors = step0_main(
        step0_product_dict, current_date_str)
    # Print the list of collections that are ready for processing

    print(collections_ready_for_processors)

    for collection_ready in collections_ready_for_processors:
        print('Collection ready: {}'.format(collection_ready))

        for product_to_be_processed in step0_product_dict[collection_ready][0]:
            print('Launching product {}'.format(product_to_be_processed))

            if product_to_be_processed == 'PRODUCT_S2_LEVEL_CSPLUS':  #
                result = "PRODUCT_S2_LEVEL_CSPLUS:  step0 only"

            elif product_to_be_processed == 'PRODUCT_S2_LEVEL_2A':
                # Check if STAC items already exist for the given date, against the step0_collection
                api_path = getattr(config, 'STAC_FSDI_API')
                collection = getattr(config, 'PRODUCT_S2_LEVEL_2A')['step0_collection']
                stac_catalog_url, collection_id = main_utils.extract_collection_id_from_url(collection, api_path)
                daily_items = main_utils.get_stac_items_for_date(
                    stac_catalog_url, collection_id,
                    datetime.datetime.strptime(current_date_str, "%Y-%m-%d").date()
                )
                if len(daily_items) == 0 or force_reprocess:  # add force_reprocess
                    if force_reprocess and len(daily_items) > 0:
                        print(f"Force reprocess enabled: reprocessing {current_date_str} despite existing STAC items.")
                    result = step1_processor_s2_sr.process_product_s2_sr(
                        current_date_str, collection_ready)
                else:
                    print(f"STAC items already exist for date {current_date_str}: skipping processing.")
                    result = f"PRODUCT_S2_LEVEL_2A: STAC items already exist for date {current_date_str}, skipping processing."

            elif product_to_be_processed == 'PRODUCT_VHI':
                roi = None # Default for operational mode
                # For testing, we can set a specific ROI --> roi = (b.left, b.bottom, b.right, b.top) 
                # roi = (2681000, 1230100, 2687500, 1237900) # Sihlwald
                # roi = (2743000, 1224000, 2748900, 1229500) # Wildhaus (Orbitgrenze)
                # roi = (2596300, 1166700, 2674400, 1222700) # Emmental
                # roi = (2573000, 1199600, 2583100, 1208400) # Kerzersmoos
                # roi = (2534500, 1194100, 2550700, 1203300) # Val de Travers
                # roi = (2602200, 1163300, 2630100, 1181100) # Niesen/Thun
                roi = (2549000, 1159000, 2643000, 1213000) # Mittelland BE/FR ca. 5'000 km2

                # Does the OUTPUT (VHI) already exist? If yes, we can skip processing (unless force_reprocess is enabled)
                api_path = getattr(config, 'STAC_FSDI_API')
                collection = getattr(config, 'PRODUCT_VHI')['step1_collection']
                stac_catalog_url, collection_id = main_utils.extract_collection_id_from_url(collection, api_path)
                daily_items = main_utils.get_stac_items_for_date(
                    stac_catalog_url, collection_id,
                    datetime.datetime.strptime(current_date_str, "%Y-%m-%d").date()
                )
                if len(daily_items) == 0:
                    print(f"VHI: No existing VHI STAC items for {current_date_str}, processing VHI.")
                    result = step1_processor_vhi.process_product_vhi(
                    current_date_str, collection_ready, roi)
                elif force_reprocess:
                    print(f"Force reprocess enabled: reprocessing {current_date_str} despite existing STAC items.")
                    result = step1_processor_vhi.process_product_vhi(
                        current_date_str, collection_ready, roi)
                else:
                    print(f"STAC items already exist for date {current_date_str}: skipping processing.")
                    result = f"VHI: STAC items already exist for date {current_date_str}, skipping processing."

            # elif product_to_be_processed == 'PRODUCT_VHI_HIST':
            #     result = step1_processor_vhi_hist.process_PRODUCT_VHI_HIST(
            #         roi, current_date_str)

            elif product_to_be_processed == 'PRODUCT_MSG_CLIMA':
                result = "PRODUCT_MSG_CLIMA:  step0 only"

            elif product_to_be_processed == 'PRODUCT_MSG':
                result = "PRODUCT_MSG:  step0 only"

            else:
                raise BrokenPipeError('Inconsitent configuration')

            # print("Result:", result)

print("Processing done!")
