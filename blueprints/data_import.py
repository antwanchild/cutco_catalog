"""Shared import and completion parsing for the data blueprint."""

from __future__ import annotations

import csv
import io
import re

import openpyxl

from constants import COOKWARE_CATEGORIES, UNKNOWN_COLOR, XLSX_COL_MAP
from models import Item, Set, normalize_sku_value, parse_alternate_skus


def _parse_owned_raw(owned_raw: str, default_person: str | None):
    """Parse 'Owned?' cell. Returns (status, person_name)."""
    val = owned_raw.strip()
    if val.lower() in {"yes", "y", "true", "1"}:
        return "Owned", default_person
    if val.lower() in {"no", "n", "false", "0", ""}:
        return "Wishlist", default_person
    return "Owned", val or default_person


def _build_notes(row: dict) -> tuple[str | None, list[str]]:
    """Combine spreadsheet auxiliary columns into a single notes string."""
    parts = []
    for key, label in [("_notes_price", "Price")]:
        value = row.get(key, "").strip()
        if value and value not in ("0", "none", "n/a", "-"):
            parts.append(f"{label}: {value}")
    return ("; ".join(parts) or None), []


def _normalize_import_color(value: str) -> str:
    """Normalize imported color text into a consistent display/storage form."""
    cleaned = (value or "").strip()
    if not cleaned:
        return UNKNOWN_COLOR
    lowered = cleaned.lower()
    if lowered in {"unknown", "unknown / unspecified", "unknown/unspecified"}:
        return UNKNOWN_COLOR
    return cleaned.title()


def _display_import_color(color: str) -> str:
    """Shorten the long unknown color label in import previews."""
    return "Unknown" if color == UNKNOWN_COLOR else color


def _preview_import_color(color: str, is_cookware: bool = False) -> str:
    """Return a preview color, hiding meaningless cookware color labels."""
    if is_cookware:
        return "—"
    return _display_import_color(color)


def _resolve_import_variant_color(name: str, category: str, color: str) -> str:
    """Return the stored variant color to use for import rows."""
    resolved_color = _normalize_import_color(color)
    if category in COOKWARE_CATEGORIES:
        return UNKNOWN_COLOR
    if resolved_color.lower() == "stainless" and "stainless" in (name or "").lower():
        return UNKNOWN_COLOR
    return resolved_color


def _build_item_sku_lookup(items: list[Item]) -> dict[str, Item]:
    lookup: dict[str, Item] = {}
    for item in items:
        primary_sku = normalize_sku_value(item.sku)
        if primary_sku and primary_sku not in lookup:
            lookup[primary_sku] = item
    for item in items:
        for alias_sku in parse_alternate_skus(item.alternate_skus):
            if alias_sku and alias_sku not in lookup:
                lookup[alias_sku] = item
    return lookup


def _match_import_item(
    *,
    existing_items: dict[str, Item],
    existing_names: dict[str, Item],
    sku: str,
    name: str,
) -> Item | None:
    """Match an import row to an existing item, preferring SKU over name."""
    if sku:
        return existing_items.get(sku)
    if name:
        return existing_names.get(name.lower())
    return None


def _normalize_variant_lookup_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _build_item_name_lookup(items: list[Item]) -> dict[str, Item]:
    lookup: dict[str, Item] = {}
    ambiguous: set[str] = set()
    for item in items:
        key = _normalize_variant_lookup_name(item.name)
        if not key:
            continue
        if key in ambiguous:
            continue
        if key in lookup:
            lookup.pop(key, None)
            ambiguous.add(key)
            continue
        lookup[key] = item
    return lookup


def _build_set_sku_lookup(sets: list[Set]) -> dict[str, Set]:
    lookup: dict[str, Set] = {}
    for item_set in sets:
        set_sku = normalize_sku_value(item_set.sku)
        if set_sku and set_sku not in lookup:
            lookup[set_sku] = item_set
    return lookup


def _parse_truthy_field(value: str) -> bool:
    """Interpret a spreadsheet cell as a yes/no flag."""
    return (value or "").strip().lower() in {"yes", "y", "true", "1"}


def _availability_preview_fields(availability: str) -> tuple[str, str | None]:
    """Return preview-friendly availability label and badge class."""
    labels = {
        "rep only": ("Rep only", "badge-warning"),
        "Costco": ("Costco", "badge-info"),
        "non-catalog": ("Non-catalog", "badge-off-catalog"),
    }
    return labels.get(availability, ("", None))


COMPLETION_COL_MAP = {
    "person": "person",
    "collector": "person",
    "owner": "person",
    "sku": "sku",
    "item_sku": "sku",
    "model #": "sku",
    "model#": "sku",
    "quantity": "quantity",
    "qty": "quantity",
    "note": "note",
    "notes": "note",
}


