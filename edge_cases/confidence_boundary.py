# Deliberately ambiguous: dynamic SQL-like string built from a value that
# IS validated earlier in the function, several lines away — plausible
# false positive if the agent doesn't trace the validation back far enough.


def search_products(db, category):
    ALLOWED_CATEGORIES = {"electronics", "books", "clothing"}
    if category not in ALLOWED_CATEGORIES:
        raise ValueError("invalid category")
    
    # Plausible SQL injection if parsed out of context, but safe due to allowlist
    query = f"SELECT * FROM products WHERE category = '{category}'"
    return db.execute(query)
