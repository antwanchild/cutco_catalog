import csv
import io
import logging
import re
from datetime import date

import openpyxl
from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from sqlalchemy.orm import selectinload
from sqlalchemy import desc

from constants import (
    COOKWARE_CATEGORIES, EDGE_TYPES, STATUS_OPTIONS, TRUTHY, UNKNOWN_COLOR,
    XLSX_COL_MAP, canonicalize_availability, canonicalize_category,
)
from extensions import db
from helpers import admin_required, db_commit
from models import (
    Item,
    ItemVariant,
    ItemSetMember,
    Ownership,
    ActivityEvent,
    Person,
    Set,
    normalize_sku_value,
    parse_alternate_skus,
    record_activity,
    reconcile_unknown_variant,
)
from time_utils import format_container_time

data_bp = Blueprint("data", __name__)
logger = logging.getLogger(__name__)


def _parse_owned_raw(owned_raw: str, default_person: str | None):
    """Parse 'Owned?' cell. Returns (status, person_name)."""
    val = owned_raw.strip()
    if val.lower() in TRUTHY:
        return "Owned", default_person
    if val.lower() in {"no", "n", "false", "0", ""}:
        return "Wishlist", default_person
    return "Owned", val or default_person


def _parse_whole_number(value: str, label: str) -> tuple[int | None, str | None]:
    """Parse a spreadsheet cell into a non-negative whole number."""
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"0", "none", "n/a", "-"}:
        return None, None
    if re.fullmatch(r"\d+", cleaned):
        return int(cleaned), None
    return None, f"{label} must be a whole number."


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


def _build_set_sku_lookup(sets: list[Set]) -> dict[str, Set]:
    lookup: dict[str, Set] = {}
    for item_set in sets:
        set_sku = normalize_sku_value(item_set.sku)
        if set_sku and set_sku not in lookup:
            lookup[set_sku] = item_set
    return lookup


def _parse_quantity_fields(row: dict) -> tuple[int | None, int | None, list[str]]:
    """Parse ownership quantity fields as whole numbers."""
    errors: list[str] = []
    quantity_purchased = None
    quantity_given_away = None
    for key, label in [
        ("quantity_purchased", "Quantity Purchased"),
        ("quantity_given_away", "Quantity Given Away"),
    ]:
        value = row.get(key, "").strip()
        if not value or value.lower() in {"0", "none", "n/a", "-"}:
            continue
        parsed_value, error = _parse_whole_number(value, label)
        if error:
            errors.append(error)
            continue
        if parsed_value is not None:
            if key == "quantity_purchased":
                quantity_purchased = parsed_value
            else:
                quantity_given_away = parsed_value
    return quantity_purchased, quantity_given_away, errors


def _parse_truthy_field(value: str) -> bool:
    """Interpret a spreadsheet cell as a yes/no flag."""
    return (value or "").strip().lower() in TRUTHY


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


def _parse_completion_quantity(raw_value: str) -> tuple[int | None, str | None]:
    """Parse a completion quantity as a positive whole number."""
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return 1, None
    if re.fullmatch(r"[1-9]\d*", cleaned):
        return int(cleaned), None
    return None, "Quantity must be a positive whole number."


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


