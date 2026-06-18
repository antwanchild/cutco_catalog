"""Attachment storage helpers for item views."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from flask import current_app

from extensions import db
from helpers import db_commit
from models import Item, ItemAttachment

_ALLOWED_ATTACHMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _attachments_root() -> Path:
    """Return the base upload directory for item attachments."""
    return Path(current_app.config["ATTACHMENTS_DIR"]).expanduser()


def _attachment_dir(item_id: int) -> Path:
    """Return the storage directory for one item's attachments."""
    return _attachments_root() / str(item_id)


def _allowed_attachment(filename: str | None) -> bool:
    """Return whether a filename looks like a supported image upload."""
    if not filename:
        return False
    return Path(filename).suffix.lower() in _ALLOWED_ATTACHMENT_EXTENSIONS


def _store_attachment(item: Item, uploaded_file, caption: str | None) -> ItemAttachment | None:
    """Persist an uploaded attachment and return the database row."""
    original_filename = Path(uploaded_file.filename or "").name
    if not _allowed_attachment(original_filename):
        return None
    if uploaded_file.mimetype and not uploaded_file.mimetype.startswith("image/"):
        return None

    storage_dir = _attachment_dir(item.id)
    storage_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(original_filename).suffix.lower()
    stored_filename = f"{uuid4().hex}{suffix}"
    uploaded_file.save(storage_dir / stored_filename)

    attachment = ItemAttachment(
        item=item,
        original_filename=original_filename,
        stored_filename=stored_filename,
        content_type=uploaded_file.mimetype or None,
        caption=(caption or "").strip() or None,
    )
    db.session.add(attachment)
    if not db_commit(db.session, error_msg="Could not save attachment."):
        stored_path = storage_dir / stored_filename
        if stored_path.exists():
            stored_path.unlink()
        return None
    return attachment
