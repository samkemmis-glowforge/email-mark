"""Reads files from Google Drive via the same service account that
content_calendar.py uses for the calendar sheet.

Used by the FB drafting flow to pull image assets out of Drive without
making them publicly viewable. The asset folder needs to be shared
(Viewer access) with the service account's email — you can find that
email in the JSON file at GOOGLE_APPLICATION_CREDENTIALS, under
`client_email`. Sharing the folder gives the service account scoped
read access without any public exposure.

Public entrypoints:
    extract_file_id(url) -> Optional[str]
    download_file(file_id) -> Tuple[bytes, str]      # (content, mime_type)
"""

from __future__ import annotations

import io
import os
import re
from typing import Optional, Tuple

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

# Drive URL forms we extract a file ID from:
#   https://drive.google.com/file/d/FILE_ID/view
#   https://drive.google.com/open?id=FILE_ID
#   https://drive.google.com/uc?id=FILE_ID
#   https://drive.google.com/uc?export=download&id=FILE_ID
# A bare folder URL (drive.google.com/drive/folders/...) is intentionally
# NOT matched because those aren't single files.
_FILE_ID_PATTERNS = [
    re.compile(r"/file/d/([A-Za-z0-9_-]{20,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{20,})"),
]

# Mime types Meta's /photos endpoint accepts. Keep tight to avoid surfacing
# Google Docs / Sheets / unrelated files from the calendar's asset column.
_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/bmp",
    "image/tiff",
    "image/webp",
}


class DriveError(RuntimeError):
    """Raised when Drive credentials are missing or a fetch fails."""


def extract_file_id(url: str) -> Optional[str]:
    """Pull the Drive file ID out of any common Drive URL form."""
    if not url:
        return None
    for pat in _FILE_ID_PATTERNS:
        match = pat.search(url)
        if match:
            return match.group(1)
    return None


def _load_credentials(scopes: list):
    """Load service-account creds from whichever env var is set.

    Supports two patterns:
      - GCP_SERVICE_ACCOUNT_JSON: the entire JSON key as an env var string
        (what Render and other PaaS providers typically use — no file on
        disk). Tried first.
      - GOOGLE_APPLICATION_CREDENTIALS: a file path pointing at the JSON
        key (Google's standard). Fallback, useful for local dev where you
        already have the key file on disk.

    Raises DriveError if neither is set.
    """
    import json as _json

    from google.oauth2 import service_account

    json_content = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if json_content:
        try:
            info = _json.loads(json_content)
        except _json.JSONDecodeError as exc:
            raise DriveError(
                f"GCP_SERVICE_ACCOUNT_JSON is set but isn't valid JSON: {exc}"
            )
        return service_account.Credentials.from_service_account_info(
            info, scopes=scopes
        )

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        return service_account.Credentials.from_service_account_file(
            path, scopes=scopes
        )

    raise DriveError(
        "No Google service-account credentials found. Set either "
        "GCP_SERVICE_ACCOUNT_JSON (entire JSON key as an env var) or "
        "GOOGLE_APPLICATION_CREDENTIALS (path to the JSON key file). "
        "Then share the relevant Drive folders with the service "
        "account's client_email."
    )


def _service():
    """Build the Drive v3 service client with service-account creds.

    Scope is `drive` (not `drive.readonly` and not `drive.file`) so we can
    BOTH read files shared with the service account AND upload new files
    into folders the service account has Editor access to. The service
    account identity is tightly scoped by what's been shared with it, so
    using the broader scope is safe — it can't see anything not shared.
    """
    from googleapiclient.discovery import build

    creds = _load_credentials(["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def download_file(file_id: str) -> Tuple[bytes, str]:
    """Download a Drive file's bytes via the service account.

    Returns (content_bytes, mime_type). Raises DriveError on missing
    credentials, permission denied, non-image file, or any Drive API
    failure. The mime_type check is strict — Mark should never accidentally
    upload a Google Doc or PDF as a Facebook photo.
    """
    # Lazy imports for the API error type + download helper.
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload

    service = _service()

    # Metadata first — gives us mime_type, name, and verifies access.
    try:
        meta = service.files().get(
            fileId=file_id, fields="mimeType,name,size"
        ).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            raise DriveError(
                f"Drive file {file_id} not found, or the service account "
                f"doesn't have access. Share the asset folder with the "
                f"service account's client_email."
            )
        raise DriveError(f"Drive metadata fetch failed: {exc}")
    except Exception as exc:
        raise DriveError(f"Drive metadata fetch failed: {exc}")

    mime = meta.get("mimeType", "application/octet-stream")
    if mime not in _IMAGE_MIME_TYPES:
        raise DriveError(
            f"Drive file '{meta.get('name')}' is {mime}, not a supported "
            f"image type. Mark should only attach JPG/PNG/GIF/BMP/TIFF/"
            f"WebP files from the calendar's asset column."
        )

    buf = io.BytesIO()
    request = service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        try:
            _, done = downloader.next_chunk()
        except HttpError as exc:
            raise DriveError(f"Drive download failed: {exc}")
    return buf.getvalue(), mime


def upload_file(
    *,
    image_bytes: bytes,
    filename: str,
    mime_type: str,
    folder_id: Optional[str] = None,
) -> Tuple[str, str]:
    """Upload bytes to a Drive folder via the service account.

    Returns (file_id, web_view_link). The web_view_link is the URL you'd
    open in a browser to see the file — also the URL that works as
    `drive_url` for draft_facebook_post.

    folder_id defaults to the SOCIAL_ASSETS_DRIVE_FOLDER_ID env var. The
    target folder must be shared with the service account's client_email
    at EDITOR access (not just Viewer — uploads write into the folder).
    """
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseUpload

    if mime_type not in _IMAGE_MIME_TYPES:
        raise DriveError(
            f"Refusing to upload {mime_type} to Drive — only image types "
            f"are allowed via this path."
        )

    folder = folder_id or os.environ.get("SOCIAL_ASSETS_DRIVE_FOLDER_ID")
    if not folder:
        raise DriveError(
            "SOCIAL_ASSETS_DRIVE_FOLDER_ID not set, and no folder_id "
            "passed. Configure the env var to the Drive folder ID where "
            "social-mark should drop image assets, and share that folder "
            "with the service account's client_email at Editor access."
        )

    service = _service()
    media = MediaIoBaseUpload(
        io.BytesIO(image_bytes), mimetype=mime_type, resumable=False
    )
    metadata = {"name": filename, "parents": [folder]}
    try:
        created = (
            service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
    except HttpError as exc:
        if exc.resp.status in (403, 404):
            raise DriveError(
                f"Could not upload to folder {folder}: {exc}. Confirm the "
                f"folder ID is correct and the service account has "
                f"Editor access to it."
            )
        raise DriveError(f"Drive upload failed: {exc}")
    except Exception as exc:
        raise DriveError(f"Drive upload failed: {exc}")

    return created["id"], created.get("webViewLink", "")