def _build_completion_preview(
    parsed_rows: list[dict],
    *,
    person_override: str | None = None,
) -> dict:
    """Resolve completion rows into expanded, rolled-up, and unresolved preview data."""
    existing_items = _build_item_sku_lookup(
        Item.query.options(selectinload(Item.variants)).all()
    )
    existing_sets = _build_set_sku_lookup(
        Set.query.options(selectinload(Set.members).selectinload(ItemSetMember.item)).all()
    )
    existing_persons = {person.name.lower(): person for person in Person.query.all()}
    existing_ownerships = {}
    for ownership in Ownership.query.options(
        selectinload(Ownership.person),
        selectinload(Ownership.variant).selectinload(ItemVariant.item),
    ).all():
        existing_ownerships[(ownership.person.name.lower(), ownership.variant.item_id, ownership.variant.color.lower())] = ownership

    unresolved_rows: list[dict] = []
    expanded_rows: list[dict] = []
    set_rows_expanded = 0

    for row in parsed_rows:
        row_num = row.get("row_num")
        person = (person_override or row.get("person", "") or "").strip()
        sku = normalize_sku_value(row.get("sku", ""))
        note = row.get("note", "").strip() or None
        quantity, qty_error = _parse_completion_quantity(row.get("quantity", ""))

        if qty_error:
            unresolved_rows.append({
                "row": row_num,
                "person": person or "—",
                "sku": sku or "—",
                "quantity": row.get("quantity", "") or "—",
                "note": note or "—",
                "reason": qty_error,
            })
            continue
        if not person:
            unresolved_rows.append({
                "row": row_num,
                "person": "—",
                "sku": sku or "—",
                "quantity": quantity,
                "note": note or "—",
                "reason": "Missing person.",
            })
            continue
        if not sku:
            unresolved_rows.append({
                "row": row_num,
                "person": person,
                "sku": "—",
                "quantity": quantity,
                "note": note or "—",
                "reason": "Missing sku.",
            })
            continue

        matched_set = existing_sets.get(sku)
        matched_item = None if matched_set else existing_items.get(sku)

        if matched_set:
            set_rows_expanded += 1
            if not matched_set.members:
                unresolved_rows.append({
                    "row": row_num,
                    "person": person,
                    "sku": sku,
                    "quantity": quantity,
                    "note": note or "—",
                    "reason": "Set SKU not found in member data.",
                })
                continue
            for membership in matched_set.members:
                member_item = membership.item
                if not member_item or not member_item.sku:
                    unresolved_rows.append({
                        "row": row_num,
                        "person": person,
                        "sku": sku,
                        "quantity": quantity,
                        "note": note or "—",
                        "reason": f"Set member missing from catalog: {member_item.name if member_item else 'unknown item'}.",
                    })
                    continue
                member_qty = quantity * (membership.quantity or 1)
                expanded_rows.append({
                    "input_row": row_num,
                    "person": person,
                    "person_key": person.lower(),
                    "original_sku": sku,
                    "resolved_sku": member_item.sku,
                    "item_id": member_item.id,
                    "item_name": member_item.name,
                    "color": UNKNOWN_COLOR,
                    "display_color": "—",
                    "quantity": member_qty,
                    "source": "Set member",
                    "note": note,
                    "source_rows": [row_num],
                })
            continue

        if matched_item:
            expanded_rows.append({
                "input_row": row_num,
                "person": person,
                "person_key": person.lower(),
                "original_sku": sku,
                "resolved_sku": matched_item.sku or sku,
                "item_id": matched_item.id,
                "item_name": matched_item.name,
                "color": UNKNOWN_COLOR,
                "display_color": "—",
                "quantity": quantity,
                "source": "Item",
                "note": note,
                "source_rows": [row_num],
            })
            continue

        unresolved_rows.append({
            "row": row_num,
            "person": person,
            "sku": sku,
            "quantity": quantity,
            "note": note or "—",
            "reason": "Item SKU not found." if sku not in existing_sets else "Set SKU not found.",
        })

    rolled_map: dict[tuple[str, int, str], dict] = {}
    for expanded in expanded_rows:
        key = (expanded["person_key"], expanded["item_id"], expanded["color"].lower())
        bucket = rolled_map.setdefault(key, {
            "person": expanded["person"],
            "person_key": expanded["person_key"],
            "item_id": expanded["item_id"],
            "item_name": expanded["item_name"],
            "sku": expanded["resolved_sku"],
            "color": expanded["color"],
            "display_color": expanded["display_color"],
            "quantity": 0,
            "notes": [],
            "source_rows": [],
            "source_count": 0,
            "action": "",
            "is_new_variant": False,
        })
        bucket["quantity"] += expanded["quantity"]
        bucket["source_count"] += 1
        bucket["source_rows"].extend(expanded["source_rows"])
        if expanded["note"]:
            bucket["notes"].append(expanded["note"])

    rolled_rows: list[dict] = []
    new_ownership_rows = 0
    updated_ownership_rows = 0
    for bucket in rolled_map.values():
        item = db.session.get(Item, bucket["item_id"])
        if not item:
            unresolved_rows.append({
                "row": min(bucket["source_rows"]) if bucket["source_rows"] else None,
                "person": bucket["person"],
                "sku": bucket["sku"],
                "quantity": bucket["quantity"],
                "note": "—",
                "reason": "Matched item vanished before preview.",
            })
            continue
        target_color = UNKNOWN_COLOR if (item.category or "") in COOKWARE_CATEGORIES else bucket["color"]
        variant = next((variant for variant in item.variants if variant.color.lower() == target_color.lower()), None)
        person_obj = existing_persons.get(bucket["person_key"])
        existing_o = None
        if person_obj and variant:
            existing_o = existing_ownerships.get((bucket["person_key"], item.id, variant.color.lower()))
        bucket["display_color"] = "—" if target_color == UNKNOWN_COLOR else target_color
        bucket["action"] = "Update ownership" if existing_o else "Create ownership"
        bucket["is_new_variant"] = variant is None
        bucket["notes_text"] = "; ".join(dict.fromkeys(bucket["notes"])) or None
        if existing_o:
            updated_ownership_rows += 1
        else:
            new_ownership_rows += 1
        rolled_rows.append(bucket)

    merged_rows = [row for row in rolled_rows if row["source_count"] > 1]

    return {
        "unresolved_rows": unresolved_rows,
        "expanded_rows": expanded_rows,
        "rolled_rows": rolled_rows,
        "merged_rows": merged_rows,
        "summary": {
            "rows_received": len(parsed_rows),
            "rows_resolved": len(expanded_rows),
            "set_rows_expanded": set_rows_expanded,
            "duplicate_rows_merged": max(len(expanded_rows) - len(rolled_rows), 0),
            "new_ownership_rows": new_ownership_rows,
            "existing_ownership_rows_updated": updated_ownership_rows,
            "unresolved_skus": len(unresolved_rows),
        },
    }


def _read_confirm_quantity_field(raw_value: str, label: str) -> tuple[int | None, str | None]:
    """Parse a posted ownership quantity field."""
    cleaned = (raw_value or "").strip()
    if not cleaned or cleaned.lower() in {"0", "none", "n/a", "-"}:
        return None, None
    if re.fullmatch(r"\d+", cleaned):
        return int(cleaned), None
    return None, f"{label} must be a whole number."


def _merge_import_ownership(
    ownership: Ownership,
    *,
    status: str,
    notes: str | None = None,
    quantity_purchased: int | None = None,
    quantity_given_away: int | None = None,
) -> None:
    """Update an existing ownership row from an import row."""
    ownership.status = status
    if notes is not None:
        ownership.notes = notes
    if quantity_purchased is not None:
        ownership.quantity_purchased = quantity_purchased
    if quantity_given_away is not None:
        ownership.quantity_given_away = quantity_given_away


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


