import csv
import io

import openpyxl
from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from constants import (
    EDGE_TYPES, STATUS_OPTIONS, TRUTHY, UNKNOWN_COLOR,
    XLSX_COL_MAP, XLSX_SET_COLS,
)
from extensions import db
from models import Item, ItemVariant, Ownership, Person, ensure_unknown_variant, get_or_create_set

data_bp = Blueprint("data", __name__)


def _parse_owned_raw(owned_raw: str, default_person: str | None):
    """Parse 'Owned?' cell. Returns (status, person_name)."""
    val = owned_raw.strip()
    if val.lower() in TRUTHY:
        return "Owned", default_person
    if val.lower() in {"no", "n", "false", "0", ""}:
        return "Wishlist", default_person
    return "Owned", val or default_person


def _build_notes(row: dict) -> str | None:
    """Combine spreadsheet auxiliary columns into a single notes string."""
    parts = []
    for key, label in [
        ("_notes_price",     "Price"),
        ("_notes_gift_box",  "Gift Box"),
        ("_notes_sheath",    "Sheath"),
        ("_notes_qty",       "Qty Purchased"),
        ("_notes_given_away","Given Away"),
    ]:
        value = row.get(key, "").strip()
        if value and value not in ("0", "none", "n/a", "-"):
            parts.append(f"{label}: {value}")
    return "; ".join(parts) or None


