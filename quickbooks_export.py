"""Read-only product identity export for QuickBooks Online's CSV importer.

No ledger values, quantities, account assignments, or customer notes are exported.
The user completes any required item type/account fields in QuickBooks' review.
"""
import csv
import io
from contextlib import closing

from flask import Response, render_template, request
from database import db
from product_collections import catalog_rows, collection_options
from validation import InputError

FORMATS = {'columns', 'legacy', 'identity'}
MAX_ROWS = 1000


def checked_text(value, label, limit):
    value = (value or '').strip()
    if len(value) > limit:
        raise InputError(f'{label}: QuickBooks export allows at most {limit} characters.')
    if value.startswith(('=', '+', '-', '@')) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InputError(f'{label}: remove formula-like prefixes or control characters before exporting.')
    if ':' in value:
        raise InputError(f'{label}: replace the colon before exporting; QuickBooks uses colons for category paths.')
    return value


def encode_quickbooks(rows, export_format='columns'):
    if export_format not in FORMATS:
        raise InputError('Choose a supported QuickBooks export format.')
    if not rows:
        raise InputError('Select at least one active product to export.')
    if len(rows) > MAX_ROWS:
        raise InputError('QuickBooks accepts up to 1,000 products per import. Select fewer products or filter by collection.')
    headers = ['Product/Service Name', 'SKU']
    if export_format == 'columns':
        headers.append('Category')
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    seen = {}
    for product in rows:
        sku = checked_text(product['sku'], 'SKU', 100)
        name = checked_text(product['product_name'], f'{sku} product name', 100 if export_format != 'columns' else 512)
        category = (checked_text(product['category'], f'{sku} collection', 512)
                    if export_format != 'identity' else '')
        if not sku or not name:
            raise InputError('Every exported product needs a SKU and product name.')
        # QuickBooks names are scoped by category. Do not silently change a name
        # or depend on SKU to disambiguate duplicates during creation/import.
        key = (category.casefold(), name.casefold())
        if key in seen:
            raise InputError(f'{sku} and {seen[key]} have the same QuickBooks product name in this category. Rename one or export only the intended item.')
        seen[key] = sku
        if export_format == 'legacy' and category:
            name = f'{category}:{name}'
            if len(name) > 100:
                raise InputError(f'{sku}: the older importer needs the collection and product name together to fit within 100 characters.')
        row = {'Product/Service Name': name, 'SKU': sku}
        if export_format == 'columns':
            row['Category'] = category
        writer.writerow(row)
    # UTF-8 BOM keeps non-ASCII product names legible when opened in Excel.
    return ('\ufeff' + output.getvalue()).encode('utf-8')


def register_quickbooks_export(app):
    @app.route('/products/quickbooks', methods=['GET', 'POST'])
    def quickbooks_export():
        values = request.form if request.method == 'POST' else request.args
        collection = values.get('collection', '').strip()
        query = values.get('q', '').strip()
        export_format = values.get('export_format', 'columns')
        if export_format not in FORMATS:
            raise InputError('Choose a supported QuickBooks export format.')
        with closing(db()) as con:
            rows = catalog_rows(con, query, collection, active_only=True)
            options = collection_options(con)
            if request.method == 'POST':
                selected = set(request.form.getlist('sku'))
                available = {r['sku'] for r in rows}
                if selected - available:
                    raise InputError('A selected product is archived, missing, or outside this filter. Reload the export page and review your selection.')
                payload = encode_quickbooks([r for r in rows if r['sku'] in selected], export_format)
                return Response(payload, mimetype='text/csv', headers={
                    'Content-Disposition': 'attachment; filename=3dprinttally-quickbooks-products.csv',
                })
        return render_template('quickbooks_export.html', products=rows, collections=options,
                               collection=collection, q=query, export_format=export_format)
