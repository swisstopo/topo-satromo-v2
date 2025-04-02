# -*- coding: utf-8 -*-
import os
import ee
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import configuration as config
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
                        # Delete file from GCS after successful download
                        blob.delete()
                        print(f"Deleted {tif_file} from GCS.")


                    except Exception as e:
                        print(f"Error processing {tif_file}: {e}")
                else:
                    print(f"Task {task_id} ({filename}) not yet ready.......")
            except Exception as e:
                print(f"Error checking task {task_id}: {e}")

    # Delete files older than 25 days from GCS bucket
    print("Cleaning up old files from GCS bucket...")
    try:
        import datetime

        # Get current time
        now = datetime.datetime.now(datetime.timezone.utc)

        # 25 days ago
        cutoff_time = now - datetime.timedelta(days=25)

        # List all blobs in the bucket
        bucket = gcs_client.bucket(config.GCLOUD_BUCKET)
        blobs = bucket.list_blobs()

        # Counter for deleted files
        deleted_count = 0

        # Check each blob's age
        for blob in blobs:
            # Skip if blob doesn't have a time_created (shouldn't happen, but as precaution)
            if not blob.time_created:
                continue

            # Convert to UTC for proper comparison
            blob_time = blob.time_created.replace(tzinfo=datetime.timezone.utc)

            # Delete if older than cutoff
            if blob_time < cutoff_time:
                blob_name = blob.name
                blob.delete()
                deleted_count += 1
                print(f"Deleted old file: {blob_name} (created on {blob_time.strftime('%Y-%m-%d')})")

        print(f"Cleanup complete. Removed {deleted_count} files older than 25 days.")
    except Exception as e:
        print(f"Error during GCS cleanup: {e}")

    print("PUBLISH Process done.")
