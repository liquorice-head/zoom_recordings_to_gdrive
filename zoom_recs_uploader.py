import os
import json
import requests
import base64
import logging
import argparse
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta  # pip install python-dateutil
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from tqdm import tqdm
import re

# ------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------
PROCESSING_DAYS = 60
DELETE_AFTER_DAYS = 365 
LOG_RETENTION_DAYS = 180

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "script.log")
STATE_FILE = os.path.join(BASE_DIR, "processed_recordings.json")
RUN_COUNT_FILE = os.path.join(BASE_DIR, "run_count.json")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, os.getenv("SERVICE_ACCOUNT_FILE"))
GOOGLE_DRIVE_PARENT_ID = os.getenv("GOOGLE_DRIVE_PARENT_ID")
ZOOM_CLIENT_ID = os.getenv("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET")
ZOOM_ACCOUNT_ID = os.getenv("ZOOM_ACCOUNT_ID")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

if not all([SERVICE_ACCOUNT_FILE, GOOGLE_DRIVE_PARENT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_ACCOUNT_ID]):
    raise ValueError("Missing required environment variables. Check your .env file.")

credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
drive_service = build("drive", "v3", credentials=credentials)

def setup_logging():
    """
    Configure logging with rotation and cleanup.
    """
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, mode="a"),
            logging.StreamHandler()
        ]
    )
    logging.info("=" * 50)
    logging.info(f"Script started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("=" * 50)
    clean_old_logs()

def clean_old_logs():
    """
    Remove log entries older than LOG_RETENTION_DAYS.
    Taking into account microseconds in the log: 'YYYY-MM-DD HH:MM:SS,fff'
    """
    try:
        if not os.path.exists(LOG_FILE):
            return
        cutoff_date = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
        updated_lines = []

        with open(LOG_FILE, "r") as log_file:
            lines = log_file.readlines()

        for line in lines:
            parts = line.split(" - ")
            if len(parts) < 2:
                updated_lines.append(line)
                continue

            timestamp_str = parts[0].strip()  # e.g. 2025-01-15 13:34:06,568
            try:
                dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S,%f")
            except ValueError:
                try:
                    dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    updated_lines.append(line)
                    continue

            if dt >= cutoff_date:
                updated_lines.append(line)

        with open(LOG_FILE, "w") as log_file:
            log_file.writelines(updated_lines)
    except Exception as e:
        logging.error(f"Failed to clean old logs: {e}")

def load_state():
    """
    Load the processed recordings state from file.
    """
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    """
    Save the processed recordings state to file.
    """
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

def load_run_count():
    """
    Load the run count from file.
    """
    if os.path.exists(RUN_COUNT_FILE):
        with open(RUN_COUNT_FILE, "r") as f:
            return json.load(f).get("run_count", 0)
    return 0

def save_run_count(run_count):
    """
    Save the run count to file.
    """
    with open(RUN_COUNT_FILE, "w") as f:
        json.dump({"run_count": run_count}, f, indent=4)

def get_zoom_access_token():
    """
    Generate Zoom access token using client_id, client_secret, and account_id.
    """
    url = "https://zoom.us/oauth/token"
    headers = {
        "Authorization": "Basic " + base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode(),
        "Content-Type": "application/x-www-form-urlencoded"
    }
    payload = {"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID}
    response = requests.post(url, headers=headers, data=payload)
    response.raise_for_status()
    return response.json().get("access_token")

# -------------------------------------------------------------------------
# Download/Upload Functions
# -------------------------------------------------------------------------
def fetch_zoom_recordings_page(token, from_date, to_date, next_page_token=None, mc=False):
    """
    Retrieve one page of Zoom recordings (page_size=300).
    """
    url = "https://api.zoom.us/v2/accounts/me/recordings"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "from": from_date,
        "to": to_date,
        "page_size": 300
    }
    if mc:
        params["mc"] = "true"
    if next_page_token:
        params["next_page_token"] = next_page_token

    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()

def fetch_zoom_recordings(token, from_date, to_date, mc=False):
    """
    Paginated retrieval of recordings in [from_date, to_date].
    """
    all_meetings = []
    next_page_token = None
    while True:
        data = fetch_zoom_recordings_page(token, from_date, to_date, next_page_token, mc=mc)
        meetings = data.get("meetings", [])
        all_meetings.extend(meetings)
        next_page_token = data.get("next_page_token")
        if not next_page_token:
            break
    return all_meetings

def fetch_zoom_recordings_in_chunks(token, start_date, end_date, mc=False):
    """
    Split requests by 1-month chunks to ensure we don't skip anything.
    """
    all_meetings = []
    current_start = start_date
    while current_start < end_date:
        current_end = current_start + relativedelta(months=1)
        if current_end > end_date:
            current_end = end_date

        from_str = current_start.strftime("%Y-%m-%d")
        to_str   = current_end.strftime("%Y-%m-%d")

        chunk_meetings = fetch_zoom_recordings(token, from_str, to_str, mc=mc)
        all_meetings.extend(chunk_meetings)
        current_start = current_end + timedelta(days=1)
    return all_meetings

def sanitize_filename(filename):
    """
    Remove invalid characters from filename.
    """
    sanitized = re.sub(r'[<>:"/\\|?*]', '', filename)
    sanitized = sanitized.replace("'", "")
    sanitized = sanitized.replace("&", "and")
    sanitized = sanitized.replace("%", "percent")
    sanitized = sanitized.replace(" ", "_")
    return sanitized.strip()

def create_folder_on_google_drive(folder_name, parent_id=None):
    """
    Create/find folder on Google Drive (with trashed=false).
    """
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    logging.debug(f"[GDRIVE] Query: {query}")
    resp = drive_service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name, parents)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = resp.get("files", [])

    if files:
        folder_id = files[0]['id']
        logging.debug(f"[GDRIVE] Folder '{folder_name}' exists: {folder_id}")
        return folder_id
    else:
        file_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder"
        }
        if parent_id:
            file_metadata["parents"] = [parent_id]

        new_folder = drive_service.files().create(
            body=file_metadata,
            fields="id",
            supportsAllDrives=True
        ).execute()
        folder_id = new_folder["id"]
        logging.debug(f"[GDRIVE] Created folder '{folder_name}' -> {folder_id}")
        return folder_id

