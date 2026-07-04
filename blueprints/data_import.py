"""Shared import and completion parsing for the data blueprint."""

from blueprints.import_shared import (
    _availability_preview_fields,  # noqa: F401
    _build_import_header_report,  # noqa: F401
    _build_item_name_lookup,  # noqa: F401
    _build_item_sku_lookup,  # noqa: F401
    _build_notes,  # noqa: F401
    _build_set_sku_lookup,  # noqa: F401
    _completion_field_name,  # noqa: F401
    _display_import_color,  # noqa: F401
    _import_row_label,  # noqa: F401
    _normalize_import_color,  # noqa: F401
    _normalize_variant_lookup_name,  # noqa: F401
    _normalized_header,  # noqa: F401
    _parse_truthy_field,  # noqa: F401
    _preview_import_color,  # noqa: F401
    _read_completion_rows,  # noqa: F401
    _resolve_import_variant_color,  # noqa: F401
    _safe_csv_filename,  # noqa: F401
)
from models import Item


# Keep the ownership parser local so import and completion flows share one rule set.
def _parse_owned_raw(owned_raw: str, default_person: str | None):
    """Parse 'Owned?' cell. Returns (status, person_name)."""
    raw_value = owned_raw.strip()
    if raw_value.lower() in {"yes", "y", "true", "1"}:
        return "Owned", default_person
    if raw_value.lower() in {"no", "n", "false", "0", ""}:
        return "Wishlist", default_person
    return "Owned", raw_value or default_person


def _match_import_item(
    *,
    existing_items: dict[str, Item],
    existing_names: dict[str, Item],
    sku: str | None,
    name: str,
) -> Item | None:
    """Match an import row to an existing item, preferring SKU over name."""
    if sku:
        return existing_items.get(sku)
    if name:
        return existing_names.get(_normalize_variant_lookup_name(name))
    return None


def _merge_note_text(existing: str | None, incoming: str | None) -> str | None:
    """Merge two free-text notes while keeping duplicates out."""
    existing_value = (existing or "").strip()
    incoming_value = (incoming or "").strip()
    if existing_value and incoming_value:
        if incoming_value.lower() in existing_value.lower():
            return existing_value
        return f"{existing_value}; {incoming_value}"
    return incoming_value or existing_value or None
