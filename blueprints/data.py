import csv
import io
import logging
import re
from datetime import date

import openpyxl
from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from constants import (
    COOKWARE_CATEGORIES, EDGE_TYPES, STATUS_OPTIONS, TRUTHY, UNKNOWN_COLOR,
    XLSX_COL_MAP, canonicalize_category,
)
from extensions import db
from helpers import admin_required, db_commit
from models import Item, ItemVariant, Ownership, Person, record_activity, reconcile_unknown_variant

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
    errors: list[str] = []
    for key, label in [
        ("_notes_price",     "Price"),
        ("_notes_gift_box",  "Gift Box"),
        ("_notes_sheath",    "Sheath"),
        ("_notes_qty",       "Quantity Purchased"),
        ("_notes_given_away","Quantity Given Away"),
    ]:
        value = row.get(key, "").strip()
        if value and value not in ("0", "none", "n/a", "-"):
            if key in {"_notes_qty", "_notes_given_away"}:
                parsed_value, error = _parse_whole_number(value, label)
                if error:
                    errors.append(error)
                    continue
                if parsed_value is not None:
                    parts.append(f"{label}: {parsed_value}")
            else:
                parts.append(f"{label}: {value}")
    return ("; ".join(parts) or None), errors


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
        warnings.append("No ownership/status column found (Owned? / status / person). Rows will default to Owned.")
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
        "notes",
    ])
    for ownership, variant, item, person in rows:
        writer.writerow([
            person.name, item.name, item.sku or "", item.category or "",
            item.edge_type, variant.color, ownership.status,
            "yes" if item.is_unicorn else "no",
            "yes" if variant.is_unicorn else "no",
            "yes" if item.edge_is_unicorn else "no",
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
    writer.writerow(["name", "sku", "color", "edge_type",
                     "is_sku_unicorn", "is_variant_unicorn", "is_edge_unicorn",
                     "person", "status", "category", "notes"])
    writer.writerow(["2-3/4\" Paring Knife", "1720", "Classic Brown", "Double-D",
                     "no", "no", "no", "Anthony", "Owned", "Kitchen Knives", ""])
    writer.writerow(["Super Shears", "2137", "Pearl White", "Straight",
                     "no", "no", "no", "Anthony", "Owned", "Kitchen Knives", ""])
    csv_buffer.seek(0)
    return Response(csv_buffer.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=cutco_import_template.csv"})


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

    existing_items   = {item.sku.upper(): item for item in Item.query.filter(Item.sku.isnot(None)).all()}
    existing_names   = {item.name.lower(): item for item in Item.query.all()}
    existing_persons = {person.name.lower(): person for person in Person.query.all()}

    already_in_catalog = []
    sku_name_mismatches = []
    new_items_list     = []
    likely_unicorns    = []
    ownership_entries  = []
    conflicts          = []
    errors             = []
    seen_skus          = set()

    for row_num, row in enumerate(parsed_rows, start=2):
        name       = row.get("name", "").strip()
        sku        = (row.get("sku", "") or "").strip().upper() or None
        color      = row.get("color", "").strip() or UNKNOWN_COLOR
        edge_type  = row.get("edge_type", "").strip() or "Unknown"
        is_sku_unicorn = row.get("is_sku_unicorn", row.get("item_is_unicorn", "")).strip().lower() in TRUTHY
        is_variant_unicorn = row.get("is_variant_unicorn", "").strip().lower() in TRUTHY
        is_edge_unicorn = row.get("is_edge_unicorn", row.get("edge_is_unicorn", "")).strip().lower() in TRUTHY
        category   = canonicalize_category(row.get("category", ""))
        note_text, note_errors = _build_notes(row)
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
            already_in_catalog.append({"item": matched_item, "row": row,
                                       "row_num": row_num,
                                       "color": color, "person": person_name,
                                       "status": status})
            if sku and matched_item.name.strip().lower() != name.lower():
                sku_name_mismatches.append({
                    "row": row_num,
                    "import_name": name,
                    "existing_name": matched_item.name,
                    "sku": sku,
                })
        elif dedup_key not in seen_skus:
            seen_skus.add(dedup_key)
            bucket = likely_unicorns if is_sku_unicorn or is_variant_unicorn or is_edge_unicorn or not sku else new_items_list
            bucket.append({
                "name": name, "sku": sku, "color": color,
                "edge_type": edge_type,
                "is_sku_unicorn": is_sku_unicorn,
                "is_variant_unicorn": is_variant_unicorn,
                "is_edge_unicorn": is_edge_unicorn,
                "category": category, "notes": notes,
                "person": person_name, "status": status,
                "row": row_num,
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
                "status":    status,
                "notes":     notes,
                "is_sku_unicorn": is_sku_unicorn,
                "is_variant_unicorn": is_variant_unicorn,
                "is_edge_unicorn": is_edge_unicorn,
                "is_new_variant": existing_variant is None,
                "is_new_person": person_name.lower() not in existing_persons,
            })

    return render_template("import_preview.html",
                           already_in_catalog=already_in_catalog,
                           sku_name_mismatches=sku_name_mismatches,
                           new_items=new_items_list,
                           likely_unicorns=likely_unicorns,
                           ownership_entries=ownership_entries,
                           conflicts=conflicts,
                           errors=errors,
                           total_rows=len(parsed_rows),
                           edge_types=EDGE_TYPES,
                           status_options=STATUS_OPTIONS,
                           person_override=person_override)


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

    existing_items   = {item.sku.upper(): item for item in Item.query.filter(Item.sku.isnot(None)).all()}
    existing_names   = {item.name.lower(): item for item in Item.query.all()}
    existing_persons = {person.name.lower(): person for person in Person.query.all()}

    item_count = int(request.form.get("item_count", 0) or 0)
    own_count  = int(request.form.get("own_count",  0) or 0)
    total_rows = int(request.form.get("total_rows", 0) or 0)

    try:
        for row_index in range(item_count):
            row_num = request.form.get(f"item_row_{row_index}", type=int)
            name_hint = request.form.get(f"item_name_{row_index}", "").strip() or None
            sku_hint = request.form.get(f"item_sku_{row_index}", "").strip().upper() or None

            if request.form.get(f"item_accept_{row_index}") != "on":
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, name_hint, sku_hint),
                    "reason": "Not selected during import review.",
                })
                continue
            item_rows_selected += 1

            name        = request.form.get(f"item_name_{row_index}", "").strip()
            sku         = request.form.get(f"item_sku_{row_index}", "").strip().upper() or None
            color       = request.form.get(f"item_color_{row_index}", "").strip() or UNKNOWN_COLOR
            edge_type   = request.form.get(f"item_edge_{row_index}", "Unknown")
            is_sku_unicorn = request.form.get(f"item_sku_unicorn_{row_index}") == "on"
            is_variant_unicorn = request.form.get(f"item_variant_unicorn_{row_index}") == "on"
            is_edge_unicorn = request.form.get(f"item_edge_unicorn_{row_index}") == "on"
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
                            in_catalog=bool(sku), notes=notes)
                db.session.add(item)
                db.session.flush()
                if sku:
                    existing_items[sku] = item
                existing_names[name.lower()] = item
                added_items += 1
            else:
                if is_sku_unicorn and not item.is_unicorn:
                    item.is_unicorn = True
                if is_edge_unicorn and not item.edge_is_unicorn:
                    item.edge_is_unicorn = True

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

            if person_name:
                person = existing_persons.get(person_name.lower())
                if not person:
                    person = Person(name=person_name)
                    db.session.add(person)
                    db.session.flush()
                    existing_persons[person_name.lower()] = person
                    added_persons += 1
                if not Ownership.query.filter_by(person_id=person.id,
                                                 variant_id=variant.id).first():
                    db.session.add(Ownership(person_id=person.id,
                                             variant_id=variant.id, status=status))
                    added_ownership += 1

            db.session.flush()
            reconcile_unknown_variant(item)
            item_rows_imported += 1

        for row_index in range(own_count):
            row_num = request.form.get(f"own_row_{row_index}", type=int)
            item_name_hint = request.form.get(f"own_item_name_{row_index}", "").strip() or None
            sku_hint = request.form.get(f"own_item_sku_{row_index}", "").strip().upper() or None

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
            color       = request.form.get(f"own_color_{row_index}", "").strip() or UNKNOWN_COLOR
            status      = request.form.get(f"own_status_{row_index}", "Owned")
            notes       = request.form.get(f"own_notes_{row_index}", "").strip() or None
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

            if not Ownership.query.filter_by(person_id=person.id,
                                              variant_id=variant.id).first():
                db.session.add(Ownership(person_id=person.id,
                                         variant_id=variant.id,
                                         status=status, notes=notes))
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