def upload_to_google_drive(file_path, file_name, year, month, meeting_folder):
    """
    Upload file: /<PARENT>/<year>/<month>/<meeting_folder>/<file_name>
    """
    year_folder_id = create_folder_on_google_drive(str(year), GOOGLE_DRIVE_PARENT_ID)
    month_folder_id = create_folder_on_google_drive(f"{month:02d}", year_folder_id)
    meeting_folder_id = create_folder_on_google_drive(meeting_folder, month_folder_id)

    file_metadata = {"name": file_name, "parents": [meeting_folder_id]}
    media = MediaFileUpload(file_path, resumable=True)
    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True
    ).execute()
    logging.info(f"File {file_name} uploaded with ID: {uploaded_file['id']}")

def download_file(download_url, token, file_path):
    """
    Download a Zoom recording file. Return True if success, False if token refresh needed.
    """
    try:
        with requests.get(download_url, headers={"Authorization": f"Bearer {token}"}, stream=True) as r:
            r.raise_for_status()
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            logging.warning("401 Unauthorized -> need token refresh.")
            return False
        else:
            raise
    return True

# -------------------------------------------------------------------------
# Deletion logic
# -------------------------------------------------------------------------
def delete_zoom_recording(token, meeting_id):
    """
    Delete ALL Zoom recordings for the given meetingId (the entire meeting).
    """
    url = f"https://api.zoom.us/v2/meetings/{meeting_id}/recordings"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.delete(url, headers=headers)
    resp.raise_for_status()  # Raise if error
    logging.info(f"Deleted recordings for meeting: {meeting_id}")

