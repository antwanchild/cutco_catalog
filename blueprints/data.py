"""Import, export, and completion-sync routes."""

import json
import csv
import io
import logging
from datetime import date

import openpyxl
from flask import Blueprint, Response, flash, redirect, render_template, request, session, url_for
from sqlalchemy.orm import selectinload
from sqlalchemy import desc

from constants import (
    COOKWARE_CATEGORIES, EDGE_TYPES, STATUS_OPTIONS, TRUTHY, UNKNOWN_COLOR,
    XLSX_COL_MAP, canonicalize_availability, canonicalize_category,
)
from extensions import db
from number_utils import parse_positive_whole_number
from blueprints.data_helpers import (
    _availability_preview_fields,
    _build_item_sku_lookup,
    _build_notes,
    _build_set_sku_lookup,
    _match_import_item,
    _merge_note_text,
    _normalize_import_color,
    _parse_owned_raw,
    _parse_truthy_field,
    _preview_import_color,
    _read_completion_rows,
    _resolve_import_variant_color,
)
import blueprints.data_workflows as data_workflows
from blueprints.data_workflows import (
    _add_import_ownership_quantities,
    _build_completion_missing_csv,
    _build_completion_missing_rows,
    _build_completion_preview,
    _build_import_header_report,
    _build_purple_campaign_variant_preview,
    _build_variant_sync_preview,
    _find_import_variant,
    _group_import_rows,
    _import_row_label,
    _merge_import_ownership,
    _parse_quantity_fields,
    _parse_variant_sync_selected_skus,
    _read_confirm_quantity_field,
    _resolve_completion_gap_people,
    _resolve_variant_sync_items,
    _safe_csv_filename,
)
from helpers import admin_required, db_commit
from models import (
    Item,
    ItemVariant,
    Ownership,
    ActivityEvent,
    Person,
    Set,
    normalize_sku_value,
    record_activity,
    reconcile_unknown_variant,
)
from scraping import scrape_item_variant_colors
from scraping import scrape_purple_campaign_variants
from time_utils import format_container_time

data_bp = Blueprint("data", __name__)
logger = logging.getLogger(__name__)


def _sync_variant_sync_helpers() -> None:
    """Keep the workflow helpers pointed at the patchable route-level scrapers."""
    data_workflows.scrape_item_variant_colors = scrape_item_variant_colors
    data_workflows.scrape_purple_campaign_variants = scrape_purple_campaign_variants


@data_bp.route("/export")
@admin_required
def export_page():
    """Render the export page."""
    suggested_name = f"cutco_collection_{date.today().isoformat()}.csv"
    return render_template("export_page.html", suggested_name=suggested_name)


@data_bp.route("/export/csv")
@admin_required
def export_csv():
    """Export the collection as CSV."""
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


@data_bp.route("/completion-gaps", methods=["GET", "POST"])
@admin_required
def completion_gaps_page():
    """Render the completion gaps page."""
    people = Person.query.order_by(Person.name).all()
    public_catalog_count = Item.query.filter_by(set_only=False, in_catalog=True).count()
    last_person_id = session.get("last_person_id")
    default_person_id = last_person_id if any(person.id == last_person_id for person in people) else "all"

    if request.method == "GET":
        selected_person_id = str(request.args.get("person_id") or default_person_id or "all").strip()
        selected_people, selected_person_value, selection_error = _resolve_completion_gap_people(
            selected_person_id, people
        )
        view_mode = (request.args.get("view") or "").strip().lower()
        if selection_error:
            flash(selection_error, "error")
        missing_rows = None
        missing_rows_csv = None
        if view_mode == "screen" and not selection_error:
            missing_rows = _build_completion_missing_rows([person.name for person in selected_people])
            missing_rows_csv = _build_completion_missing_csv(missing_rows)
        return render_template(
            "completion_gaps.html",
            people=people,
            public_catalog_count=public_catalog_count,
            default_person_id=selected_person_value,
            missing_rows=missing_rows,
            missing_rows_csv=missing_rows_csv,
            view_mode=view_mode,
        )

    selected_person_id = str(request.form.get("person_id") or "all").strip()
    selected_people, selected_person_value, selection_error = _resolve_completion_gap_people(
        selected_person_id, people
    )
    if selection_error:
        flash(selection_error, "error")
        return render_template(
            "completion_gaps.html",
            people=people,
            public_catalog_count=public_catalog_count,
            default_person_id=selected_person_value,
            missing_rows=None,
            missing_rows_csv=None,
            view_mode="",
        )

    filename_prefix = "all_collectors" if selected_person_value == "all" else selected_people[0].name or "collector"
    missing_rows = _build_completion_missing_rows([person.name for person in selected_people])
    csv_text = _build_completion_missing_csv(missing_rows)
    filename = _safe_csv_filename(
        f"cutco_completion_gaps_{filename_prefix}_{date.today().isoformat()}.csv"
    )
    logger.info("Completion gaps export requested: %d rows (%s)", len(missing_rows), filename)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@data_bp.route("/variant-sync", methods=["GET", "POST"])
