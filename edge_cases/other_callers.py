# other_callers.py — UNCHANGED FILE (exists for blast-radius context)
from edge_cases.removed_sanitizer import render_user_bio


def profile_page(user):
    return f"<div>{render_user_bio(user.bio)}</div>"


def admin_preview(user):
    return f"<div class='preview'>{render_user_bio(user.bio)}</div>"


def api_export(user):
    return {"bio_html": render_user_bio(user.bio)}