def delete_old_recordings(token):
    """
    Fetch all recordings older than DELETE_AFTER_DAYS across the entire account,
    then delete them from Zoom.
    """
    logging.info("Starting deletion of old recordings.")
    delete_cutoff = datetime.now() - timedelta(days=DELETE_AFTER_DAYS)

    # We'll gather older recordings by chunk, from 1970 up to the cutoff
    start_date = datetime(2020, 1, 1)
    end_date = delete_cutoff

    all_old_meetings = fetch_zoom_recordings_in_chunks(token, start_date, end_date, mc=False)
    logging.info(f"Found {len(all_old_meetings)} recordings older than {DELETE_AFTER_DAYS} days.")

    for recording in tqdm(all_old_meetings, desc="Deleting old recordings"):
        meeting_id = recording["id"]
        # Double-check date
        st_str = recording["start_time"][:19]
        try:
            dt_meeting = datetime.strptime(st_str, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            dt_meeting = datetime.strptime(st_str, "%Y-%m-%d %H:%M:%S")

        if dt_meeting < delete_cutoff:
            try:
                delete_zoom_recording(token, meeting_id)
            except Exception as e:
                logging.error(f"Error deleting {meeting_id}: {e}")
    logging.info("Deletion of old recordings complete.")


# -------------------------------------------------------------------------
# Main logic
# -------------------------------------------------------------------------
def process_recordings():
    """
    1) First run => all recordings from 2020; else => last PROCESSING_DAYS days
    2) Download, upload, mark state
    3) DOES NOT do any deletion here by design (user wants separate mode).
    """
    state = load_state()
    run_count = load_run_count() + 1
    save_run_count(run_count)

    if run_count == 1:
        logging.info("First run: process all available recordings.")
        start_date = datetime(2020, 1, 1)
    else:
        logging.info(f"Run #{run_count}: last {PROCESSING_DAYS} days.")
        start_date = datetime.now() - timedelta(days=PROCESSING_DAYS)

    end_date = datetime.now()
    token = get_zoom_access_token()
    recordings = fetch_zoom_recordings_in_chunks(token, start_date, end_date, mc=False)

    if not recordings:
        logging.info("No recordings found.")
        return

    for recording in tqdm(recordings, desc="Processing recordings"):
        meeting_id = recording["id"]
        if meeting_id in state:
            logging.info(f"Skipping processed meeting: {meeting_id}")
            continue

        folder_name = sanitize_filename(
            f"{recording['topic']}_{recording['host_email']}_{recording['start_time'][:10]}"
        )
        # Parse date/time to get year & month
        st_str = recording["start_time"][:19]
        try:
            dt_meet = datetime.strptime(st_str, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            dt_meet = datetime.strptime(st_str, "%Y-%m-%d %H:%M:%S")
        year, month = dt_meet.year, dt_meet.month

        # Download files
        for file_info in recording.get("recording_files", []):
            download_url = file_info.get("download_url")
            if not download_url:
                logging.warning(f"Invalid file in {meeting_id}, skipping.")
                continue

            extension = f".{file_info['file_type'].lower()}" if file_info.get("file_type") else ".bin"
            file_name = sanitize_filename(f"{folder_name}_{file_info['id']}{extension}")
            file_path = os.path.join(DOWNLOAD_DIR, file_name)

            try:
                success = download_file(download_url, token, file_path)
                if not success:
                    token = get_zoom_access_token()
                    success = download_file(download_url, token, file_path)
                if success:
                    upload_to_google_drive(file_path, file_name, year, month, folder_name)
                    os.remove(file_path)
                else:
                    logging.error(f"Failed to download after refresh: {file_path}")
            except Exception as e:
                logging.error(f"Error processing file {file_name}: {e}")

        state[meeting_id] = {"processed_at": datetime.now().isoformat()}
        save_state(state)


def main():
    setup_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Only delete recordings older than DELETE_AFTER_DAYS, skip normal downloads."
    )
    args = parser.parse_args()

    logging.info("Script execution begins...")

    try:
        if args.delete:
            # Only run deletion
            logging.info("Running in delete-only mode.")
            token = get_zoom_access_token()
            delete_old_recordings(token)
        else:
            # Normal mode: download new recordings, then delete old
            logging.info("Running in normal mode: download/upload new recordings, then delete old.")
            process_recordings()

            # After we finish downloading, we want to remove old recordings from the entire account
            # that are older than 1 year
            token = get_zoom_access_token()
            delete_old_recordings(token)

    except Exception as e:
        logging.critical(f"Critical error occurred: {e}")

    logging.info("Script execution ends.")


if __name__ == "__main__":
    main()
