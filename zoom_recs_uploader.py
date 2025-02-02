import os
import json
import requests
import base64
import logging
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta  # pip install python-dateutil
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from tqdm import tqdm
import re

# Constants
PROCESSING_DAYS = 60          # Process recordings from the last 60 days for subsequent runs
DELETE_AFTER_DAYS = 365       # Delete recordings from Zoom older than 365 days
LOG_RETENTION_DAYS = 180      # Retain log entries for 180 days

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "script.log")
STATE_FILE = os.path.join(BASE_DIR, "processed_recordings.json")
RUN_COUNT_FILE = os.path.join(BASE_DIR, "run_count.json")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Load environment variables
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, os.getenv("SERVICE_ACCOUNT_FILE"))
GOOGLE_DRIVE_PARENT_ID = os.getenv("GOOGLE_DRIVE_PARENT_ID")
ZOOM_CLIENT_ID = os.getenv("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET")
ZOOM_ACCOUNT_ID = os.getenv("ZOOM_ACCOUNT_ID")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Validate environment variables
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
    Takes into account microseconds in the log: 'YYYY-MM-DD HH:MM:SS,fff'
    """
    try:
        if not os.path.exists(LOG_FILE):
            return
        with open(LOG_FILE, "r") as log_file:
            lines = log_file.readlines()

        cutoff_date = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
        updated_lines = []

        for line in lines:
            parts = line.split(" - ")
            if len(parts) < 2:
                updated_lines.append(line)
                continue

            timestamp_str = parts[0].strip()  # "2025-01-15 13:34:06,568"
            # Attempt parsing with microseconds
            try:
                dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S,%f")
            except ValueError:
                # If parsing fails, try without microseconds
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
    Load the processed recordings state from a file.
    """
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    """
    Save the processed recordings state to a file.
    """
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)


def load_run_count():
    """
    Load the run count from a file.
    """
    if os.path.exists(RUN_COUNT_FILE):
        with open(RUN_COUNT_FILE, "r") as f:
            return json.load(f).get("run_count", 0)
    return 0


def save_run_count(run_count):
    """
    Save the run count to a file.
    """
    with open(RUN_COUNT_FILE, "w") as f:
        json.dump({"run_count": run_count}, f, indent=4)