@data_bp.route("/export")
def export_page():
    suggested_name = f"cutco_collection_{date.today().isoformat()}.csv"
    return render_template("export_page.html", suggested_name=suggested_name)


@data_bp.route("/export/csv")
def export_csv():
    rows = (db.session.query(Ownership, ItemVariant, Item, Person)
            .join(ItemVariant, Ownership.variant_id == ItemVariant.id)
            .join(Item,        ItemVariant.item_id   == Item.id)
            .join(Person,      Ownership.person_id   == Person.id)
            .order_by(Person.name, Item.name, ItemVariant.color).all())

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow([
        "person", "item_name", "sku", "category", "edge_type",
        "color", "status",
        "is_sku_unicorn", "is_variant_unicorn", "is_edge_unicorn",
        "quantity_purchased", "quantity_given_away",
        "notes",
    ])
    for ownership, variant, item, person in rows:
        writer.writerow([
            person.name, item.name, item.sku or "", item.category or "",
            item.edge_type, variant.color, ownership.status,
            "yes" if item.is_unicorn else "no",
            "yes" if variant.is_unicorn else "no",
            "yes" if item.edge_is_unicorn else "no",
            ownership.quantity_purchased if ownership.quantity_purchased is not None else "",
            ownership.quantity_given_away if ownership.quantity_given_away is not None else "",
            ownership.notes or "",
        ])
    csv_buffer.seek(0)
    filename = _safe_csv_filename(request.args.get("filename", "cutco_collection.csv"))
    logger.info("CSV export requested: %d rows (%s)", len(rows), filename)
    return Response(csv_buffer.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename={filename}"})


@data_bp.route("/import/template")
def import_template():
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["name", "sku", "owned", "color", "availability", "quantity purchased",
                     "quantity given away", "category", "edge",
                     "is_sku_unicorn", "is_variant_unicorn", "is_edge_unicorn", "price"])
    writer.writerow(["2-3/4\" Paring Knife", "1720", "Anthony", "Classic Brown", "public", "1",
                     "0", "Kitchen Knives", "Double-D", "no", "no", "no", "12.50"])
    writer.writerow(["Super Shears", "2137", "yes", "Pearl White", "non-catalog", "", "",
                     "Kitchen Knives", "Straight", "no", "no", "no", ""])
    csv_buffer.seek(0)
    return Response(csv_buffer.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=cutco_import_starter.csv"})


@data_bp.route("/import", methods=["GET", "POST"])
@admin_required
def import_page():
    if request.method == "GET":
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all(),
                               import_check=None)

    uploaded_file = request.files.get("csvfile")
    if not uploaded_file or not uploaded_file.filename:
        flash("Please choose a file.", "error")
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all(),
                               import_check=None)

    person_override = request.form.get("person_override", "").strip() or None
    ext = uploaded_file.filename.rsplit(".", 1)[-1].lower()
    logger.info("Import file received: %s (person override: %s)", uploaded_file.filename, person_override or "none")

    if request.form.get("mode") == "check":
        try:
            header_report = _build_import_header_report(uploaded_file, ext)
            if header_report["ok"]:
                flash("Header check passed.", "success")
            else:
                flash("Header check found required column issues.", "warning")
            return render_template(
                "import_page.html",
                people=Person.query.order_by(Person.name).all(),
                import_check=header_report,
            )
        except Exception as exc:
            logger.error("Import header check failed: %s", exc)
            flash("Could not read headers from this file. Use CSV/XLSX with a header row.", "error")
            return render_template(
                "import_page.html",
                people=Person.query.order_by(Person.name).all(),
                import_check=None,
            )

    try:
        if ext == "xlsx":
            wb = openpyxl.load_workbook(io.BytesIO(uploaded_file.stream.read()), data_only=True)
            ws = wb.active
            raw_headers = [str(cell.value).strip() if cell.value is not None else ""
                           for cell in ws[1]]
            norm_rows = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                if all(cell_value is None for cell_value in row):
                    continue
                norm_rows.append({raw_headers[col_idx]: str(cell_value).strip() if cell_value is not None else ""
                                  for col_idx, cell_value in enumerate(row)})
            parsed_rows = []
            for row in norm_rows:
                out_row = {}
                for orig_key, val in row.items():
                    normalized_key = orig_key.strip().lower()
                    if normalized_key in XLSX_COL_MAP:
                        out_row[XLSX_COL_MAP[normalized_key]] = val
                    else:
                        out_row[normalized_key.replace(" ", "_")] = val
                parsed_rows.append(out_row)
        else:
            stream = io.StringIO(uploaded_file.stream.read().decode("utf-8-sig"))
            reader = csv.DictReader(stream)
            parsed_rows = []
            for row in reader:
                out_row = {}
                for orig_key, val in row.items():
                    normalized_key = orig_key.strip().lower()
                    value = val.strip() if val is not None else ""
                    if normalized_key in XLSX_COL_MAP:
                        out_row[XLSX_COL_MAP[normalized_key]] = value
                    else:
                        out_row[normalized_key.replace(" ", "_")] = value
                parsed_rows.append(out_row)

    except Exception as exc:
        logger.error("Import file parse failed: %s", exc)
        flash("Could not parse the uploaded file — check that it is a valid CSV or XLSX.", "error")
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    if person_override:
        for row in parsed_rows:
            row["owned_raw"] = row.get("owned_raw", "yes")
            row["_person_override"] = person_override

    existing_items   = _build_item_sku_lookup(Item.query.all())
    existing_set_skus = _build_set_sku_lookup(Set.query.all())
    existing_names   = {item.name.lower(): item for item in Item.query.all()}
    existing_persons = {person.name.lower(): person for person in Person.query.all()}

    already_in_catalog = []
    sku_name_mismatches = []
    new_items_list     = []
    likely_unicorns    = []
    set_sku_collisions = []
    ownership_entries  = []
    conflicts          = []
    errors             = []
    seen_skus          = set()

    for row_num, row in enumerate(parsed_rows, start=2):
        name       = row.get("name", "").strip()
        sku        = normalize_sku_value(row.get("sku", ""))
        color      = _normalize_import_color(row.get("color", ""))
        availability_raw = row.get("availability", "").strip()
        availability = canonicalize_availability(availability_raw)
        legacy_non_catalog = _parse_truthy_field(row.get("non_catalog", ""))
        edge_type  = row.get("edge_type", "").strip() or "Unknown"
        is_sku_unicorn = row.get("is_sku_unicorn", row.get("item_is_unicorn", "")).strip().lower() in TRUTHY
        is_variant_unicorn = row.get("is_variant_unicorn", "").strip().lower() in TRUTHY
        is_edge_unicorn = row.get("is_edge_unicorn", row.get("edge_is_unicorn", "")).strip().lower() in TRUTHY
        non_catalog = legacy_non_catalog or is_sku_unicorn or is_variant_unicorn or is_edge_unicorn
        if availability == "public" and non_catalog:
            availability = "non-catalog"
        non_catalog = non_catalog or availability != "public"
        category   = canonicalize_category(row.get("category", ""))
        note_text, note_errors = _build_notes(row)
        quantity_purchased, quantity_given_away, quantity_errors = _parse_quantity_fields(row)
        note_errors.extend(quantity_errors)
        if note_errors:
            errors.append({
                "row": row_num,
                "reason": "; ".join(note_errors),
                "data": row,
            })
            continue
        notes      = note_text or row.get("notes", "").strip() or None
        owned_raw = row.get("owned_raw", row.get("status", "yes"))
        status, person_name = _parse_owned_raw(owned_raw, row.get("_person_override") or row.get("person", ""))

        if person_override:
            person_name = person_override

        if not name:
            errors.append({"row": row_num, "reason": "Missing name", "data": row})
            continue

        if status not in STATUS_OPTIONS:
            status = "Owned"

        matched_item = None
        if sku and sku in existing_items:
            matched_item = existing_items[sku]
        elif name.lower() in existing_names:
            matched_item = existing_names[name.lower()]

        matched_set = existing_set_skus.get(sku) if sku else None
        matches_set_sku = bool(sku and matched_set and not matched_item)

        is_cookware = ((matched_item.category or "") in COOKWARE_CATEGORIES) if matched_item else False
        target_color = UNKNOWN_COLOR if is_cookware else color
        existing_variant = None
        if matched_item:
            existing_variant = next(
                (
                    variant
                    for variant in matched_item.variants
                    if variant.color.lower() == target_color.lower()
                ),
                None,
            )

        dedup_key = (sku or name.lower(), color.lower())

        if matched_item:
            is_cookware = (matched_item.category or "") in COOKWARE_CATEGORIES
            already_in_catalog.append({"item": matched_item, "row": row,
                                       "row_num": row_num,
                                       "color": color, "display_color": _preview_import_color(color, is_cookware),
                                       "non_catalog": non_catalog,
                                       "availability": availability,
                                       "availability_label": _availability_preview_fields(availability)[0],
                                       "availability_badge_class": _availability_preview_fields(availability)[1],
                                       "person": person_name,
                                       "status": status})
            already_in_catalog[-1]["row"] = row_num
            if sku and matched_item.name.strip().lower() != name.lower():
                sku_name_mismatches.append({
                    "row": row_num,
                    "import_name": name,
                    "existing_name": matched_item.name,
                    "sku": sku,
                })
            already_in_catalog[-1].update({
                "is_sku_unicorn": is_sku_unicorn,
                "is_variant_unicorn": is_variant_unicorn,
                "is_edge_unicorn": is_edge_unicorn,
            })
        elif dedup_key not in seen_skus:
            seen_skus.add(dedup_key)
            if matches_set_sku:
                bucket = set_sku_collisions
            else:
                bucket = likely_unicorns if is_sku_unicorn or is_variant_unicorn or is_edge_unicorn or not sku else new_items_list
            bucket.append({
                "name": name, "sku": sku, "color": color,
                "display_color": _preview_import_color(color, is_cookware),
                "edge_type": edge_type,
                "non_catalog": non_catalog,
                "availability": availability,
                "availability_label": _availability_preview_fields(availability)[0],
                "availability_badge_class": _availability_preview_fields(availability)[1],
                "is_sku_unicorn": is_sku_unicorn,
                "is_variant_unicorn": is_variant_unicorn,
                "is_edge_unicorn": is_edge_unicorn,
                "quantity_purchased": quantity_purchased,
                "quantity_given_away": quantity_given_away,
                "category": category, "notes": notes,
                "person": person_name, "status": status,
                "row": row_num,
                "matches_set_sku": matches_set_sku,
                "matched_set_name": matched_set.name if matched_set is not None else None,
            })

        if person_name and matched_item:
            person_obj = existing_persons.get(person_name.lower())
            if person_obj:
                if existing_variant:
                    existing_o = Ownership.query.filter_by(
                        person_id=person_obj.id, variant_id=existing_variant.id).first()
                    if existing_o:
                        if existing_o.status != status:
                            conflicts.append({
                                "row": row_num,
                                "person": person_name,
                                "item": matched_item.name,
                                "sku": matched_item.sku,
                                "color": color,
                                "existing_status": existing_o.status,
                                "import_status": status,
                                "oid": existing_o.id,
                            })
                        continue
            ownership_entries.append({
                "row": row_num,
                "person": person_name,
                "item_name": matched_item.name,
                "sku": matched_item.sku,
                "item_id":   matched_item.id,
                "color":     target_color,
                "display_color": _preview_import_color(target_color, is_cookware),
                "status":    status,
                "notes":     notes,
                "non_catalog": non_catalog,
                "availability": matched_item.availability,
                "is_sku_unicorn": is_sku_unicorn,
                "is_variant_unicorn": is_variant_unicorn,
                "is_edge_unicorn": is_edge_unicorn,
                "quantity_purchased": quantity_purchased,
                "quantity_given_away": quantity_given_away,
                "is_new_variant": existing_variant is None,
                "is_new_person": person_name.lower() not in existing_persons,
            })

    return render_template("import_preview.html",
                           already_in_catalog=already_in_catalog,
                           sku_name_mismatches=sku_name_mismatches,
                           new_items=new_items_list,
                           likely_unicorns=likely_unicorns,
                           set_sku_collisions=set_sku_collisions,
                           ownership_entries=ownership_entries,
                           conflicts=conflicts,
                           errors=errors,
                           total_rows=len(parsed_rows),
                           edge_types=EDGE_TYPES,
                           status_options=STATUS_OPTIONS,
                           person_override=person_override)


