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


# Set the CPL_DEBUG environment variable to enable verbose output
# os.environ["CPL_DEBUG"] = "ON"


def determine_run_type():
    """
    Determines the run type based on the existence of the SECRET on the local machine file. And determine platform

    If the file `config.GDRIVE_SECRETS` exists, sets the run type to 2 (DEV) and prints a corresponding message.
    Otherwise, sets the run type to 1 (PROD) and prints a corresponding message.
    """
    global run_type
    global os_name

    # Get the operating system name
    os_name = platform.system()

    # Set SOURCE , DESTINATION and MOUNTPOINTS

    if os.path.exists(config.GOOGLE_SECRETS):
        run_type = 2
        print("\nType 2 run PUBLISHER: We are on a local machine")


    else:
        run_type = 1
        print("\nType 1 run PUBLISHER: We are on Github")


def initialize_gee():
    """
    Initialize Google Earth Engine (GEE), RCLONE and Google Drive authentication.

    This function authenticates GEE and Google Drive either using a service account key file
    or GitHub secrets depending on the run type.

    Returns:
    None
    """

    scopes = ["https://www.googleapis.com/auth/drive"]


    if run_type == 2:
        # Initialize GEE and Google Drive using service account key file

        # Authenticate using the service account key file
        with open(config.GOOGLE_SECRETS, "r") as f:
            service_account_key = json.load(f)

        # Authenticate Google Drive
        gauth = GoogleAuth()
        gauth.service_account_file = config.GOOGLE_SECRETS
        gauth.service_account_email = service_account_key["client_email"]
        gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
            gauth.service_account_file, scopes=scopes
        )

        google_secret_file = config.GOOGLE_SECRETS

        # Load AWS credentials from JSON
        with open(config.S3_SECRETS, "r") as f:
            aws_creds = json.load(f)



    else:
        # Initialize GEE and Google Drive using GitHub secrets

        # Authenticate using the provided secrets from GitHub Actions
        gauth = GoogleAuth()
        google_client_secret = json.loads(
            os.environ.get('GOOGLE_CLIENT_SECRET'))
        gauth.service_account_email = google_client_secret["client_email"]
        gauth.service_account_file = "keyfile.json"
        with open(gauth.service_account_file, "w") as f:
            f.write(json.dumps(google_client_secret))
        gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
            gauth.service_account_file, scopes=scopes
        )

        # Write GDRIVE Secrest config to a file
        google_secret = os.environ.get('GOOGLE_CLIENT_SECRET')
        google_secret_file = "keyfile.json"
        with open(google_secret_file, "w") as f:
            f.write(google_secret)

        # Write S3 secret config to a file
        s3_secret = os.environ.get('S3_SECRETS')
        s3_secret_file = "s3.json"
        with open(s3_secret_file, "w") as f:
            f.write(s3_secret)

        # Load AWS credentials from JSON
        with open(s3_secret_file, "r") as f:
            aws_creds = json.load(f)


    # Create the GCS client
    global storage_client
    storage_client = storage.Client.from_service_account_json(
            gauth.service_account_file)

    # Initialize EE
    credentials = ee.ServiceAccountCredentials(
        gauth.service_account_email, gauth.service_account_file
    )
    ee.Initialize(credentials)

    # Test EE initialization
    image = ee.Image("NASA/NASADEM_HGT/001")
    title = image.get("title").getInfo()
    if title == "NASADEM: NASA NASADEM Digital Elevation 30m":
        print("GEE initialization successful")
    else:
        print("GEE initialization FAILED")

    # Initialize S3 client with credentials
    global s3
    s3 = boto3.client(
        "s3",
        aws_access_key_id=aws_creds["aws_access_key_id"],
        aws_secret_access_key=aws_creds["aws_secret_access_key"],
    )
    print("S3 initialization successful")


if __name__ == "__main__":

    # Test if we are on a local machine or if we are on Github
    determine_run_type()

    # Authenticate with GEE and GDRIVE
    initialize_gee()

    # Google Cloud Storage client

    gcs_client = storage_client


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
                        s3.upload_file(local_tmp_file, config.S3_BUCKET_NAME, s3_key)
                        s3_key = os.path.join(s3_key_path, filename+"_metadata.json").replace("\\", "/")
                        s3.upload_file(os.path.join(config.PROCESSING_DIR,filename+"_metadata.json"), config.S3_BUCKET_NAME, s3_key)
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
