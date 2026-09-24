"""Shared Google OAuth.

Lives below both `export` and `outreach.sender` so the two can share one
downloaded OAuth client without importing each other, which the layering rule
in docs/architecture.md forbids.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def load_credentials(
    scopes: list[str],
    token_path: Path,
    credentials_path: Path,
    *,
    purpose: str,
) -> Any:
    """Cached user credentials for `scopes`, running the consent flow once if needed.

    Each scope set gets its own token file. Sharing one would hand a caller a
    token carrying a grant it never asked for -- a send scope reused for an
    upload, or the reverse -- and the narrower of the two would fail at the API
    call rather than here, where the message can say what to do about it.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - exercised by the extra being absent
        raise RuntimeError(
            f"{purpose} needs the Google client library: uv sync --extra email"
        ) from exc

    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not credentials_path.is_file():
            raise RuntimeError(
                f"{purpose} found no OAuth client at {credentials_path}. Create one in "
                "Google Cloud, download it, and see README for the setup steps."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
        creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
        log.info("stored a %s token at %s", purpose, token_path)
    return creds
