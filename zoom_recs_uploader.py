import os
import json
import requests
import base64
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from tqdm import tqdm
import re

# Constants
PROCESSING_DAYS = 60  # Process recordings from the last 60 days for subsequent runs
DELETE_AFTER_DAYS = 365  # Delete recordings from Zoom older than 365 days
LOG_RETENTION_DAYS = 180  # Retain log entries for 180 days

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

credentials = Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=SCOPES
)
drive_service = build("drive", "v3", credentials=credentials)


def setup_logging():
    """Set up logging with rotation and cleanup."""
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
    """Remove log entries older than LOG_RETENTION_DAYS."""
    try:
        if not os.path.exists(LOG_FILE):
            return
        with open(LOG_FILE, "r") as log_file:
            lines = log_file.readlines()
        cutoff_date = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
        updated_lines = [
            line for line in lines
            if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", line) or
            datetime.strptime(line.split(" - ")[0], "%Y-%m-%d %H:%M:%S") >= cutoff_date
        ]
        with open(LOG_FILE, "w") as log_file:
            log_file.writelines(updated_lines)
    except Exception as e:
        logging.error(f"Failed to clean old logs: {e}")


def load_state():
    """Load processed recordings state from file."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    """Save processed recordings state to file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)


def load_run_count():
    """Load the run count from file."""
    if os.path.exists(RUN_COUNT_FILE):
        with open(RUN_COUNT_FILE, "r") as f:
            return json.load(f).get("run_count", 0)
    return 0


def save_run_count(run_count):
    with open(RUN_COUNT_FILE, "w") as f:
        json.dump({"run_count": run_count}, f, indent=4)


def get_zoom_access_token():
    """Generate access token using Zoom credentials."""
    url = "https://zoom.us/oauth/token"
    headers = {
        "Authorization": "Basic " + base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode(),
        "Content-Type": "application/x-www-form-urlencoded"
    }
    payload = {"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID}
    response = requests.post(url, headers=headers, data=payload)
    response.raise_for_status()
    return response.json().get("access_token")


def fetch_zoom_recordings(token, from_date, to_date):
    """Fetch recordings from Zoom."""
    url = "https://api.zoom.us/v2/accounts/me/recordings"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"from": from_date, "to": to_date, "page_size": 100}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json().get("meetings", [])


def sanitize_filename(filename):
    """
    Sanitize a filename by removing or replacing invalid characters.
    """
    # Remove prohibited characters
    sanitized = re.sub(r'[<>:"/\\|?*]', '', filename)  # Remove characters restricted by Windows/Google Drive
    sanitized = sanitized.replace("'", "")  # Remove single quotes
    sanitized = sanitized.replace("&", "and")  # Replace ampersand with "and"
    sanitized = sanitized.replace("%", "percent")  # Replace percent sign with "percent"
    sanitized = sanitized.replace(" ", "_")  # Replace spaces with underscores
    sanitized = sanitized.strip()  # Trim leading and trailing whitespace
    return sanitized


def create_folder_on_google_drive(folder_name, parent_id=None):
    """Create a folder on Google Drive or Shared Drive."""
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder'"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    response = drive_service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = response.get("files", [])
    if files:
        return files[0]['id']
    else:
        file_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]
        }
        folder = drive_service.files().create(
            body=file_metadata,
            fields="id",
            supportsAllDrives=True
        ).execute()
        return folder["id"]


def upload_to_google_drive(file_path, file_name, year, month, meeting_folder):
    """Upload a file to Google Drive under a specific folder structure."""
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
    logging.info(f"File {file_name} uploaded successfully with ID: {uploaded_file['id']}")


def download_file(download_url, token, file_path):
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


def process_recordings():
    """Main logic for processing recordings."""
    state = load_state()
    run_count = load_run_count() + 1
    save_run_count(run_count)

    if run_count == 1:
        logging.info("First run detected: Processing all available recordings.")
        from_date = "1970-01-01"
    else:
        logging.info(f"Run #{run_count}: Processing recordings from the last {PROCESSING_DAYS} days.")
        from_date = (datetime.now() - timedelta(days=PROCESSING_DAYS)).strftime("%Y-%m-%d")

    to_date = datetime.now().strftime("%Y-%m-%d")
    token = get_zoom_access_token()
    recordings = fetch_zoom_recordings(token, from_date, to_date)

    if not recordings:
        logging.info("No recordings found.")
        return

    for recording in tqdm(recordings, desc="Processing recordings"):
        meeting_id = recording["id"]
        if meeting_id in state:
            logging.info(f"Skipping already processed recording: {meeting_id}")
            continue

        folder_name = sanitize_filename(f"{recording['topic']}_{recording['host_email']}_{recording['start_time'][:10]}")
        year, month = datetime.now().year, datetime.now().month

        for file in recording.get("recording_files", []):
            download_url = file.get("download_url")
            if not download_url:
                logging.warning(f"Invalid file in meeting {meeting_id}. Skipping.")
                continue

            file_extension = f".{file['file_type'].lower()}" if file.get("file_type") else ".bin"
            file_name = sanitize_filename(f"{folder_name}_{file['id']}{file_extension}")
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
                    logging.error(f"Failed to download file after token refresh: {file_path}")

            except Exception as e:
                logging.error(f"Error processing file {file_name}: {e}")

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