def _merge_note_text(existing: str | None, incoming: str | None) -> str | None:
    """Merge two free-text notes while keeping duplicates out."""
    existing_value = (existing or "").strip()
    incoming_value = (incoming or "").strip()
    if existing_value and incoming_value:
        if incoming_value.lower() in existing_value.lower():
            return existing_value
        return f"{existing_value}; {incoming_value}"
    return incoming_value or existing_value or None


def _completion_field_name(raw_name: str | None) -> str | None:
    if not raw_name:
        return None
    normalized = _normalized_header(raw_name)
    return COMPLETION_COL_MAP.get(normalized, normalized)


def _read_completion_rows(uploaded_file, paste_text: str) -> tuple[list[dict], str | None]:
    """Read pasted or uploaded completion rows from CSV-like text."""
    content = (paste_text or "").strip()
    if content:
        source_label = "paste"
    elif uploaded_file and uploaded_file.filename:
        content = uploaded_file.stream.read().decode("utf-8-sig")
        source_label = "csv"
    else:
        return [], "Paste rows or choose a CSV file."

    if not content.strip():
        return [], "We couldn't read any rows from this file."

    sample = content[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in content and content.count("\t") >= content.count(",") else csv.excel

    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    raw_headers = [header.strip() for header in (reader.fieldnames or []) if header and header.strip()]
    mapped_headers = {_completion_field_name(header) for header in raw_headers}
    if "person" not in mapped_headers or "sku" not in mapped_headers:
        return [], "Please include a header row with person and sku."

    parsed_rows: list[dict] = []
    for row_num, row in enumerate(reader, start=2):
        if not any((cell or "").strip() for cell in row.values() if cell is not None):
            continue
        normalized: dict[str, str] = {}
        for orig_key, val in row.items():
            field_name = _completion_field_name(orig_key)
            if not field_name:
                continue
            normalized[field_name] = val.strip() if val is not None else ""
        normalized["source_label"] = source_label
        normalized["row_num"] = row_num
        parsed_rows.append(normalized)
    return parsed_rows, None


def _safe_csv_filename(raw_name: str) -> str:
    """Normalize a user-provided filename into a safe CSV filename."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (raw_name or "").strip()).strip("._")
    if not cleaned:
        cleaned = "cutco_collection"
    if not cleaned.lower().endswith(".csv"):
        cleaned += ".csv"
    return cleaned


def _import_row_label(row_num: int | None, name: str | None = None, sku: str | None = None) -> str:
    """Build a compact human-readable row label for import summaries."""
    parts = []
    if row_num is not None:
        parts.append(f"Row {row_num}")
    if name:
        parts.append(name)
    if sku:
        parts.append(f"SKU {sku}")
    return " - ".join(parts) if parts else "Unknown row"


def _normalized_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def _build_import_header_report(uploaded_file, ext: str) -> dict:
    """Analyze import file headers and return a header summary."""
    raw_headers: list[str] = []
    if ext == "xlsx":
        workbook = openpyxl.load_workbook(io.BytesIO(uploaded_file.stream.read()), data_only=True)
        worksheet = workbook.active
        for cell in worksheet[1]:
            if cell.value is None:
                continue
            header = str(cell.value).strip()
            if header:
                raw_headers.append(header)
    else:
        stream = io.StringIO(uploaded_file.stream.read().decode("utf-8-sig"))
        reader = csv.reader(stream)
        raw_headers = [col.strip() for col in next(reader, []) if col and col.strip()]

    mapped_headers = set()
    for header in raw_headers:
        normalized = _normalized_header(header)
        if normalized in XLSX_COL_MAP:
            mapped_headers.add(XLSX_COL_MAP[normalized])
        else:
            mapped_headers.add(normalized)

    missing_required = []
    if "name" not in mapped_headers:
        missing_required.append("name")

    ownership_columns_found = bool({"owned_raw", "status", "person"} & mapped_headers)
    unicorn_columns_found = bool({"is_sku_unicorn", "is_variant_unicorn", "is_edge_unicorn"} & mapped_headers)
    unknown_headers = sorted(
        header for header in raw_headers
        if _normalized_header(header) not in XLSX_COL_MAP
    )

    warnings = []
    if not ownership_columns_found:
        warnings.append("No ownership/status column found (owned / Owned? / status / person). Rows will default to Owned.")
    if not unicorn_columns_found:
        warnings.append("No unicorn columns found. If needed, add is_sku_unicorn / is_variant_unicorn / is_edge_unicorn.")

    return {
        "ok": not missing_required,
        "file_type": ext.upper(),
        "raw_headers": raw_headers,
        "mapped_headers": sorted(mapped_headers),
        "missing_required": missing_required,
        "warnings": warnings,
        "unknown_headers": unknown_headers,
    }
