# Rewriting the README.md content due to environment reset

# Zoom Recordings to Google Drive Script

This script automates the process of downloading Zoom recordings, structuring them into an organized hierarchy, and uploading them to Google Drive or Shared Drive. It includes logging, tracking script executions, and managing recordings on Zoom.

---

## Key Features

1. **First Run Full Processing**: Processes all Zoom recordings on the first run.
2. **Incremental Processing**: Processes recordings from the last `PROCESSING_DAYS` (default: 60 days) in subsequent runs.
3. **Recording Cleanup**: Deletes recordings from Zoom older than `DELETE_AFTER_DAYS` (default: 365 days) after successful processing.
4. **Logging and Rotation**: Keeps logs for `LOG_RETENTION_DAYS` (default: 180 days), with a clean-up mechanism.
5. **Run Tracking**: Counts and logs each execution of the script.
6. **Google Drive Folder Structure**: Organizes files in `Year/Month/Meeting_Name_Host_Date` format.

---

## Directory Structure

The project follows this structure:

```
zoom_recordings_to_gdrive/
├── zoom_to_drive.py         # Main script
├── .env                     # Configuration file
├── requirements.txt         # Python dependencies
├── service_account.json     # Google service account credentials
├── processed_recordings.json# Tracks processed recordings
├── run_count.json           # Tracks the number of script runs
├── downloads/               # Temporary download folder
├── script.log               # Log file
```

---

## Google Drive Setup

### Personal Google Drive

1. **Enable Google Drive API**:
   - Go to [Google Cloud Console](https://console.cloud.google.com/).
   - Create a project and enable **Google Drive API**.

2. **Create Service Account**:
   - In **IAM & Admin → Service Accounts**, create a new service account.
   - Download the JSON key file.

3. **Grant Folder Access**:
   - Share a folder on your personal Google Drive with the service account email (e.g., `service-account-name@project-id.iam.gserviceaccount.com`) and assign **Editor** permissions.

### Shared Drive (Team Drive)

1. Follow the steps for **Personal Google Drive**.
2. Add the service account to the Shared Drive with **Content Manager** or **Manager** permissions.

---

## Domain-Wide Delegation (Optional, for G Suite Admins)

If you're using G Suite, you can enable **Domain-Wide Delegation** for service accounts to manage multiple user accounts.

1. **Enable Domain-Wide Delegation**:
   - Go to **IAM & Admin → Service Accounts**.
   - Edit your service account and enable "Enable G Suite Domain-wide Delegation."
   - Note the **Client ID** displayed.

2. **Grant API Scopes**:
   - Go to [Google Admin Console](https://admin.google.com/).
   - Navigate to **Security → API Controls → Domain-wide Delegation**.
   - Add a new API client using the service account's **Client ID**.
   - Use the scope: `https://www.googleapis.com/auth/drive`.

---

## Zoom API Setup

1. **Create a Zoom App**:
   - Log in to the [Zoom App Marketplace](https://marketplace.zoom.us/).
   - Create a **Server-to-Server OAuth** app.

2. **Add Scopes**:
   Use these scopes:
   - `cloud_recording:read:list_account_recordings:admin`
   - `cloud_recording:read:list_user_recordings:admin`
   - `cloud_recording:read:list_account_recordings:master`

3. **Save Credentials**:
   - Note the **Client ID**, **Client Secret**, and **Account ID**.

---

## Environment Variables (`.env`)

Create a `.env` file in the root directory with the following content:

```plaintext
SERVICE_ACCOUNT_FILE=service_account.json
GOOGLE_DRIVE_PARENT_ID=GoogleDriveFolderID
ZOOM_CLIENT_ID=YourZoomClientID
ZOOM_CLIENT_SECRET=YourZoomClientSecret
ZOOM_ACCOUNT_ID=YourZoomAccountID
DOWNLOAD_DIR=downloads
```

---

## Customizable Constants

The script includes constants for customization:

- `PROCESSING_DAYS`: Days to process recordings for subsequent runs. Default: `60`.
- `DELETE_AFTER_DAYS`: Days to retain recordings on Zoom. Default: `365`.
- `LOG_RETENTION_DAYS`: Days to retain logs. Default: `180`.

Modify these constants directly in the script to adjust behavior.

---

## Example Folder Structure on Google Drive

The script organizes recordings as follows:

```
/2025/01/Team_Meeting_john.doe@example.com_2025-01-14/
```

---

## Automation with Cron

Automate the script execution using `cron`. Example for monthly runs:

```bash
0 0 1 * * /usr/bin/python3 /path/to/zoom_to_drive.py >> /path/to/logs/script.log 2>&1
```

---

## Installation

Install Python dependencies:

```bash
pip install -r requirements.txt
```

---

## Example Log Output

```plaintext
2025-01-14 20:00:00,123 - INFO - ==================================================
2025-01-14 20:00:00,123 - INFO - Script started at 2025-01-14 20:00:00
2025-01-14 20:00:00,123 - INFO - ==================================================
2025-01-14 20:00:00,124 - INFO - Run count: 1
2025-01-14 20:00:00,125 - INFO - First run detected: Processing all available recordings.
2025-01-14 20:05:00,456 - INFO - File Team_Meeting.mp4 uploaded successfully.
```

---

## Troubleshooting

1. **Authentication Errors**:
   - Verify `.env` credentials.
   - Ensure service account access.

2. **Google Drive Upload Issues**:
   - Confirm service account access to the folder.
   - Ensure API scopes are correctly configured.

3. **Zoom API Errors**:
   - Check API scopes for the Zoom app.
   - Confirm Zoom credentials in `.env`.

---
