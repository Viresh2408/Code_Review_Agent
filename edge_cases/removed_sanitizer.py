# removed_sanitizer.py — PART OF THE DIFF
def render_user_bio(bio_text):
    # BUG: sanitization was removed in this diff.
    # Imagine a line `bio_text = escape(bio_text)` was deleted.
    # This leaves downstream callers vulnerable to XSS.
    return bio_text