@data_bp.route("/export/csv")
def export_csv():
    rows = (db.session.query(Ownership, ItemVariant, Item, Person)
            .join(ItemVariant, Ownership.variant_id == ItemVariant.id)
            .join(Item,        ItemVariant.item_id   == Item.id)
            .join(Person,      Ownership.person_id   == Person.id)
            .order_by(Person.name, Item.name, ItemVariant.color).all())

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["person", "item_name", "sku", "category", "edge_type",
                     "color", "status", "is_unicorn", "notes"])
    for ownership, variant, item, person in rows:
        writer.writerow([person.name, item.name, item.sku or "", item.category or "",
                         item.edge_type, variant.color, ownership.status,
                         "yes" if (variant.is_unicorn or item.is_unicorn) else "no", ownership.notes or ""])
    csv_buffer.seek(0)
    return Response(csv_buffer.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=cutco_collection.csv"})


@data_bp.route("/import/template")
def import_template():
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["name", "sku", "color", "edge_type", "is_unicorn",
                     "person", "status", "category", "notes"])
    writer.writerow(["2-3/4\" Paring Knife", "1720", "Classic Brown", "Double-D",
                     "no", "Anthony", "Owned", "Kitchen Knives", ""])
    writer.writerow(["Super Shears", "2137", "Pearl White", "Straight",
                     "no", "Anthony", "Owned", "Kitchen Knives", ""])
    csv_buffer.seek(0)
    return Response(csv_buffer.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=cutco_import_template.csv"})


@data_bp.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "GET":
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    uploaded_file = request.files.get("csvfile")
    if not uploaded_file or not uploaded_file.filename:
        flash("Please choose a file.", "error")
        return render_template("import_page.html",
                               people=Person.query.order_by(Person.name).all())

    person_override = request.form.get("person_override", "").strip() or None
    ext = uploaded_file.filename.rsplit(".", 1)[-1].lower()

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
                set_memberships = []
                for orig_key, val in row.items():
                    normalized_key = orig_key.strip().lower()
                    if normalized_key in XLSX_COL_MAP:
                        out_row[XLSX_COL_MAP[normalized_key]] = val
                    elif normalized_key in XLSX_SET_COLS:
                        if val.strip().lower() in TRUTHY:
                            set_memberships.append(XLSX_SET_COLS[normalized_key])
                    else:
                        out_row[normalized_key.replace(" ", "_")] = val
                out_row["_sets"] = set_memberships
                parsed_rows.append(out_row)
        else:
            stream = io.StringIO(uploaded_file.stream.read().decode("utf-8-sig"))
            reader = csv.DictReader(stream)
            parsed_rows = []
            for row in reader:
                out_row = {k.strip().lower().replace(" ", "_"): v.strip()
                           for k, v in row.items()}
                out_row["_sets"] = []
                parsed_rows.append(out_row)

    except Exception as exc:
        flash(f"Could not parse file: {exc}", "error")
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
        is_unicorn = row.get("is_unicorn", "").strip().lower() in TRUTHY
        category   = row.get("category", "").strip() or None
        notes      = _build_notes(row) or row.get("notes", "").strip() or None
        set_names  = row.get("_sets", [])

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

        dedup_key = (sku or name.lower(), color.lower())

        if matched_item:
            already_in_catalog.append({"item": matched_item, "row": row,
                                       "color": color, "person": person_name,
                                       "status": status, "sets": set_names})
        elif dedup_key not in seen_skus:
            seen_skus.add(dedup_key)
            bucket = likely_unicorns if is_unicorn or not sku else new_items_list
            bucket.append({
                "name": name, "sku": sku, "color": color,
                "edge_type": edge_type, "is_unicorn": is_unicorn,
                "category": category, "notes": notes,
                "person": person_name, "status": status,
                "sets": set_names, "row": row_num,
            })

        if person_name and matched_item:
            person_obj = existing_persons.get(person_name.lower())
            if person_obj:
                variant = next((v for v in matched_item.variants
                                if v.color.lower() == color.lower()), None)
                if variant:
                    existing_o = Ownership.query.filter_by(
                        person_id=person_obj.id, variant_id=variant.id).first()
                    if existing_o:
                        if existing_o.status != status:
                            conflicts.append({
                                "person": person_name,
                                "item": matched_item.name,
                                "color": color,
                                "existing_status": existing_o.status,
                                "import_status": status,
                                "oid": existing_o.id,
                            })
                        continue
            ownership_entries.append({
                "person": person_name,
                "item_name": matched_item.name,
                "item_id":   matched_item.id,
                "color":     color,
                "status":    status,
                "notes":     notes,
                "is_new_person": person_name.lower() not in existing_persons,
            })

    return render_template("import_preview.html",
                           already_in_catalog=already_in_catalog,
                           new_items=new_items_list,
                           likely_unicorns=likely_unicorns,
                           ownership_entries=ownership_entries,
                           conflicts=conflicts,
                           errors=errors,
                           edge_types=EDGE_TYPES,
                           status_options=STATUS_OPTIONS,
                           person_override=person_override)


@data_bp.route("/import/confirm", methods=["POST"])
def import_confirm():
    added_items     = 0
    added_ownership = 0
    added_persons   = 0

    existing_items   = {item.sku.upper(): item for item in Item.query.filter(Item.sku.isnot(None)).all()}
    existing_names   = {item.name.lower(): item for item in Item.query.all()}
    existing_persons = {person.name.lower(): person for person in Person.query.all()}

    item_count = int(request.form.get("item_count", 0))
    for i in range(item_count):
        if request.form.get(f"item_accept_{i}") != "on":
            continue

        name        = request.form.get(f"item_name_{i}", "").strip()
        sku         = request.form.get(f"item_sku_{i}", "").strip().upper() or None
        color       = request.form.get(f"item_color_{i}", "").strip() or UNKNOWN_COLOR
        edge_type   = request.form.get(f"item_edge_{i}", "Unknown")
        is_unicorn  = request.form.get(f"item_unicorn_{i}") == "on"
        category    = request.form.get(f"item_category_{i}", "").strip() or None
        notes       = request.form.get(f"item_notes_{i}", "").strip() or None
        person_name = request.form.get(f"item_person_{i}", "").strip()
        status      = request.form.get(f"item_status_{i}", "Owned")
        set_names   = [sname for sname in request.form.get(f"item_sets_{i}", "").split("|") if sname]

        if not name:
            continue

        item = None
        if sku and sku in existing_items:
            item = existing_items[sku]
        elif name.lower() in existing_names:
            item = existing_names[name.lower()]

        if not item:
            item = Item(name=name, sku=sku, category=category,
                        edge_type=edge_type, is_unicorn=False,
                        in_catalog=bool(sku), notes=notes)
            db.session.add(item)
            db.session.flush()
            ensure_unknown_variant(item)
            if sku:
                existing_items[sku] = item
            existing_names[name.lower()] = item
            added_items += 1

        for sname in set_names:
            item_set = get_or_create_set(sname)
            if item_set not in item.sets:
                item.sets.append(item_set)

        target_color = color if (color and color != UNKNOWN_COLOR) else UNKNOWN_COLOR
        variant = next((v for v in item.variants
                        if v.color.lower() == target_color.lower()), None)
        if not variant:
            variant = ItemVariant(item_id=item.id, color=target_color, is_unicorn=is_unicorn)
            db.session.add(variant)
            db.session.flush()
        elif is_unicorn and not variant.is_unicorn:
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

    own_count = int(request.form.get("own_count", 0))
    for i in range(own_count):
        if request.form.get(f"own_accept_{i}") != "on":
            continue

        item_id     = int(request.form.get(f"own_item_id_{i}", 0))
        person_name = request.form.get(f"own_person_{i}", "").strip()
        color       = request.form.get(f"own_color_{i}", "").strip() or UNKNOWN_COLOR
        status      = request.form.get(f"own_status_{i}", "Owned")
        notes       = request.form.get(f"own_notes_{i}", "").strip() or None

        item = Item.query.get(item_id)
        if not item or not person_name:
            continue

        person = existing_persons.get(person_name.lower())
        if not person:
            person = Person(name=person_name)
            db.session.add(person)
            db.session.flush()
            existing_persons[person_name.lower()] = person
            added_persons += 1

        variant = next((v for v in item.variants
                        if v.color.lower() == color.lower()), None)
        if not variant:
            variant = ItemVariant(item_id=item.id, color=color)
            db.session.add(variant)
            db.session.flush()

        if not Ownership.query.filter_by(person_id=person.id,
                                          variant_id=variant.id).first():
            db.session.add(Ownership(person_id=person.id,
                                     variant_id=variant.id,
                                     status=status, notes=notes))
            added_ownership += 1

    db.session.commit()

    parts = []
    if added_items:
        parts.append(f"{added_items} item{'s' if added_items != 1 else ''}")
    if added_persons:
        parts.append(f"{added_persons} collector{'s' if added_persons != 1 else ''}")
    if added_ownership:
        parts.append(f"{added_ownership} ownership entr{'ies' if added_ownership != 1 else 'y'}")
    flash("Import complete — added " + (", ".join(parts) if parts else "nothing new") + ".", "success")
    return redirect(url_for("catalog.catalog"))
