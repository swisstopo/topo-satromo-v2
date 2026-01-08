# -*- coding: utf-8 -*-
import datetime
import ee
import configuration as config
from step0_functions import get_step0_dict, step0_main
from step1_processors import step1_processor_s2_sr#, step1_processor_l57_toa, step1_processor_l89_sr, step1_processor_l89_toa, step1_processor_s3_toa, step1_processor_vhi, step1_processor_vhi_hist
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
    delay = 3  # in days
    previous_date = current_date - datetime.timedelta(days=delay)

    # Convert the previous date to a string in the format 'YYYY-MM-DD' and set it to current date
    current_date_str = previous_date.strftime('%Y-%m-%d')

    # Check for command line argument (highest priority)
    from configuration import arg_date_str
    if arg_date_str:
        current_date_str = arg_date_str
        print(f'Using command line date: {arg_date_str}')
        debug_mode = False
    else:
        # Enable debug mode if no command line argument is given
        debug_mode = True

    # Check for debug override (second priority)
    if debug_mode:
        current_date_str = "2025-06-10"
        print("*****************************")
        print("Using manually set date:", current_date_str)
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
                # ROI is only taking effect when testing. On prod we will use the clipping as defined in step0_processor_s2_sr
                # border = ee.FeatureCollection(
                #     "USDOS/LSIB_SIMPLE/2017").filter(ee.Filter.eq("country_co", "SZ"))
                # roi = border.geometry().buffer(config.ROI_BORDER_BUFFER)
                # roi = ee.Geometry.Rectangle(
                #     [7.075402, 46.107098, 7.100894, 46.123639])
                # roi = ee.Geometry.Rectangle(
                #     [9.49541, 47.22246, 9.55165, 47.26374,])  # Liechtenstein
                # roi = ee.Geometry.Rectangle(
                #     [8.10, 47.18, 8.20, 47.25])  # 6221 Rickenbach
                # roi = ee.Geometry.Rectangle(
                #     [7.938447, 47.514378, 8.127522, 47.610846])

                # Check if STAC items already exist for the given date, against the step0_collection
                api_path = getattr(config, 'STAC_FSDI_API')
                collection = getattr(config, 'PRODUCT_S2_LEVEL_2A')['step0_collection']
                stac_catalog_url, collection_id = main_utils.extract_collection_id_from_url(collection, api_path)
                daily_items = main_utils.get_stac_items_for_date(stac_catalog_url, collection_id, datetime.datetime.strptime(current_date_str, "%Y-%m-%d").date())
                if len(daily_items) == 0:
                    result = step1_processor_s2_sr.process_product_s2_sr(
                        current_date_str,collection_ready)
                else:
                    print(f"STAC items already exist for date {current_date_str}: skipping processing.")
                    result = f"PRODUCT_S2_LEVEL_2A: STAC items already exist for date {current_date_str}, skipping processing."

            elif product_to_be_processed == 'PRODUCT_VHI':
                roi = ee.Geometry.Rectangle(config.ROI_RECTANGLE)
                # roi = ee.Geometry.Rectangle(
                #     [8.10, 47.18, 8.20, 47.25])  # 6221 Rickenbach
                # roi = ee.Geometry.Rectangle(
                #    [7.81, 46.35, 8.06, 46.46])  # Oberaletschgletscher
                # roi = ee.Geometry.Rectangle(
                #     [7.16, 47.20, 7.27, 47.24])  # Tavannes
                # roi = ee.Geometry.Rectangle(
                #     [8.06, 47.14, 8.72, 47.18])  # Raten ZG/SZ
                result = step1_processor_vhi.process_PRODUCT_VHI(
                    roi, collection_ready, current_date_str)

            elif product_to_be_processed == 'PRODUCT_VHI_HIST':
                roi = ee.Geometry.Rectangle(config.ROI_RECTANGLE)
                # roi = ee.Geometry.Rectangle(
                #     [8.06, 47.14, 8.72, 47.18])  # Raten ZG/SZ
                # roi = ee.Geometry.Rectangle(
                #     [9.41, 46.83, 9.65, 47.02])  # Chur/Landquart
                # roi = ee.Geometry.Rectangle(
                #     [6.40, 46.47, 6.81, 46.61])  # Lausanne VD
                result = step1_processor_vhi_hist.process_PRODUCT_VHI_HIST(
                    roi, current_date_str)



            elif product_to_be_processed == 'PRODUCT_MSG_CLIMA':
                # roi = ee.Geometry.Rectangle(
                #     [9.49541, 47.22246, 9.55165, 47.26374,])  # Liechtenstein
                result = "PRODUCT_MSG_CLIMA:  step0 only"

            elif product_to_be_processed == 'PRODUCT_MSG':
                # roi = ee.Geometry.Rectangle(
                #     [9.49541, 47.22246, 9.55165, 47.26374,])  # Liechtenstein
                result = "PRODUCT_MSG:  step0 only"

            else:
                raise BrokenPipeError('Inconsitent configuration')

            # print("Result:", result)

print("Processing done!")