@admin_required
def variant_sync_page():
    """Render the variant sync preview page."""
    _sync_variant_sync_helpers()
    all_items = Item.query.options(selectinload(Item.variants)).filter(Item.cutco_url.isnot(None)).all()
    categories = sorted(
        {item.category for item in all_items if item.category},
        key=lambda value: value.lower(),
    )

    if request.method == "GET":
        return render_template(
            "variant_sync.html",
            categories=categories,
            preview=None,
            scope="all",
            category="",
            selected_skus_text="",
        )

    scope = (request.form.get("scope") or "all").strip().lower()
    category = (request.form.get("category") or "").strip()
    selected_skus_text = request.form.get("selected_skus", "").strip()
    selected_skus = _parse_variant_sync_selected_skus(selected_skus_text)

    # Variant color pages can change over time, so always start with a fresh scrape.
    scrape_item_variant_colors.cache_clear()
    scrape_purple_campaign_variants.cache_clear()
    items, selection_error = _resolve_variant_sync_items(scope, category, selected_skus)
    if selection_error:
        flash(selection_error, "error")
        return render_template(
            "variant_sync.html",
            categories=categories,
            preview=None,
            scope=scope or "all",
            category=category,
            selected_skus_text=selected_skus_text,
        )
    if not items:
        flash("No catalog items with URLs were found for that scope.", "warning")
        return render_template(
            "variant_sync.html",
            categories=categories,
            preview=None,
            scope=scope or "all",
            category=category,
            selected_skus_text=selected_skus_text,
        )

    preview = _build_variant_sync_preview(items)
    promo_preview = _build_purple_campaign_variant_preview()
    preview["promo_items"] = promo_preview.get("items", [])
    preview["promo_summary"] = promo_preview.get("summary", {})
    preview["scope"] = scope
    preview["scope_label"] = {
        "all": "Entire catalog",
        "category": f"Category: {category}",
        "selected": "Selected SKUs",
    }.get(scope, "Entire catalog")
    preview["category"] = category
    preview["selected_skus_text"] = selected_skus_text
    preview_json = json.dumps(preview, ensure_ascii=False)
    return render_template(
        "variant_sync_preview.html",
        preview=preview,
        preview_json=preview_json,
        categories=categories,
        scope=scope,
        category=category,
        selected_skus_text=selected_skus_text,
    )


