"""Read-only views and public share links."""

from flask import Blueprint, abort, flash, render_template, request, redirect, send_from_directory, url_for

from constants import UNKNOWN_COLOR
from extensions import db
from helpers import admin_required, db_commit, is_authenticated_user, user_required
from helpers import (_collection_token, _gift_token,
                     _verify_collection_token, _verify_gift_token)
from models import Item, ItemAttachment, Person, Set
from blueprints.views_helpers import (
    _attachment_dir,
    _build_collection_card_context,
    _build_gift_list_context,
    _build_item_owners_context,
    _build_matrix_context,
    _build_stats_context,
    _store_attachment,
)

views_bp = Blueprint("views", __name__)


@views_bp.route("/attachments/<int:attachment_id>")
def attachment_file(attachment_id):
    """Serve a stored attachment file."""
    attachment = db.session.get(ItemAttachment, attachment_id)
    if not attachment:
        abort(404)
    storage_dir = _attachment_dir(attachment.item_id)
    file_path = storage_dir / attachment.stored_filename
    if not file_path.exists():
        abort(404)
    return send_from_directory(storage_dir, attachment.stored_filename, as_attachment=False)


@views_bp.route("/views/item/<int:item_id>")
def item_owners(item_id):
    """Render the ownership view for a single item."""
    context = _build_item_owners_context(item_id, private_view=is_authenticated_user())
    item = context["item"]
    if not item:
        abort(404)
    return render_template("item_owners.html", **context)


@views_bp.route("/views/item/<int:item_id>/attachments", methods=["POST"])
@admin_required
def item_attachment_upload(item_id):
    """Upload one image attachment for an item."""
    item = db.session.get(Item, item_id)
    if not item:
        abort(404)

    uploaded_file = request.files.get("attachment")
    if not uploaded_file or not uploaded_file.filename:
        flash("Choose an image to upload.", "warning")
        return redirect(url_for("views.item_owners", item_id=item_id))

    attachment = _store_attachment(item, uploaded_file, request.form.get("caption"))
    if attachment is None:
        flash("That file type is not supported. Please upload a JPG, PNG, GIF, or WEBP image.", "error")
        return redirect(url_for("views.item_owners", item_id=item_id))

    flash(f'Added attachment "{attachment.original_filename}".', "success")
    return redirect(url_for("views.item_owners", item_id=item_id))


@views_bp.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
@admin_required
def attachment_delete(attachment_id):
    """Delete a stored attachment and its backing file."""
    attachment = db.session.get(ItemAttachment, attachment_id)
    if not attachment:
        abort(404)

    item_id = attachment.item_id
    file_path = _attachment_dir(item_id) / attachment.stored_filename
    db.session.delete(attachment)
    if not db_commit(db.session, error_msg="Could not delete attachment."):
        return redirect(url_for("views.item_owners", item_id=item_id))
    if file_path.exists():
        file_path.unlink()
    flash("Attachment deleted.", "info")
    return redirect(url_for("views.item_owners", item_id=item_id))


@views_bp.route("/views/matrix")
@user_required
def matrix():
    """Render the ownership matrix view."""
    sort_field  = (request.args.get("sort", "name") or "name").strip().lower()
    if sort_field not in {"name", "sku"}:
        sort_field = "name"
    sort_dir = (request.args.get("dir", "asc") or "asc").strip().lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "asc"
    context = _build_matrix_context(sort_field, sort_dir)
    return render_template("matrix.html",
                           people=context["people"],
                           items=context["items"],
                           sort_field=sort_field,
                           sort_dir=sort_dir,
                           item_lookup=context["item_lookup"],
                           variant_lookup=context["variant_lookup"],
                           variants_by_item=context["variants_by_item"],
                           UNKNOWN_COLOR=UNKNOWN_COLOR)


@views_bp.route("/stats", strict_slashes=False)
@user_required
def stats():
    """Render collection summary statistics."""
    person_id   = request.args.get("person", type=int)
    private_view = is_authenticated_user()
    context = _build_stats_context(person_id, private_view=private_view)
    return render_template(
        "stats.html",
        people=context["people"],
        person_id=person_id,
        summary=context["summary"],
        public_summary=context["public_summary"],
        cat_data=context["cat_data"],
        val_data=context["val_data"],
        color_data=context["color_data"],
        top_colors=context["top_colors"],
        edge_data=context["edge_data"],
        collector_rows=context["collector_rows"],
        cov_cats=context["cov_cats"],
        cov_owned=context["cov_owned"],
        cov_gap=context["cov_gap"],
    )


# ── Gift list ─────────────────────────────────────────────────────────────────

@views_bp.route("/sets/<int:set_id>/gift-token")
@views_bp.route("/sets/<int:sid>/gift-token")
def gift_token(set_id=None, sid=None):
    """Generate a shareable gift list token."""
    set_id = set_id if set_id is not None else sid
    person_id = request.args.get("person", type=int)
    if not person_id:
        abort(400)
    # Validate both exist
    if not db.session.get(Set, set_id):
        abort(404)
    if not db.session.get(Person, person_id):
        abort(404)
    token = _gift_token(set_id, person_id)
    gift_url = request.host_url.rstrip("/") + f"/gifts/{token}"
    return render_template("gift_share.html", gift_url=gift_url,
                           set_id=set_id, person_id=person_id)


@views_bp.route("/gifts/<token>")
def gift_list(token):
    """Render a public gift list page."""
    ids = _verify_gift_token(token)
    if ids is None:
        abort(404)
    set_id, person_id = ids
    context = _build_gift_list_context(set_id, person_id)
    if context is None:
        abort(404)
    return render_template("gift_list.html",
                           item_set=context["item_set"], person=context["person"],
                           missing_items=context["missing_items"],
                           owned_count=context["owned_count"], total=context["total"], pct=context["pct"])


# ── Collection card ───────────────────────────────────────────────────────────

@views_bp.route("/people/<int:person_id>/collection-token")
def collection_token(person_id):
    """Generate a shareable collection token."""
    person = db.session.get(Person, person_id)
    if not person:
        abort(404)
    token  = _collection_token(person_id)
    card_url = request.host_url.rstrip("/") + f"/collection-card/{token}"
    return render_template("collection_card_share.html", person=person,
                           card_url=card_url, person_id=person_id)


@views_bp.route("/collection-card/<token>")
def collection_card(token):
    """Render a public collection card page."""
    person_id = _verify_collection_token(token)
    if person_id is None:
        abort(404)
    context = _build_collection_card_context(person_id)
    if context is None:
        abort(404)
    return render_template("collection_card.html",
                           person=context["person"],
                           by_category=context["by_category"],
                           owned_count=context["owned_count"],
                           catalog_total=context["catalog_total"],
                           coverage_pct=context["coverage_pct"],
                           total_value=context["total_value"],
                           priced=context["priced"])
