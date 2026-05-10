"""Upload validation and content-addressed storage for student submissions.

Per phase 5 of the design doc:
- size caps per qtype (5MB xlsx, 2MB image, 100KB python)
- magic-byte / signature checks — never trust the filename
- storage path ``data/uploads/<sha256[:2]>/<sha256>.<ext>``, outside any
  static mount, so files are not served back to the browser
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.config import settings


UPLOADS_DIR: Final[Path] = settings.DATA_DIR / "uploads"

# qtype -> (max bytes, extension, kind-label)
_QTYPE_LIMITS: dict[str, tuple[int, str, str]] = {
    "image": (settings.MAX_UPLOAD_IMAGE_BYTES, ".png", "image"),
    "python": (settings.MAX_UPLOAD_PYTHON_BYTES, ".py", "python"),
    "excel": (settings.MAX_UPLOAD_EXCEL_BYTES, ".xlsx", "excel"),
}

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_ZIP_MAGIC = b"PK\x03\x04"  # xlsx is a zip


class UploadError(ValueError):
    """Raised when an upload fails size or signature checks."""


@dataclass(frozen=True)
class StoredUpload:
    """The result of writing a validated upload to disk."""

    path: Path
    sha256: str
    size: int
    ext: str
    kind: str
    text: str | None  # populated for python (utf-8 source); None for binary kinds


def _sniff_image_ext(data: bytes) -> str | None:
    if data.startswith(_PNG_MAGIC):
        return ".png"
    if data.startswith(_JPEG_MAGIC):
        return ".jpg"
    return None


def _validate_python_source(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise UploadError("python source must be valid UTF-8") from e
    # No NULs and no obvious binary garbage (zip, exe, etc).
    if "\x00" in text:
        raise UploadError("python source contains NUL bytes")
    if data.startswith(_ZIP_MAGIC) or data.startswith(b"MZ"):
        raise UploadError("python source has a binary signature")
    return text


def validate_payload(qtype: str, filename: str | None, data: bytes) -> tuple[str, str | None]:
    """Validate size + signature for an upload. Returns (extension, decoded_text).

    Raises ``UploadError`` (subclass of ``ValueError``) on rejection.
    """
    if qtype not in _QTYPE_LIMITS:
        raise UploadError(f"qtype {qtype!r} is not an upload type")

    max_bytes, default_ext, _kind = _QTYPE_LIMITS[qtype]
    size = len(data)
    if size == 0:
        raise UploadError("empty upload")
    if size > max_bytes:
        raise UploadError(
            f"upload too large for {qtype}: {size} bytes > {max_bytes} cap"
        )

    if qtype == "image":
        ext = _sniff_image_ext(data)
        if ext is None:
            raise UploadError(
                "image must start with a PNG or JPEG signature; "
                "did you upload a different file type?"
            )
        return ext, None

    if qtype == "excel":
        if not data.startswith(_ZIP_MAGIC):
            raise UploadError(
                "xlsx must start with the ZIP signature PK\\x03\\x04; "
                "did you upload a different file type?"
            )
        # An .xlsx is a ZIP of OOXML parts; the workbook part path is fixed.
        # Cheap sanity check: the bytes "xl/" appear somewhere in the archive.
        if b"xl/" not in data[:4096] and b"xl/" not in data:
            raise UploadError(
                "file has a ZIP signature but does not look like an .xlsx workbook"
            )
        return ".xlsx", None

    if qtype == "python":
        text = _validate_python_source(data)
        return ".py", text

    raise UploadError(f"unreachable: qtype {qtype!r}")  # pragma: no cover


def store_upload(qtype: str, data: bytes, ext: str, uploads_dir: Path | None = None) -> StoredUpload:
    """Write the bytes to a content-addressed path under ``uploads/``.

    ``uploads_dir`` defaults to ``settings.DATA_DIR / "uploads"``; tests pass a
    tmp_path here.
    """
    base = Path(uploads_dir) if uploads_dir else UPLOADS_DIR
    digest = hashlib.sha256(data).hexdigest()
    sub = base / digest[:2]
    sub.mkdir(parents=True, exist_ok=True)
    path = sub / f"{digest}{ext}"
    # Hashed path is content-addressed: identical bytes dedupe automatically.
    if not path.exists():
        path.write_bytes(data)
    kind = _QTYPE_LIMITS[qtype][2]
    text_value: str | None = None
    if qtype == "python":
        text_value = data.decode("utf-8", errors="replace")
    return StoredUpload(
        path=path,
        sha256=digest,
        size=len(data),
        ext=ext,
        kind=kind,
        text=text_value,
    )


def validate_and_store(
    qtype: str,
    filename: str | None,
    data: bytes,
    *,
    uploads_dir: Path | None = None,
) -> StoredUpload:
    """One-shot helper: validate + store. Raises ``UploadError`` on rejection."""
    ext, text_value = validate_payload(qtype, filename, data)
    stored = store_upload(qtype, data, ext, uploads_dir=uploads_dir)
    if text_value is not None and stored.text is None:  # pragma: no cover
        return StoredUpload(
            path=stored.path,
            sha256=stored.sha256,
            size=stored.size,
            ext=stored.ext,
            kind=stored.kind,
            text=text_value,
        )
    return stored


__all__ = [
    "UploadError",
    "StoredUpload",
    "UPLOADS_DIR",
    "validate_payload",
    "store_upload",
    "validate_and_store",
]