@data_bp.route("/variant-sync/confirm", methods=["POST"])
@admin_required
def variant_sync_confirm():
    """Apply the variant sync preview."""
    preview_raw = request.form.get("preview_json", "")
    if not preview_raw:
        flash("Variant sync preview data was missing.", "error")
        return redirect(url_for("data.variant_sync_page"))

    try:
        preview = json.loads(preview_raw)
    except json.JSONDecodeError:
        flash("Variant sync preview data could not be read.", "error")
        return redirect(url_for("data.variant_sync_page"))

    created_variants = 0
    retained_variants = 0
    skipped_items = 0
    touched_items = 0
    mark_purple_as_unicorn = request.form.get("mark_purple_variants_unicorn") == "on"
    confirm_target = (request.form.get("confirm_target") or "all").strip()
    skipped_details: list[dict] = []

    try:
        promo_summary = preview.get("promo_summary", {})
        def should_process(item_data: dict, *, section: str) -> bool:
            if confirm_target == "all":
                return True
            if confirm_target == "promo":
                return section == "promo"
            if confirm_target.startswith("category:"):
                return section == "category" and confirm_target == f"category:{item_data.get('category', '')}"
            return True

        def apply_item_preview(item_data: dict, *, allow_purple_unicorn: bool = False) -> None:
            nonlocal created_variants, retained_variants, skipped_items, touched_items
            item_id = item_data.get("item_id")
            if not item_id:
                return
            item = db.session.get(Item, item_id)
            if not item:
                skipped_items += 1
                skipped_details.append({
                    "item": item_data.get("item_name", "Unknown item"),
                    "sku": item_data.get("sku", "—"),
                    "reason": "Item was not found during confirmation.",
                })
                return

            if item_data.get("status") == "skipped":
                skipped_items += 1
                skipped_details.append({
                    "item": item.name,
                    "sku": item.sku or "—",
                    "reason": item_data.get("skip_reason") or "No clear variants were detected.",
                })
                return

            existing_real = {variant.color.lower() for variant in item.variants if variant.color != UNKNOWN_COLOR}
            create_colors = []
            for color in item_data.get("create_colors", []):
                color_value = (color or "").strip()
                if not color_value:
                    continue
                if color_value.lower() in existing_real:
                    retained_variants += 1
                    continue
                variant = ItemVariant(item=item, color=color_value, source="variant_sync")
                if allow_purple_unicorn and mark_purple_as_unicorn and color_value.lower().startswith("purple"):
                    variant.is_unicorn = True
                db.session.add(variant)
                create_colors.append(color_value)
                created_variants += 1
            if allow_purple_unicorn:
                for variant in item.variants:
                    if variant.color.lower().startswith("purple"):
                        if mark_purple_as_unicorn:
                            variant.is_unicorn = True
            retained_variants += len(item_data.get("retained_colors", []))
            if create_colors or item_data.get("retained_colors"):
                touched_items += 1
                db.session.flush()
                reconcile_unknown_variant(item)

        for item_data in preview.get("items", []):
            if should_process(item_data, section="category"):
                apply_item_preview(item_data)
        for item_data in preview.get("promo_items", []):
            if should_process(item_data, section="promo"):
                apply_item_preview(item_data, allow_purple_unicorn=True)

        combined_summary = dict(preview.get("summary", {}))
        for key in (
            "items_scanned",
            "variants_found",
            "variants_to_create",
            "variants_retained",
            "items_with_no_clear_variants",
            "purple_variant_count",
        ):
            combined_summary[key] = combined_summary.get(key, 0) + promo_summary.get(key, 0)
        combined_summary["has_purple_variants"] = combined_summary.get("purple_variant_count", 0) > 0

        record_activity(
            "sync",
            "Variant sync complete",
            (
                f"Items scanned {combined_summary.get('items_scanned', 0)}, "
                f"variants created {created_variants}, retained {retained_variants}, "
                f"skipped {skipped_items}."
            ),
        )
        if db_commit(db.session):
            return render_template(
                "variant_sync_result.html",
                summary=combined_summary,
                created_variants=created_variants,
                retained_variants=retained_variants,
                skipped_items=skipped_items,
                touched_items=touched_items,
                skipped_details=skipped_details,
                scope_label=preview.get("scope_label", "Entire catalog"),
            )
    except Exception as exc:
        db.session.rollback()
        logger.error("Variant sync failed: %s", exc)
        flash("Variant sync failed — no changes were saved.", "error")
        return redirect(url_for("data.variant_sync_page"))

    return redirect(url_for("data.variant_sync_page"))


