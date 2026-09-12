"""Collections reuse the existing category field, keeping catalog identities intact."""

DEFAULT_COLLECTIONS = (
    'Office Organization', 'Kitchen Organization', 'Bathroom Organization',
    'Home Organization', 'Crosses', 'Signs', 'Custom Orders',
)


def collection_key(value):
    return ' '.join((value or '').split()).casefold()


def collection_options(con):
    # Existing spelling wins. Newly typed collections become reusable on save.
    names = {}
    for row in con.execute("SELECT category FROM products ORDER BY created_at, sku"):
        name = (row['category'] or '').strip()
        if name:
            names.setdefault(collection_key(name), name)
    for name in DEFAULT_COLLECTIONS:
        names.setdefault(collection_key(name), name)
    return sorted(names.values(), key=str.casefold)


def canonical_collection(con, value):
    name = ' '.join((value or '').split())
    key = collection_key(name)
    return next((n for n in collection_options(con) if collection_key(n) == key), name)


def catalog_rows(con, query='', collection='', active_only=False):
    rows = con.execute('SELECT * FROM products ORDER BY active DESC, product_name, sku').fetchall()
    query = query.strip().casefold()
    key = collection_key(collection)
    return [r for r in rows
            if (not active_only or r['active'])
            and (not key or collection_key(r['category']) == key)
            and (not query or any(query in (r[field] or '').casefold()
                                  for field in ('sku', 'product_name', 'material', 'category', 'department')))]
