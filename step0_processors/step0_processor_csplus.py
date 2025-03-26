import ee
import numpy as np
from main_functions import main_utils
from .step0_utils import write_asset_as_empty
import os
import json
import configuration as config
import csv

# Pre-processing pipeline for daily Cloudscore Plus (cs+) mosaics over Switzerland

##############################
# INTRODUCTION
# This script provides a tool to preprocess Cloudscore Plus (cs+) data over Switzerland.
# It can mask clouds and cloud shadows, detect terrain shadows, mosaic images from the same image swath,
# co-register images to the Sentinel-2 Global Reference Image, and export the results.
#

##############################
# CONTENT
# The switches enable / disable the execution of individual steps in this script

# This script includes the following steps:
# 1. Masking clouds and cloud shadows
# 2. Detecting terrain shadows
# 3. Mosaicing of images from the same day (=same orbital track) over Switzerland
# 4. Registering the S2 Mosaic to the Sentinel-2 global reference image
# 5. Exporting spectral bands, additional layers and relevant properties
#
# The script is set up to export one mosaic image per day.



def generate_csplus_mosaic_for_single_date(day_to_process: str, collection: str, task_description: str) -> None:
    ##############################
    # SWITCHES
    # The switches enable / disable the execution of individual steps in this script

    # options: True, False - defines if the CloudScore+ dataset should be used (if False': s2cloudless)
    cloudScorePlus = True


    ##############################
    # TIME
    # define a date or use the current date: ee.Date(Date.now())
    start_date = ee.Date(day_to_process)
    end_date = ee.Date(day_to_process).advance(1, 'day')

    ##############################
    # SPACE

    # Official swisstopo boundaries
    # source: https:#www.swisstopo.admin.ch/de/geodata/landscape/boundaries3d.html#download
    # processing: reprojected in QGIS to epsg32632
    aoi_CH = ee.FeatureCollection(
        "projects/satromo-prod/assets/res/swissBOUNDARIES3D_1_5_TLM_LANDESGEBIET_dissolve_epsg32632").geometry()

    ##############################
    # SATELLITE DATA
    # MULTIPLE ORBITS per day: For 2025 starting in March, ESA runs S2A and S2C in parallel resulting in multiple orbits per day

    # Sentinel-2
    S2_sr_orbits= ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
        .filter(ee.Filter.bounds(aoi_CH)) \
        .filter(ee.Filter.date(start_date, end_date))

    # unique SENSING_ORBIT_NUMBER
    unique_orbits = S2_sr_orbits.aggregate_array('SENSING_ORBIT_NUMBER') \
        .distinct() \
        .getInfo()

    # S2 CloudScore+
    S2_csp = ee.ImageCollection('GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED') \
        .filter(ee.Filter.bounds(aoi_CH)) \
        .filter(ee.Filter.date(start_date, end_date))

    # S2cloudless
    S2_clouds = ee.ImageCollection('COPERNICUS/S2_CLOUD_PROBABILITY') \
        .filter(ee.Filter.bounds(aoi_CH)) \
        .filter(ee.Filter.date(start_date, end_date))

    # List to store task IDs
    task_ids = []

    # Check if we have data at all
    if len(unique_orbits) == 0:
        write_asset_as_empty(collection, day_to_process, 'No candidate scene')
        return

    # Loop over all orbits
    for orbit in unique_orbits:

        # Print if unique_orbit has more than 1 element
        if len(unique_orbits) > 1:
            print(f"Processing orbit: {orbit} of {day_to_process}")


        # Sentinel-2
        S2_sr = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filter(ee.Filter.bounds(aoi_CH)) \
            .filter(ee.Filter.date(start_date, end_date)) \
            .filter(ee.Filter.eq('SENSING_ORBIT_NUMBER', orbit)) \
            .linkCollection(S2_csp, ['cs', 'cs_cdf']) \
            .linkCollection(S2_clouds, ['probability'])

        # Is a scene available for this date -> Yes: continue / No: abort ('No candidate scene')
        image_list_size = S2_sr.size().getInfo()

        if image_list_size == 0:
            write_asset_as_empty(collection, day_to_process, 'No candidate scene')
            return

        # Are all tiles for the overpass available -> Yes: continue / No: abort ('Tile upload incomplete')
        SENSING_ORBIT_NUMBER = S2_sr.first().get('SENSING_ORBIT_NUMBER').getInfo()
        if image_list_size < 4 and SENSING_ORBIT_NUMBER == 8:
            write_asset_as_empty(collection, day_to_process,
                                'Tile upload incomplete')
            return  # exit if condition met
        if image_list_size < 11 and SENSING_ORBIT_NUMBER == 108:
            write_asset_as_empty(collection, day_to_process,
                                'Tile upload incomplete')
            return
        if image_list_size < 11 and SENSING_ORBIT_NUMBER == 65:
            write_asset_as_empty(collection, day_to_process,
                                'Tile upload incomplete')
            return
        if image_list_size < 4 and SENSING_ORBIT_NUMBER == 22:
            write_asset_as_empty(collection, day_to_process,
                                'Tile upload incomplete')
            return

        # Get image_list_size for the cloud probability dataset
        if cloudScorePlus is True:
            image_list_size_cloud = S2_sr.select('cs').size().getInfo()
        else:
            image_list_size_cloud = S2_sr.select('probability').size().getInfo()

        # Are CloudScore+ datasets for all tiles available -> Yes: continue / No: abort ('Cloud probability data missing')
        if image_list_size_cloud < 4 and SENSING_ORBIT_NUMBER == 8:
            write_asset_as_empty(collection, day_to_process,
                                'Cloud probability data missing')
            return
        if image_list_size_cloud < 11 and SENSING_ORBIT_NUMBER == 108:
            write_asset_as_empty(collection, day_to_process,
                                'Cloud probability data missing')
            return
        if image_list_size_cloud < 11 and SENSING_ORBIT_NUMBER == 65:
            write_asset_as_empty(collection, day_to_process,
                                'Cloud probability data missing')
            return
        if image_list_size_cloud < 4 and SENSING_ORBIT_NUMBER == 22:
            write_asset_as_empty(collection, day_to_process,
                                'Cloud probability data missing')
            return


        # Get the list of all images in the collection
        #image_list = S2_csp.toList(image_list_size_cloud)
        # Get the list of linked S2_csp images with their full properties
        image_list = S2_sr.select('cs').map(lambda image: image.copyProperties(S2_csp.filter(ee.Filter.eq('system:index', image.get('system:index'))).first())).toList(image_list_size_cloud)


        # Loop through each image and export it
        for i in range(image_list_size_cloud):
            image = ee.Image(image_list.get(i))

            # Get the SOURCE_PRODUCT_ID
            source_product_id = image.get('SOURCE_PRODUCT_ID').getInfo()

            # Scale and convert bands to uint8
            scaled_image = image.multiply(100).toUint8()

            #  Configure export task
            task = ee.batch.Export.image.toCloudStorage(
                image=scaled_image,
                description=source_product_id,
                #scale=scale,
                #region=region,
                fileNamePrefix=source_product_id,
                maxPixels=1e13,
                #crs=crs,
                fileFormat="GeoTIFF",
                bucket=config.GCLOUD_BUCKET
            )

            # Start the export task
            task.start()

            # Get Task ID
            task_id = task.status()["id"]

            # Add task_id to task_ids list
            task_ids.append(task_id)

            # Save Task ID and filename to a text file
            header = ["Task ID", "Filename"]
            data = [task_id, source_product_id]

            # Check if the file already exists
            file_exists = os.path.isfile(config.GEE_RUNNING_TASKS)

            with open(config.GEE_RUNNING_TASKS, "a", newline="") as f:
                writer = csv.writer(f)

                # Write the header if the file is newly created
                if not file_exists:
                    writer.writerow(header)

                # Write the data
                writer.writerow(data)

            print(f"Started export task {i+1}/{image_list_size_cloud} for image with SOURCE_PRODUCT_ID: {source_product_id}")
            print(f"  Task ID: {task.id}")
            print(f"  Exporting to: {config.GCLOUD_BUCKET}/{source_product_id}.tif")


            # Adding extracting image info
            image_info = ee.Image(image).getInfo()

            # Convert keys to uppercase and add prefix
            image_info_gee = {"GEE_" + key.upper(): value for key,
                            value in image_info.items()}

            # Add swisstopo_data to image_info_gee
            # image_info_gee["SWISSTOPO"] = swisstopo_data

            # Export the dictionary as JSON
            with open(os.path.join(config.PROCESSING_DIR, source_product_id + "_metadata.json"), 'w') as json_file:
                json.dump(image_info_gee, json_file)

        print(f"\nStarted {len(task_ids)} export tasks")

