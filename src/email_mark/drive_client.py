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


def _require_creds_path() -> str:
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        raise DriveError(
            "GOOGLE_APPLICATION_CREDENTIALS not set. Drive file access "
            "requires a service account; share the Drive asset folder "
            "(Viewer access) with the service account's client_email and "
            "set this env var to the JSON key file path."
        )
    return path


def download_file(file_id: str) -> Tuple[bytes, str]:
    """Download a Drive file's bytes via the service account.

    Returns (content_bytes, mime_type). Raises DriveError on missing
    credentials, permission denied, non-image file, or any Drive API
    failure. The mime_type check is strict — Mark should never accidentally
    upload a Google Doc or PDF as a Facebook photo.
    """
    # Lazy imports so the package doesn't import these unless someone
    # actually needs Drive access.
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload

    creds_path = _require_creds_path()
    creds = service_account.Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

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