def get_zoom_access_token():
    """
    Generate an access token using Zoom credentials.
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


# ------------------------------------------------------------------------------
# Functions for paginated loading and segmented (monthly) retrieval
# ------------------------------------------------------------------------------
def fetch_zoom_recordings_page(token, from_date, to_date, next_page_token=None, mc=False):
    """
    Retrieve one "page" (page_size=300) of Zoom recordings,
    taking into account next_page_token (if any), returning JSON response.
    """
    url = "https://api.zoom.us/v2/accounts/me/recordings"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "from": from_date,
        "to": to_date,
        "page_size": 300  # max page_size for Zoom
    }
    if mc:
        params["mc"] = "true"

    if next_page_token:
        params["next_page_token"] = next_page_token

    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()


def fetch_zoom_recordings(token, from_date, to_date, mc=False):
    """
    Get all recordings from Zoom for the range from_date–to_date,
    using paginated loading (next_page_token).
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
    Split the request into monthly intervals (avoiding the 6-month jump that could skip months).
    Retrieve all recordings by calling fetch_zoom_recordings within each segment.
    """
    all_meetings = []
    current_start = start_date

    while current_start < end_date:
        # Add 1 month to the current start
        current_end = current_start + relativedelta(months=1)
        if current_end > end_date:
            current_end = end_date

        from_str = current_start.strftime("%Y-%m-%d")
        to_str = current_end.strftime("%Y-%m-%d")

        chunk_meetings = fetch_zoom_recordings(token, from_str, to_str, mc=mc)
        all_meetings.extend(chunk_meetings)

        # The next interval starts 1 day after current_end
        current_start = current_end + timedelta(days=1)

    return all_meetings


# ------------------------------------------------------------------------------
# Other helper functions
# ------------------------------------------------------------------------------
def sanitize_filename(filename):
    """
    Sanitize a filename by removing or replacing invalid characters.
    """
    sanitized = re.sub(r'[<>:"/\\|?*]', '', filename)
    sanitized = sanitized.replace("'", "")
    sanitized = sanitized.replace("&", "and")
    sanitized = sanitized.replace("%", "percent")
    sanitized = sanitized.replace(" ", "_")
    sanitized = sanitized.strip()
    return sanitized


def create_folder_on_google_drive(folder_name, parent_id=None):
    """
    Create (or find existing) folder (folder_name) in Google Drive under parent_id, if provided.
    Includes extra logs and trashed=false filtering.
    """
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    logging.debug(f"[GDRIVE] Folder search query: {query}")

    response = drive_service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name, parents)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = response.get("files", [])

    logging.debug(f"[GDRIVE] Found files: {files}")

    if files:
        folder_id = files[0]['id']
        logging.debug(f"[GDRIVE] Folder '{folder_name}' already exists with ID={folder_id}")
        return folder_id
    else:
        logging.debug(f"[GDRIVE] Creating folder '{folder_name}' under parent={parent_id}")
        file_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder"
        }
        if parent_id:
            file_metadata["parents"] = [parent_id]

        folder = drive_service.files().create(
            body=file_metadata,
            fields="id",
            supportsAllDrives=True
        ).execute()

        folder_id = folder["id"]
        logging.debug(f"[GDRIVE] Created folder '{folder_name}' with ID={folder_id}")
        return folder_id


def upload_to_google_drive(file_path, file_name, year, month, meeting_folder):
    """
    Upload a file to Google Drive under the folder structure:
       /[parent]/<year>/<month>/<meeting_folder>/<file_name>
    """
    year_folder_id = create_folder_on_google_drive(str(year), GOOGLE_DRIVE_PARENT_ID)
    month_folder_id = create_folder_on_google_drive(f"{month:02d}", year_folder_id)
    meeting_folder_id = create_folder_on_google_drive(meeting_folder, month_folder_id)

    file_metadata = {
        "name": file_name,
        "parents": [meeting_folder_id]
    }
    media = MediaFileUpload(file_path, resumable=True)
    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True
    ).execute()
    logging.info(f"File {file_name} uploaded successfully with ID: {uploaded_file['id']}")


def download_file(download_url, token, file_path):
    """
    Download a file from download_url (secured by a Bearer token).
    Returns True if successful, or False if a token refresh is needed.
    """
    try:
        with requests.get(download_url, headers={"Authorization": f"Bearer {token}"}, stream=True) as r:
            r.raise_for_status()
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            logging.warning("Unauthorized error detected. Refreshing token.")
            return False
        else:
            raise
    return True


# ------------------------------------------------------------------------------
# Main logic
# ------------------------------------------------------------------------------
def process_recordings():
    """
    Main workflow:
    1. Check how many times the script has run (run_count).
    2. If it's the first run — get all possible recordings from 1970 (in practice, Zoom only returns up to 6 months back).
    3. If not the first run — get recordings for the last PROCESSING_DAYS days.
    4. Download, upload to Google Drive, and mark in STATE_FILE as processed.
    """
    state = load_state()
    run_count = load_run_count() + 1
    save_run_count(run_count)

    if run_count == 1:
        logging.info("First run detected: Processing all available recordings.")
        start_date = datetime(1970, 1, 1)
    else:
        logging.info(f"Run #{run_count}: Processing recordings from the last {PROCESSING_DAYS} days.")
        start_date = datetime.now() - timedelta(days=PROCESSING_DAYS)

    end_date = datetime.now()

    # Get token
    token = get_zoom_access_token()

    # Gather all recordings for the period start_date – end_date
    recordings = fetch_zoom_recordings_in_chunks(token, start_date, end_date, mc=False)

    if not recordings:
        logging.info("No recordings found.")
        return

    # Process each recording
    for recording in tqdm(recordings, desc="Processing recordings"):
        meeting_id = recording["id"]
        if meeting_id in state:
            logging.info(f"Skipping already processed recording: {meeting_id}")
            continue

        # Folder name like: "Topic_host_email_YYYY-MM-DD"
        folder_name = sanitize_filename(
            f"{recording['topic']}_{recording['host_email']}_{recording['start_time'][:10]}"
        )

        # Parse the actual meeting date (year/month) from start_time
        start_time_str = recording["start_time"]
        dt_str = start_time_str[:19]
        try:
            meeting_dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            # fallback, e.g. "YYYY-MM-DD HH:MM:SS"
            meeting_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")

        year = meeting_dt.year
        month = meeting_dt.month

        # Download all recording files
        for file_info in recording.get("recording_files", []):
            download_url = file_info.get("download_url")
            if not download_url:
                logging.warning(f"Invalid file in meeting {meeting_id}. Skipping.")
                continue

            file_extension = f".{file_info['file_type'].lower()}" if file_info.get("file_type") else ".bin"
            file_name = sanitize_filename(f"{folder_name}_{file_info['id']}{file_extension}")
            file_path = os.path.join(DOWNLOAD_DIR, file_name)

            try:
                success = download_file(download_url, token, file_path)
                if not success:
                    # Try refreshing the token and retry
                    token = get_zoom_access_token()
                    success = download_file(download_url, token, file_path)

                if success:
                    # Use the parsed year, month
                    upload_to_google_drive(file_path, file_name, year, month, folder_name)
                    os.remove(file_path)
                else:
                    logging.error(f"Failed to download file after token refresh: {file_path}")

            except Exception as e:
                logging.error(f"Error processing file {file_name}: {e}")

        # Save to state that this meeting_id was processed
        state[meeting_id] = {"processed_at": datetime.now().isoformat()}
        save_state(state)


def main():
    setup_logging()
    logging.info("Script execution begins...")
    try:
        process_recordings()
    except Exception as e:
        logging.critical(f"Critical error occurred: {e}")
    logging.info("Script execution ends.")


if __name__ == "__main__":
    main()