@data_bp.route("/completion-import", methods=["GET", "POST"])
@admin_required
def completion_import_page():
    recent_completion_imports = (
        db.session.execute(
            db.select(ActivityEvent)
            .filter_by(kind="import")
            .where(ActivityEvent.title == "Completion import complete")
            .order_by(desc(ActivityEvent.occurred_at), desc(ActivityEvent.id))
            .limit(5)
        )
        .scalars()
        .all()
    )
    if request.method == "GET":
        return render_template(
            "completion_import.html",
            people=Person.query.order_by(Person.name).all(),
            recent_completion_imports=[
                {
                    "title": event.title,
                    "details": event.details,
                    "time": format_container_time(event.occurred_at),
                }
                for event in recent_completion_imports
            ],
            preview=None,
        )

    pasted_rows = request.form.get("rows_text", "")
    uploaded_file = request.files.get("csvfile")

    parsed_rows, parse_error = _read_completion_rows(uploaded_file, pasted_rows)
    if parse_error:
        flash(parse_error, "error")
        return render_template(
            "completion_import.html",
            people=Person.query.order_by(Person.name).all(),
            recent_completion_imports=[
                {
                    "title": event.title,
                    "details": event.details,
                    "time": format_container_time(event.occurred_at),
                }
                for event in recent_completion_imports
            ],
            preview=None,
        )

    person_override = request.form.get("person_override", "").strip() or None
    preview = _build_completion_preview(parsed_rows, person_override=person_override)
    return render_template(
        "completion_import_preview.html",
        preview=preview,
        person_override=person_override,
        people=Person.query.order_by(Person.name).all(),
        recent_completion_imports=[
            {
                "title": event.title,
                "details": event.details,
                "time": format_container_time(event.occurred_at),
            }
            for event in recent_completion_imports
        ],
    )


