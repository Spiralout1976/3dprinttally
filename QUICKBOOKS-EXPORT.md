# Export products from 3DPrintTally to QuickBooks Online

## Product setup

Create each product in 3DPrintTally with its normal TYPE-NNN SKU, a recognizable name, and a collection. For example, ORG-001 is Pen Holder in Office Organization. A different collection does not restart the ORG sequence, and moving the product does not change its SKU.

The existing Category field is now displayed as Collection. Existing values are retained. Choose a suggested collection or type a new name; names become reusable after saving a product. Each product has one collection. The item-type suggestions include ORG, CRS, and CUST, and other codes can still be entered manually.

## Download the CSV

1. Open **Products → Export for QuickBooks**.
2. Optionally filter by collection or search SKU/product name, then select **Apply filters**.
3. Review the active products. Uncheck any components, custom items, or other entries you do not want in QuickBooks. Archived products are excluded.
4. Choose the layout matching the sample available in your QuickBooks Online import screen.
5. Select **Download QuickBooks CSV**. Each selected product appears once, regardless of how many production parts it has.

Choose at most 1,000 products per download. For larger catalogs, export collections or selected groups in separate files.

## Available layouts

### New: separate category

For the newer importer with a Category field:

```csv
Product/Service Name,SKU,Category
Pen Holder,ORG-001,Office Organization
Utensil Organizer,ORG-002,Kitchen Organization
Toothbrush Holder,ORG-003,Bathroom Organization
```

Map Product/Service Name to the QuickBooks name field, SKU to SKU, and Category to Category. A collection becomes a QuickBooks product category, not an income account or a QuickBooks Class.

### Older: category in name

For the older import workflow that represents categories as part of a product path:

```csv
Product/Service Name,SKU
Office Organization:Pen Holder,ORG-001
Kitchen Organization:Utensil Organizer,ORG-002
```

Map these columns to Product/Service Name and SKU. QuickBooks interprets the colon as a category separator. The export validates the combined name against the older importer's 100-character limit.

### SKU and name only

Use this when you only want product identities and will organize categories in QuickBooks:

```csv
Product/Service Name,SKU
Pen Holder,ORG-001
Utensil Organizer,ORG-002
```

The examples above are illustrative; your download contains your selected saved products. CSV files use UTF-8 with a BOM, with commas/quotes escaped using standard CSV quoting.

## Import into QuickBooks Online

1. Open **Settings → Import data → Products and Services** in QuickBooks Online. The newer workflow may also show **Create products**.
2. Download QuickBooks' sample file to identify the importer layout and any required fields for your item setup.
3. Upload the 3DPrintTally CSV and map the name, SKU, and optional category fields.
4. Complete required **Type**, **Income Account**, and any other item-specific fields in QuickBooks' review screen or in its sample spreadsheet before importing. 3DPrintTally intentionally does not guess your account names or accounting classification.
5. Review the entries and save/import when correct. For later imports, review existing-item matching and overwrite options in your own account; the export has no knowledge of what already exists there.

This is a product identity export. It does not transfer stock counts, opening balances, prices, purchase costs, orders, private notes, or customer details. Choosing to track a product as Inventory in QuickBooks can require additional quantity/date/account information; 3DPrintTally tracks filament grams, not completed-product counts, and cannot supply those figures.

An import is not a continuous sync. The two websites and QuickBooks remain independent. Use the same SKU when manually creating the product on your customer-facing website.

## Export validation

- Every selected row must still be an active product matching the current filter.
- Duplicate product names within an exported category are rejected; SKU alone is not assumed to make a duplicate QuickBooks name safe. In name/SKU-only mode, duplicate names anywhere in the selection are rejected.
- Literal colons are rejected in names/categories because they change QuickBooks category paths. Adjust the saved name or use the name/SKU-only layout if only the collection has a colon.
- Formula-like prefixes and control characters are rejected instead of silently altering product identities.
- SKU length is limited to 100 characters. New-layout name/category fields are limited to 512 characters; identity-only names and older category/name paths use a conservative 100-character limit.

Exporting reads the catalog and does not alter product, job, filament, or accounting records.

## Verification and source

Local regression and browser tests checked CSV contents, selections, filters, escaping, multiple production parts, and limits. No import was performed in your QuickBooks account; its sample template and validation screen remain the final compatibility check.

Import layouts and limits were checked against [Intuit's QuickBooks Online product/service import instructions](https://quickbooks.intuit.com/learn-support/en-us/help-article/list-management/import-products-services-quickbooks-online/L4o3mXx2u_US_en_US) on September 8, 2026.