@data_bp.route("/import/template")
@admin_required
def import_template():
    """Download a starter CSV template for imports."""
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
    """Render the import page."""
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

        matched_item = _match_import_item(
            existing_items=existing_items,
            existing_names=existing_names,
            sku=sku,
            name=name,
        )

        matched_set = existing_set_skus.get(sku) if sku else None
        matches_set_sku = bool(sku and matched_set and not matched_item)

        item_category_for_color = (matched_item.category if matched_item else category) or ""
        target_color = _resolve_import_variant_color(name, item_category_for_color, color)
        is_cookware = item_category_for_color in COOKWARE_CATEGORIES
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

        if matched_item:
            already_in_catalog.append({"item": matched_item, "row": row,
                                       "row_num": row_num,
                                       "color": target_color, "display_color": _preview_import_color(target_color, is_cookware),
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
        else:
            if matches_set_sku:
                bucket = set_sku_collisions
            else:
                bucket = likely_unicorns if is_sku_unicorn or is_variant_unicorn or is_edge_unicorn or not sku else new_items_list
            bucket.append({
                "name": name, "sku": sku, "color": target_color,
                "display_color": _preview_import_color(target_color, is_cookware),
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

    return render_template(
        "import_preview.html",
        already_in_catalog=already_in_catalog,
        sku_name_mismatches=sku_name_mismatches,
        new_items=new_items_list,
        new_item_groups=_group_import_rows(new_items_list, base_index=0),
        likely_unicorns=likely_unicorns,
        likely_unicorn_groups=_group_import_rows(likely_unicorns, base_index=len(new_items_list)),
        set_sku_collisions=set_sku_collisions,
        set_sku_collision_groups=_group_import_rows(
            set_sku_collisions,
            base_index=len(new_items_list) + len(likely_unicorns),
        ),
        ownership_entries=ownership_entries,
        ownership_groups=_group_import_rows(ownership_entries, base_index=0),
        conflicts=conflicts,
        errors=errors,
        total_rows=len(parsed_rows),
        item_rows_total=len(new_items_list) + len(likely_unicorns) + len(set_sku_collisions),
        edge_types=EDGE_TYPES,
        status_options=STATUS_OPTIONS,
        person_override=person_override,
    )


@data_bp.route("/completion-import", methods=["GET", "POST"])
@admin_required
def completion_import_page():
    """Render the completion import page."""
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
            export_name=f"cutco_completion_result_{date.today().isoformat()}.csv",
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
            export_name=f"cutco_completion_result_{date.today().isoformat()}.csv",
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
        export_name=f"cutco_completion_result_{date.today().isoformat()}.csv",
    )


@data_bp.route("/completion-import/export", methods=["POST"])
@admin_required
def completion_import_export():
    """Export completion import rows as CSV."""
    export_count = int(request.form.get("export_count", 0) or 0)
    rows = []
    for idx in range(export_count):
        rows.append({
            "person": request.form.get(f"export_person_{idx}", "").strip(),
            "sku": request.form.get(f"export_sku_{idx}", "").strip(),
            "item": request.form.get(f"export_item_{idx}", "").strip(),
            "color": request.form.get(f"export_display_color_{idx}", "").strip() or "—",
            "quantity": request.form.get(f"export_quantity_{idx}", "").strip(),
            "action": request.form.get(f"export_action_{idx}", "").strip(),
            "notes": request.form.get(f"export_note_{idx}", "").strip(),
            "source_rows": request.form.get(f"export_source_rows_{idx}", "").strip(),
        })

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["person", "sku", "item", "color", "total_quantity", "action", "notes", "source_rows"])
    for row in rows:
        writer.writerow([
            row["person"],
            row["sku"],
            row["item"],
            row["color"],
            row["quantity"],
            row["action"],
            row["notes"],
            row["source_rows"],
        ])
    csv_buffer.seek(0)
    filename = _safe_csv_filename(request.form.get("filename", f"cutco_completion_result_{date.today().isoformat()}.csv"))
    logger.info("Completion export requested: %d rows (%s)", len(rows), filename)
    return Response(
        csv_buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@data_bp.route("/completion-import/missing-export", methods=["POST"])
@admin_required
def completion_import_missing_export():
    """Export unresolved completion rows as CSV."""
    export_count = int(request.form.get("export_count", 0) or 0)
    rows = []
    for idx in range(export_count):
        rows.append({
            "person": request.form.get(f"missing_person_{idx}", "").strip(),
            "missing_sku": request.form.get(f"missing_sku_{idx}", "").strip(),
            "item": request.form.get(f"missing_item_{idx}", "").strip(),
            "category": request.form.get(f"missing_category_{idx}", "").strip() or "—",
            "availability": request.form.get(f"missing_availability_{idx}", "").strip() or "public",
        })

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["person", "missing_sku", "item", "category", "availability"])
    for row in rows:
        writer.writerow([
            row["person"],
            row["missing_sku"],
            row["item"],
            row["category"],
            row["availability"],
        ])
    csv_buffer.seek(0)
    filename = _safe_csv_filename(request.form.get("filename", f"cutco_completion_missing_{date.today().isoformat()}.csv"))
    logger.info("Completion missing export requested: %d rows (%s)", len(rows), filename)
    return Response(
        csv_buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@data_bp.route("/completion-import/confirm", methods=["POST"])
@admin_required
def completion_import_confirm():
    """Apply a completion import preview."""
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
    export_rows = []

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

            quantity, qty_error = parse_positive_whole_number(request.form.get(f"row_quantity_{row_index}", ""))
            if qty_error:
                skipped_details.append({
                    "row": row_num,
                    "label": _import_row_label(row_num, item_name, sku),
                    "reason": qty_error,
                })
                continue

            notes = request.form.get(f"row_note_{row_index}", "").strip() or None
            color = request.form.get(f"row_color_{row_index}", "").strip() or UNKNOWN_COLOR
            target_color = color if color and color != UNKNOWN_COLOR else UNKNOWN_COLOR

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
                variant = ItemVariant(item_id=item.id, color=target_color, source="collection_import")
                db.session.add(variant)
                db.session.flush()

            existing_o = Ownership.query.filter_by(person_id=person.id, variant_id=variant.id).first()
            if existing_o:
                existing_o.status = "Owned"
                existing_o.quantity_purchased = (existing_o.quantity_purchased or 0) + quantity
                if notes:
                    existing_o.notes = _merge_note_text(existing_o.notes, notes)
                updated_ownership += 1
                action = "Update ownership"
            else:
                db.session.add(Ownership(
                    person_id=person.id,
                    variant_id=variant.id,
                    status="Owned",
                    quantity_purchased=quantity,
                    notes=notes,
                ))
                created_ownership += 1
                action = "Create ownership"

            db.session.flush()
            reconcile_unknown_variant(item)
            processed_rows += 1
            export_rows.append({
                "person": person.name,
                "sku": item.sku or sku,
                "item": item.name,
                "display_color": "—" if target_color == UNKNOWN_COLOR else target_color,
                "quantity": quantity,
                "action": action,
                "notes": notes or "",
                "source_rows": str(row_num) if row_num is not None else "",
            })

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
        missing_export_rows = _build_completion_missing_rows([row["person"] for row in export_rows])
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
            export_rows=export_rows,
            export_name=f"cutco_completion_result_{date.today().isoformat()}.csv",
            missing_export_rows=missing_export_rows,
            missing_export_name=f"cutco_completion_missing_{date.today().isoformat()}.csv",
            missing_people_count=len({row["person"] for row in missing_export_rows}),
            missing_catalog_items_count=len(Item.query.filter_by(set_only=False, in_catalog=True).all()),
        )
    return redirect(url_for("data.completion_import_page"))


@data_bp.route("/import/confirm", methods=["POST"])
@admin_required
def import_confirm():
    """Apply an item and ownership import preview."""
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

            item = _match_import_item(
                existing_items=existing_items,
                existing_names=existing_names,
                sku=sku,
                name=name,
            )

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

            target_color = _resolve_import_variant_color(item.name, item.category or category, color)
            variant = _find_import_variant(item, target_color)
            if not variant:
                variant = ItemVariant(item_id=item.id, color=target_color, is_unicorn=is_variant_unicorn, source="catalog_sync")
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
                    _add_import_ownership_quantities(
                        existing_o,
                        status=status,
                        notes=existing_o.notes,
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

            target_color = _resolve_import_variant_color(item.name, item.category or "", color)
            variant = _find_import_variant(item, target_color)
            if not variant:
                variant = ItemVariant(item_id=item.id, color=target_color, is_unicorn=is_variant_unicorn, source="collection_import")
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
