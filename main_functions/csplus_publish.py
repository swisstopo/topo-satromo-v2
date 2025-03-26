# -*- coding: utf-8 -*-
from pydrive.auth import GoogleAuth
from oauth2client.service_account import ServiceAccountCredentials
import boto3
import json
import os
import ee
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import configuration as config
import platform
from google.cloud import storage
from main_functions import main_utils


if __name__ == "__main__":

    # Test if we are on Local DEV Run or if we are on PROD
    main_utils.determine_run_type()

    # Authenticate with GEE
    main_utils.initialize_gee()

    # Google Cloud Storage client

    gcs_client = main_utils.storage_client


    # Extract S3 path from config
    s3_path = config.PRODUCT_S2_LEVEL_CSPLUS['step0_collection']  # Example: 's3://satromoint/data/CLOUD_SCORE_PLUS'


    # Read the status file
    with open(config.GEE_RUNNING_TASKS, "r") as f:
        lines = f.readlines()

    # Process each line
    for line in lines[1:]:  # Skip header
        task_id, filename = line.strip().split(",")
        # Check if filename is a cloudscoreplus export
        if not len(filename.split("_")) != 6:
            print(filename + " :not a cloudscoreplus export, skipping...")
        else:
            try:
                # Check task status
                task_status = ee.data.getTaskStatus(task_id)[0]

                tif_file = f"{filename}.tif"
                gcs_blob_path = tif_file
                local_tmp_file = tif_file

                # Remove 's3://<bucket>/' prefix to get only the object key
                s3_key_path = s3_path.replace(f"s3://{config.S3_BUCKET_NAME}/", "").rstrip("/")

                # Append filename
                s3_key = os.path.join(s3_key_path, tif_file).replace("\\", "/")

                # If task is not completed, print "done"
                if task_status["state"] == "COMPLETED":
                    print(f"Task {task_id} ({filename}) is completed -> done")

                    try:
                        # Download from GCS
                        bucket = gcs_client.bucket(config.GCLOUD_BUCKET)
                        blob = bucket.blob(gcs_blob_path)
                        blob.download_to_filename(local_tmp_file)
                        print(f"Downloaded {tif_file} from GCS.")

                        # Upload to S3
                        main_utils.s3.upload_file(local_tmp_file, config.S3_BUCKET_NAME, s3_key)
                        s3_key = os.path.join(s3_key_path, filename+"_metadata.json").replace("\\", "/")
                        main_utils.s3.upload_file(os.path.join(config.PROCESSING_DIR,filename+"_metadata.json"), config.S3_BUCKET_NAME, s3_key)
                        print(f"Uploaded {tif_file} and JSON to S3.")

                        # Cleanup local file
                        os.remove(local_tmp_file)
                        os.remove(os.path.join(config.PROCESSING_DIR,filename+"_metadata.json"))

                        # Remove from  Processing Tasks
                        with open(config.GEE_RUNNING_TASKS, "r", encoding="utf-8") as f:
                            lines = f.readlines()

                        # Filter out lines that contain the filename
                        updated_lines = [line for line in lines if filename not in line]

                        # Overwrite the file with filtered content
                        with open(config.GEE_RUNNING_TASKS, "w", encoding="utf-8") as f:
                            f.writelines(updated_lines)


                    except Exception as e:
                        print(f"Error processing {tif_file}: {e}")
                else:
                    print(f"Task {task_id} ({filename}) not yet ready.......")
            except Exception as e:
                print(f"Error checking task {task_id}: {e}")

    print("PUBLISH Process done.")