@data_bp.route("/completion-import/confirm", methods=["POST"])
@admin_required
def completion_import_confirm():
    from sqlalchemy.exc import SQLAlchemyError

    existing_persons = {person.name.lower(): person for person in Person.query.all()}
    item_count = int(request.form.get("rolled_count", 0) or 0)
    total_rows = int(request.form.get("total_rows", 0) or 0)
    selected_rows = 0
    processed_rows = 0
    created_ownership = 0
    updated_ownership = 0
    created_people = 0
    skipped_details = []

    try:
        for row_index in range(item_count):
            row_num = request.form.get(f"row_input_{row_index}", type=int)
            person_name = request.form.get(f"row_person_{row_index}", "").strip()
            sku = request.form.get(f"row_sku_{row_index}", "").strip()
            item_name = request.form.get(f"row_item_{row_index}", "").strip() or None

            if request.form.get(f"row_accept_{row_index}") != "on":
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, item_name, sku),
                    "reason": "Not selected during import review.",
                })
                continue
            selected_rows += 1

            item_id = int(request.form.get(f"row_item_id_{row_index}", 0) or 0)
            item = db.session.get(Item, item_id)
            if not item:
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, item_name, sku),
                    "reason": "Matched catalog item was not found during confirmation.",
                })
                continue

            quantity, qty_error = _parse_completion_quantity(request.form.get(f"row_quantity_{row_index}", ""))
            if qty_error:
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, item_name, sku),
                    "reason": qty_error,
                })
                continue

            notes = request.form.get(f"row_note_{row_index}", "").strip() or None
            color = request.form.get(f"row_color_{row_index}", "").strip() or UNKNOWN_COLOR
            target_color = UNKNOWN_COLOR if (item.category or "") in COOKWARE_CATEGORIES else color

            person = existing_persons.get(person_name.lower())
            if not person:
                person = Person(name=person_name)
                db.session.add(person)
                db.session.flush()
                existing_persons[person_name.lower()] = person
                created_people += 1

            variant = next((existing_variant for existing_variant in item.variants
                            if existing_variant.color.lower() == target_color.lower()), None)
            if not variant:
                variant = ItemVariant(item_id=item.id, color=target_color)
                db.session.add(variant)
                db.session.flush()

            existing_o = Ownership.query.filter_by(person_id=person.id, variant_id=variant.id).first()
            if existing_o:
                existing_o.status = "Owned"
                existing_o.quantity_purchased = (existing_o.quantity_purchased or 0) + quantity
                if notes:
                    existing_o.notes = _merge_note_text(existing_o.notes, notes)
                updated_ownership += 1
            else:
                db.session.add(Ownership(
                    person_id=person.id,
                    variant_id=variant.id,
                    status="Owned",
                    quantity_purchased=quantity,
                    notes=notes,
                ))
                created_ownership += 1

            db.session.flush()
            reconcile_unknown_variant(item)
            processed_rows += 1

    except SQLAlchemyError as exc:
        db.session.rollback()
        logger.error("Completion import flush failed: %s", exc)
        flash("Completion import failed — database error during processing. No changes were saved.", "error")
        return redirect(url_for("data.completion_import_page"))

    if db_commit(db.session):
        for idx in range(int(request.form.get("unresolved_count", 0) or 0)):
            skipped_details.append({
                "row": request.form.get(f"unresolved_row_{idx}", type=int),
                "label": _import_row_label(
                    request.form.get(f"unresolved_row_{idx}", type=int),
                    request.form.get(f"unresolved_person_{idx}", "").strip() or None,
                    request.form.get(f"unresolved_sku_{idx}", "").strip() or None,
                ),
                "reason": request.form.get(f"unresolved_reason_{idx}", "").strip() or "Could not resolve row.",
            })

        skipped_details.sort(key=lambda entry: (entry["row"] is None, entry["row"] or 0, entry["label"]))
        if created_ownership > updated_ownership:
            outcome_note = "mostly created new ownership entries"
        elif updated_ownership > created_ownership:
            outcome_note = "mostly updated existing ownership entries"
        else:
            outcome_note = "a balanced mix of new and updated ownership entries"
        summary = (
            f"Completion import complete — processed {processed_rows} row{'s' if processed_rows != 1 else ''}, "
            f"created {created_ownership} ownership entr{'ies' if created_ownership != 1 else 'y'}, "
            f"updated {updated_ownership} ownership entr{'ies' if updated_ownership != 1 else 'y'}; "
            f"{outcome_note}."
        )
        record_activity(
            "import",
            "Completion import complete",
            f"Processed {processed_rows} rows, created {created_ownership} ownership entries, updated {updated_ownership} ownership entries.",
        )
        db.session.commit()
        return render_template(
            "completion_import_result.html",
            summary=summary,
            total_rows=total_rows,
            selected_rows=selected_rows,
            processed_rows=processed_rows,
            skipped_details=skipped_details,
            created_people=created_people,
            created_ownership=created_ownership,
            updated_ownership=updated_ownership,
        )
    return redirect(url_for("data.completion_import_page"))


