"""`export --to-sheets`: the Drive upload, with no Drive in the picture.

The upload is the one piece here that can quietly do the wrong thing -- making
a fresh sheet on every scan instead of replacing the last one, which breaks the
bookmarked link the whole feature exists to provide.
"""

from __future__ import annotations

import pytest

from jobhunter import export as export_module

# Stands in for a MediaFileUpload, which needs the optional Google client.
SENTINEL_MEDIA = object()


class FakeFiles:
    """The three `service.files()` calls the uploader makes, recorded."""

    def __init__(self, existing: list[dict] | None = None) -> None:
        self.existing = existing or []
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.queries: list[str] = []

    def list(self, *, q, fields, pageSize):
        self.queries.append(q)
        return _Exec({"files": self.existing})

    def create(self, *, body, media_body, fields):
        self.created.append(body)
        return _Exec({"id": "new-id", "webViewLink": "https://docs.google.com/d/new-id"})

    def update(self, *, fileId, media_body, fields):
        self.updated.append({"fileId": fileId})
        return _Exec({"id": fileId, "webViewLink": f"https://docs.google.com/d/{fileId}"})


class _Exec:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeService:
    def __init__(self, files: FakeFiles) -> None:
        self._files = files

    def files(self):
        return self._files


@pytest.fixture
def csv_file(tmp_path):
    path = tmp_path / "shortlist.csv"
    path.write_text("score,company,url\n96,Orcrist,https://example.com/1\n")
    return path


def test_first_upload_creates_a_sheet(csv_file):
    files = FakeFiles(existing=[])
    url = export_module.upload_to_sheets(csv_file, service=FakeService(files), media=SENTINEL_MEDIA)

    assert files.created == [{"name": "shortlist", "mimeType": export_module.SHEET_MIME}]
    assert not files.updated
    assert url == "https://docs.google.com/d/new-id"


def test_second_upload_replaces_the_same_sheet(csv_file):
    """A rescan must not strand the link the user bookmarked."""
    files = FakeFiles(existing=[{"id": "existing-id"}])
    url = export_module.upload_to_sheets(csv_file, service=FakeService(files), media=SENTINEL_MEDIA)

    assert files.updated == [{"fileId": "existing-id"}]
    assert not files.created
    assert url == "https://docs.google.com/d/existing-id"


def test_title_overrides_the_filename(csv_file):
    files = FakeFiles()
    export_module.upload_to_sheets(
        csv_file, title="Q4 shortlist", service=FakeService(files), media=SENTINEL_MEDIA
    )
    assert files.created[0]["name"] == "Q4 shortlist"


def test_an_apostrophe_in_the_title_does_not_break_the_query(csv_file):
    """Drive's `q` is a quoted string; an unescaped name is a malformed query."""
    files = FakeFiles()
    export_module.upload_to_sheets(
        csv_file, title="Shubh's jobs", service=FakeService(files), media=SENTINEL_MEDIA
    )
    assert files.queries == ["name = 'Shubh\\'s jobs' and trashed = false"]


def test_xlsx_uploads_too(tmp_path):
    path = tmp_path / "shortlist.xlsx"
    path.write_bytes(b"PK\x03\x04 not really a workbook, never parsed")
    files = FakeFiles()
    export_module.upload_to_sheets(path, service=FakeService(files), media=SENTINEL_MEDIA)
    assert files.created[0]["mimeType"] == export_module.SHEET_MIME


def test_an_unsupported_suffix_is_refused_before_any_upload(tmp_path):
    path = tmp_path / "shortlist.json"
    path.write_text("[]")
    files = FakeFiles()
    with pytest.raises(ValueError, match="cannot upload"):
        export_module.upload_to_sheets(path, service=FakeService(files), media=SENTINEL_MEDIA)
    assert not files.created


def test_a_missing_file_is_refused_before_any_upload(tmp_path):
    files = FakeFiles()
    with pytest.raises(FileNotFoundError):
        export_module.upload_to_sheets(
            tmp_path / "gone.csv", service=FakeService(files), media=SENTINEL_MEDIA
        )
    assert not files.created