@data_bp.route("/import/confirm", methods=["POST"])
@admin_required
def import_confirm():
    from sqlalchemy.exc import SQLAlchemyError

    added_items     = 0
    added_ownership = 0
    added_persons   = 0
    item_rows_selected = 0
    item_rows_imported = 0
    own_rows_selected = 0
    own_rows_imported = 0
    skipped_details = []

    existing_items   = _build_item_sku_lookup(Item.query.all())
    existing_names   = {item.name.lower(): item for item in Item.query.all()}
    existing_persons = {person.name.lower(): person for person in Person.query.all()}

    item_count = int(request.form.get("item_count", 0) or 0)
    own_count  = int(request.form.get("own_count",  0) or 0)
    total_rows = int(request.form.get("total_rows", 0) or 0)

    try:
        for row_index in range(item_count):
            row_num = request.form.get(f"item_row_{row_index}", type=int)
            name_hint = request.form.get(f"item_name_{row_index}", "").strip() or None
            sku_hint = normalize_sku_value(request.form.get(f"item_sku_{row_index}", ""))

            if request.form.get(f"item_accept_{row_index}") != "on":
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, name_hint, sku_hint),
                    "reason": "Not selected during import review.",
                })
                continue
            item_rows_selected += 1

            name        = request.form.get(f"item_name_{row_index}", "").strip()
            sku         = normalize_sku_value(request.form.get(f"item_sku_{row_index}", ""))
            color       = _normalize_import_color(request.form.get(f"item_color_{row_index}", ""))
            edge_type   = request.form.get(f"item_edge_{row_index}", "Unknown")
            availability_raw = request.form.get(f"item_availability_{row_index}", "").strip()
            availability_specified = bool(availability_raw)
            availability = canonicalize_availability(availability_raw)
            non_catalog = request.form.get(f"item_non_catalog_{row_index}") == "on"
            is_sku_unicorn = request.form.get(f"item_sku_unicorn_{row_index}") == "on"
            is_variant_unicorn = request.form.get(f"item_variant_unicorn_{row_index}") == "on"
            is_edge_unicorn = request.form.get(f"item_edge_unicorn_{row_index}") == "on"
            quantity_purchased, qty_error = _read_confirm_quantity_field(
                request.form.get(f"item_quantity_purchased_{row_index}", ""),
                "Quantity Purchased",
            )
            if qty_error:
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, name_hint, sku_hint),
                    "reason": qty_error,
                })
                continue
            quantity_given_away, qty_error = _read_confirm_quantity_field(
                request.form.get(f"item_quantity_given_away_{row_index}", ""),
                "Quantity Given Away",
            )
            if qty_error:
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, name_hint, sku_hint),
                    "reason": qty_error,
                })
                continue
            if availability == "public" and (non_catalog or is_sku_unicorn or is_variant_unicorn or is_edge_unicorn):
                availability = "non-catalog"
            non_catalog = non_catalog or availability != "public" or is_sku_unicorn or is_variant_unicorn or is_edge_unicorn
            category    = canonicalize_category(request.form.get(f"item_category_{row_index}", ""))
            notes       = request.form.get(f"item_notes_{row_index}", "").strip() or None
            person_name = request.form.get(f"item_person_{row_index}", "").strip()
            status      = request.form.get(f"item_status_{row_index}", "Owned")

            if not name:
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, name_hint, sku_hint),
                    "reason": "Missing name.",
                })
                continue

            item = None
            if sku and sku in existing_items:
                item = existing_items[sku]
            elif name.lower() in existing_names:
                item = existing_names[name.lower()]

            if not item:
                item = Item(name=name, sku=sku, category=category,
                            edge_type=edge_type, is_unicorn=is_sku_unicorn,
                            edge_is_unicorn=is_edge_unicorn,
                            availability=availability,
                            in_catalog=availability == "public" and not non_catalog, notes=notes)
                db.session.add(item)
                db.session.flush()
                if sku:
                    existing_items[sku] = item
                existing_names[name.lower()] = item
                added_items += 1
            else:
                if availability_specified or non_catalog:
                    item.availability = availability
                    item.in_catalog = availability == "public" and not item.set_only
                if is_sku_unicorn and not item.is_unicorn:
                    item.is_unicorn = True
                if is_edge_unicorn and not item.edge_is_unicorn:
                    item.edge_is_unicorn = True
                if non_catalog:
                    item.in_catalog = False

            is_cookware = (item.category or "") in COOKWARE_CATEGORIES
            target_color = UNKNOWN_COLOR if is_cookware else (color if (color and color != UNKNOWN_COLOR) else UNKNOWN_COLOR)
            variant = next((existing_variant for existing_variant in item.variants
                            if existing_variant.color.lower() == target_color.lower()), None)
            if not variant:
                variant = ItemVariant(item_id=item.id, color=target_color, is_unicorn=is_variant_unicorn)
                db.session.add(variant)
                db.session.flush()
            elif is_variant_unicorn and not variant.is_unicorn:
                variant.is_unicorn = True

            person = None
            if person_name:
                person = existing_persons.get(person_name.lower())
                if not person:
                    person = Person(name=person_name)
                    db.session.add(person)
                    db.session.flush()
                    existing_persons[person_name.lower()] = person
                    added_persons += 1
                existing_o = Ownership.query.filter_by(person_id=person.id, variant_id=variant.id).first()
                if existing_o:
                    if existing_o.status != status:
                        continue
                    _merge_import_ownership(
                        existing_o,
                        status=status,
                        quantity_purchased=quantity_purchased,
                        quantity_given_away=quantity_given_away,
                    )
                else:
                    db.session.add(Ownership(
                        person_id=person.id,
                        variant_id=variant.id,
                        status=status,
                        quantity_purchased=quantity_purchased,
                        quantity_given_away=quantity_given_away,
                    ))
                    added_ownership += 1

            db.session.flush()
            reconcile_unknown_variant(item)
            item_rows_imported += 1

        for row_index in range(own_count):
            row_num = request.form.get(f"own_row_{row_index}", type=int)
            item_name_hint = request.form.get(f"own_item_name_{row_index}", "").strip() or None
            sku_hint = normalize_sku_value(request.form.get(f"own_item_sku_{row_index}", ""))

            if request.form.get(f"own_accept_{row_index}") != "on":
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, item_name_hint, sku_hint),
                    "reason": "Not selected during import review.",
                })
                continue
            own_rows_selected += 1

            item_id     = int(request.form.get(f"own_item_id_{row_index}", 0))
            person_name = request.form.get(f"own_person_{row_index}", "").strip()
            color       = _normalize_import_color(request.form.get(f"own_color_{row_index}", ""))
            status      = request.form.get(f"own_status_{row_index}", "Owned")
            notes       = request.form.get(f"own_notes_{row_index}", "").strip() or None
            quantity_purchased, qty_error = _read_confirm_quantity_field(
                request.form.get(f"own_quantity_purchased_{row_index}", ""),
                "Quantity Purchased",
            )
            if qty_error:
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, item_name_hint, sku_hint),
                    "reason": qty_error,
                })
                continue
            quantity_given_away, qty_error = _read_confirm_quantity_field(
                request.form.get(f"own_quantity_given_away_{row_index}", ""),
                "Quantity Given Away",
            )
            if qty_error:
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, item_name_hint, sku_hint),
                    "reason": qty_error,
                })
                continue
            is_sku_unicorn = request.form.get(f"own_sku_unicorn_{row_index}") == "on"
            is_variant_unicorn = request.form.get(f"own_variant_unicorn_{row_index}") == "on"
            is_edge_unicorn = request.form.get(f"own_edge_unicorn_{row_index}") == "on"

            item = db.session.get(Item, item_id)
            if not item:
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, item_name_hint, sku_hint),
                    "reason": "Matched catalog item was not found during confirmation.",
                })
                continue
            if not person_name:
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, item_name_hint, sku_hint),
                    "reason": "Missing person/collector name.",
                })
                continue
            if is_sku_unicorn and not item.is_unicorn:
                item.is_unicorn = True
            if is_edge_unicorn and not item.edge_is_unicorn:
                item.edge_is_unicorn = True

            person = existing_persons.get(person_name.lower())
            if not person:
                person = Person(name=person_name)
                db.session.add(person)
                db.session.flush()
                existing_persons[person_name.lower()] = person
                added_persons += 1

            target_color = UNKNOWN_COLOR if (item.category or "") in COOKWARE_CATEGORIES else color
            variant = next((existing_variant for existing_variant in item.variants
                            if existing_variant.color.lower() == target_color.lower()), None)
            if not variant:
                variant = ItemVariant(item_id=item.id, color=target_color, is_unicorn=is_variant_unicorn)
                db.session.add(variant)
                db.session.flush()
            elif is_variant_unicorn and not variant.is_unicorn:
                variant.is_unicorn = True

            existing_o = Ownership.query.filter_by(person_id=person.id, variant_id=variant.id).first()
            if existing_o:
                if existing_o.status != status:
                    continue
                _merge_import_ownership(
                    existing_o,
                    status=status,
                    notes=notes,
                    quantity_purchased=quantity_purchased,
                    quantity_given_away=quantity_given_away,
                )
            else:
                db.session.add(Ownership(
                    person_id=person.id,
                    variant_id=variant.id,
                    status=status,
                    notes=notes,
                    quantity_purchased=quantity_purchased,
                    quantity_given_away=quantity_given_away,
                ))
                added_ownership += 1

            db.session.flush()
            reconcile_unknown_variant(item)
            own_rows_imported += 1

    except SQLAlchemyError as exc:
        db.session.rollback()
        logger.error("Import flush failed: %s", exc)
        flash("Import failed — database error during processing. No changes were saved.", "error")
        return redirect(url_for("catalog.catalog"))

    if db_commit(db.session):
        logger.info("Import complete: %d items, %d ownership, %d persons", added_items, added_ownership, added_persons)
        selected_rows = item_rows_selected + own_rows_selected
        imported_rows = item_rows_imported + own_rows_imported
        error_count = int(request.form.get("error_count", 0) or 0)
        for idx in range(error_count):
            row_num = request.form.get(f"error_row_{idx}", type=int)
            name_hint = request.form.get(f"error_name_{idx}", "").strip() or None
            sku_hint = request.form.get(f"error_sku_{idx}", "").strip().upper() or None
            reason = request.form.get(f"error_reason_{idx}", "").strip() or "Could not parse row."
            skipped_details.append({
                "row": row_num,
                "label": _import_row_label(row_num, name_hint, sku_hint),
                "reason": reason,
            })

        conflict_count = int(request.form.get("conflict_count", 0) or 0)
        for idx in range(conflict_count):
            row_num = request.form.get(f"conflict_row_{idx}", type=int)
            item_name = request.form.get(f"conflict_item_{idx}", "").strip() or None
            sku_hint = request.form.get(f"conflict_sku_{idx}", "").strip().upper() or None
            person_name = request.form.get(f"conflict_person_{idx}", "").strip()
            existing_status = request.form.get(f"conflict_existing_status_{idx}", "").strip()
            import_status = request.form.get(f"conflict_import_status_{idx}", "").strip()
            reason = (
                f'Existing entry for {person_name or "collector"} kept unchanged '
                f"({existing_status or 'existing'} vs {import_status or 'import'})."
            )
            skipped_details.append({
                "row": row_num,
                "label": _import_row_label(row_num, item_name, sku_hint),
                "reason": reason,
            })

        skipped_details.sort(key=lambda entry: (entry["row"] is None, entry["row"] or 0, entry["label"]))
        parts = []
        if total_rows:
            parts.append(f"read {total_rows} row{'s' if total_rows != 1 else ''}")
        parts.append(f"selected {selected_rows} row{'s' if selected_rows != 1 else ''}")
        parts.append(f"imported {imported_rows} row{'s' if imported_rows != 1 else ''}")
        if added_items:
            parts.append(f"{added_items} item{'s' if added_items != 1 else ''}")
        if added_persons:
            parts.append(f"{added_persons} collector{'s' if added_persons != 1 else ''}")
        if added_ownership:
            parts.append(f"{added_ownership} ownership entr{'ies' if added_ownership != 1 else 'y'}")
        summary = "Import complete — added " + (", ".join(parts) if parts else "nothing new") + "."
        record_activity(
            "import",
            "Import complete",
            f"Imported {imported_rows} rows, added {added_items} items, {added_persons} collectors, {added_ownership} ownership entries.",
        )
        db.session.commit()
        return render_template(
            "import_result.html",
            summary=summary,
            total_rows=total_rows,
            selected_rows=selected_rows,
            imported_rows=imported_rows,
            skipped_details=skipped_details,
            added_items=added_items,
            added_persons=added_persons,
            added_ownership=added_ownership,
        )
    return redirect(url_for("catalog.catalog"))
