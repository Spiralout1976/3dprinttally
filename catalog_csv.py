"""Versioned catalog CSV with stable filament identities and an atomic preview/apply flow."""
import csv
import hashlib
import io
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing
from flask import request, render_template, session, redirect, url_for, flash, Response
from config import DATA_DIR
from database import db
from validation import InputError, number

PRODUCT = {'Product Name':'product_name', 'Material':'material', 'Notes':'notes',
           'Category':'category', 'Department':'department', 'Active':'active',
           'Assembly Labor Min':'assembly_labor_min', 'Actual Selling Price ($)':'actual_selling_price'}
PART = {'Part Name':'part_name', 'Qty per Product':'qty_per_product', 'Qty on Plate':'qty_on_plate',
        'Filament Used (g)':'filament_used_g', 'Filament Cost ($)':'filament_cost',
        'Print Time':'print_time_text', 'Active Labor Min':'active_labor_min',
        'Nozzle Size (mm)':'nozzle_size', 'Layer Height (mm)':'layer_height',
        'STL Filename':'stl_filename', '3MF Filename':'threemf_filename',
        'Part Material':'material', 'Part Notes':'notes', 'Filament Cost Override':'filament_cost_override'}
EXTRA = ['CSV Version', 'Has Part', 'Filament Pool JSON', 'Filament Draws JSON', 'Assembly Components JSON']
NUMERIC = {'qty_per_product','qty_on_plate','filament_used_g','filament_cost','active_labor_min',
           'nozzle_size','layer_height','assembly_labor_min','actual_selling_price'}


def fingerprint(con):
    content = {table:[tuple(r) for r in con.execute(f'SELECT * FROM {table} ORDER BY rowid')]
               for table in ('products','product_parts','assembly_components','filament_types')}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def pool_identity(con, fid):
    if not fid:
        return None
    row = con.execute('SELECT material,color_name,brand FROM filament_types WHERE id=?', (fid,)).fetchone()
    if not row:
        raise InputError('A catalog part references a missing filament pool. Repair its link before export.')
    return dict(row)


def resolve_pool(con, identity):
    if identity is None:
        return None
    if not isinstance(identity, dict) or set(identity) != {'material','color_name','brand'}:
        raise InputError('Filament pool identity must contain material, color_name and brand.')
    if any(not isinstance(v,str) for v in identity.values()):
        raise InputError('Filament identity values must be text.')
    row = con.execute('SELECT id FROM filament_types WHERE material=? AND color_name=? AND brand=?',
                      (identity['material'],identity['color_name'],identity['brand'])).fetchone()
    if not row:
        raise InputError('Missing filament pool: ' + ' / '.join(identity.values()) + '. Add it before importing; links are never silently dropped.')
    return row[0]


def encode_export(con):
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=['SKU', *PRODUCT, *PART, *EXTRA, 'True Cost / Item ($)', 'Suggested Retail ($)'])
    writer.writeheader()
    for product in con.execute('SELECT * FROM products ORDER BY sku').fetchall():
        from app import get_product_parts, get_settings
        from costing import calculate_product_cost
        cost_parts=get_product_parts(con,product['sku'])
        pricing=calculate_product_cost(cost_parts,product['assembly_labor_min'],product['actual_selling_price'],get_settings()) if cost_parts else {}
        components = [dict(r) for r in con.execute('SELECT component_sku,qty,sort_order FROM assembly_components WHERE assembly_sku=? ORDER BY sort_order,id',(product['sku'],))]
        parts = con.execute('SELECT * FROM product_parts WHERE sku=? ORDER BY sort_order,id',(product['sku'],)).fetchall()
        for part in parts or [None]:
            row = {'SKU': product['sku'], **{label:product[key] for label,key in PRODUCT.items()},
                   'CSV Version':'2', 'Has Part':int(part is not None), 'Assembly Components JSON':json.dumps(components),
                   'True Cost / Item ($)':pricing.get('true_cost_per_item',''), 'Suggested Retail ($)':pricing.get('suggested_retail','')}
            if part:
                row.update({label:part[key] for label,key in PART.items()})
                row['Filament Pool JSON'] = json.dumps(pool_identity(con,part['filament_type_id']))
                raw = json.loads(part['filament_draws_json']) if part['filament_draws_json'] else None
                row['Filament Draws JSON'] = json.dumps([
                    {**{k:v for k,v in draw.items() if k != 'filament_type_id'},
                     'pool':pool_identity(con, draw.get('filament_type_id'))} for draw in raw
                ] if raw is not None else None)
            for label, value in row.items():
                if isinstance(value, str) and (
                    value.lstrip().startswith(('=', '+', '-', '@'))
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)
                ):
                    raise InputError(f'{product["sku"]} {label}: remove formula-like prefixes or control characters before CSV export. A full backup preserves the original text.')
            writer.writerow(row)
    return out.getvalue()


def cell(row, label, key):
    aliases = {'SKU':('Generated SKU','sku'), 'Product Name':('product_name','Name')}
    for candidate in (label, key, *aliases.get(label,())):
        if candidate in row:
            return (row[candidate] or '').strip()
    return ''


def parse_json(value, label):
    try:
        return json.loads(value or 'null')
    except (ValueError, TypeError):
        raise InputError(f'{label}: invalid JSON.') from None


def apply_csv(con, text):
    try:
        return _apply_csv(con, text)
    except csv.Error as error:
        raise InputError('Invalid CSV: check quoting and field lengths.') from error


def _apply_csv(con, text):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise InputError('CSV needs a header with unique column names.')
    grouped = {}
    legacy = False
    for n,row in enumerate(reader,2):
        if n > 10001:
            raise InputError('CSV is limited to 10,000 rows.')
        if None in row or any(value is None for value in row.values()):
            raise InputError(f'Row {n}: every row must have exactly the header column count.')
        sku = cell(row,'SKU','sku').upper()
        if not re.fullmatch(r'[A-Z0-9]{2,10}-\d{3,}',sku):
            raise InputError(f'Row {n}: SKU must be TYPE-NNN.')
        if not cell(row,'Product Name','product_name'):
            raise InputError(f'Row {n}: Product Name is required.')
        version = row.get('CSV Version','').strip()
        if version not in ('','2'):
            raise InputError(f'Row {n}: unsupported CSV version.')
        legacy |= version != '2'
        grouped.setdefault(sku,[]).append(row)
    if not grouped:
        raise InputError('CSV has no product rows.')
    stats = {'products':len(grouped),'added':0,'updated':0,'parts':0,'components':0,'legacy':legacy}
    # Insert/update all products before validating assembly links, independent of row order.
    for sku,rows in grouped.items():
        first = rows[0]
        product = {key:cell(first,label,key) for label,key in PRODUCT.items()}
        for row in rows[1:]:
            if any(cell(row,label,key) != cell(first,label,key) for label,key in PRODUCT.items()):
                raise InputError(f'{sku}: repeated rows disagree about product fields.')
        for key in ('assembly_labor_min','actual_selling_price'):
            product[key] = number(product[key], f'{sku} {key}')
        active = product['active'].lower()
        if active not in ('','1','true','yes','active','0','false','no','inactive'):
            raise InputError(f'{sku}: Active must be 0 or 1.')
        product['active'] = int(active in ('','1','true','yes','active'))
        exists = con.execute('SELECT 1 FROM products WHERE sku=?',(sku,)).fetchone()
        if exists:
            stats['updated'] += 1
            # Legacy files may omit columns: preserve omitted product fields.
            if first.get('CSV Version') != '2':
                product = {key:value for key,value in product.items() if any(label in first or key in first for label,k in PRODUCT.items() if k==key)}
            fields = ','.join(f'{k}=?' for k in product)
            con.execute(f'UPDATE products SET {fields},updated_at=CURRENT_TIMESTAMP WHERE sku=?',(*product.values(),sku))
        else:
            stats['added'] += 1
            con.execute(f'INSERT INTO products(sku,{",".join(product)}) VALUES({",".join("?" for _ in range(len(product)+1))})',(sku,*product.values()))
    for sku,rows in grouped.items():
        version = rows[0].get('CSV Version','')
        if any(row.get('CSV Version','') != version for row in rows):
            raise InputError(f'{sku}: do not mix CSV versions for one product.')
        old_parts = [dict(r) for r in con.execute('SELECT * FROM product_parts WHERE sku=? ORDER BY sort_order,id',(sku,))]
        part_rows = [r for r in rows if (r.get('Has Part')=='1' if version=='2' else bool(cell(r,'Qty on Plate','qty_on_plate')))]
        if version=='2' and any(r.get('Has Part') not in ('0','1') for r in rows):
            raise InputError(f'{sku}: Has Part must be 0 or 1.')
        if version=='2' or part_rows:
            parsed=[]
            for index,row in enumerate(part_rows):
                part = {key:cell(row,label,key) for label,key in PART.items()}
                part['part_name'] = part['part_name'] or 'Main'
                for key in NUMERIC.intersection(part):
                    part[key] = number(part[key],f'{sku} {key}', minimum=.000001 if key in ('qty_on_plate','qty_per_product') else 0)
                part['qty_per_product'] = part['qty_per_product'] or 1
                part['filament_cost_override'] = int(number(part['filament_cost_override'] or 0,'Filament override',0,1,True,False))
                if part['print_time_text']:
                    from costing import parse_print_time
                    duration = parse_print_time(part['print_time_text'])
                    number(duration, f'{sku} print time',.000001,525600,optional=False)
                if version=='2':
                    part['filament_type_id'] = resolve_pool(con,parse_json(row.get('Filament Pool JSON'),'Filament pool'))
                    draws = parse_json(row.get('Filament Draws JSON'),'Filament draws')
                    if draws is not None:
                        if not isinstance(draws,list) or len(draws)>32:
                            raise InputError('Filament draws must be a list of at most 32 colors.')
                        mapped=[]
                        for draw in draws:
                            if not isinstance(draw,dict) or 'pool' not in draw:
                                raise InputError('Each filament draw needs a pool identity.')
                            grams = number(draw.get('grams'),'Filament grams',0,1e9,optional=False)
                            fid = resolve_pool(con,draw['pool'])
                            if fid is None:
                                raise InputError('Each filament draw needs an existing pool.')
                            mapped.append({**{k:v for k,v in draw.items() if k!='pool'},'filament_type_id':fid,'grams':grams})
                        part['filament_draws_json'] = json.dumps(mapped)
                    else:
                        part['filament_draws_json'] = None
                else:
                    matches=[p for p in old_parts if p['part_name']==part['part_name']]
                    if len(matches)>1:
                        raise InputError(f'{sku}: ambiguous legacy part names; export a version 2 CSV first.')
                    previous=matches[0] if matches else None
                    if previous:
                        for key in ('filament_type_id','filament_cost_override','filament_draws_json','notes','material'):
                            part[key]=previous[key]
                parsed.append(part)
            if version!='2':
                names={part['part_name'] for part in parsed}
                if any(p['part_name'] not in names and (p['filament_type_id'] or p['filament_draws_json'] or p['filament_cost_override']) for p in old_parts):
                    raise InputError(f'{sku}: legacy import would discard a linked part. Use a version 2 export.')
            con.execute('DELETE FROM product_parts WHERE sku=?',(sku,))
            for index,part in enumerate(parsed):
                con.execute(f'INSERT INTO product_parts(sku,sort_order,{",".join(part)}) VALUES({",".join("?" for _ in range(len(part)+2))})',(sku,index,*part.values()))
                stats['parts']+=1
        if version=='2':
            con.execute('DELETE FROM assembly_components WHERE assembly_sku=?',(sku,))
    for sku,rows in grouped.items():
        if rows[0].get('CSV Version')!='2':
            continue
        components = parse_json(rows[0].get('Assembly Components JSON'),'Assembly components')
        if not isinstance(components,list) or len(components)>1000:
            raise InputError(f'{sku}: components must be a list of at most 1,000 entries.')
        if any(parse_json(r.get('Assembly Components JSON'),'Assembly components') != components for r in rows[1:]):
            raise InputError(f'{sku}: assembly components differ across repeated rows.')
        from app import _add_assembly_component_row
        for component in components:
            if not isinstance(component,dict):
                raise InputError('Invalid assembly component.')
            qty=number(component.get('qty'),'Component quantity',.000001,1e9,optional=False)
            ok,message=_add_assembly_component_row(con,sku,component.get('component_sku',''),qty)
            if not ok:
                raise InputError(message)
            stats['components']+=1
    return stats


def register_catalog_csv(app):
    @app.get('/products/export', endpoint='export_products')
    def export_products():
        with closing(db()) as con:
            text=encode_export(con)
        return Response(text, mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=label-products-v2.csv'})

    @app.post('/products/import', endpoint='import_products')
    def import_products():
        upload=request.files.get('file')
        if not upload or not upload.filename:
            raise InputError('Choose a CSV file.')
        try:
            text=upload.read(8*1024*1024+1).decode('utf-8-sig')
        except UnicodeDecodeError:
            raise InputError('CSV must be UTF-8 encoded.') from None
        if len(text.encode())>8*1024*1024:
            raise InputError('CSV must be 8 MB or smaller.')
        with closing(db()) as con, closing(sqlite3.connect(':memory:')) as preview:
            preview.row_factory=sqlite3.Row
            con.backup(preview); preview.execute('PRAGMA foreign_keys=ON')
            mark=fingerprint(con)
            stats=apply_csv(preview,text)
        folder=DATA_DIR/'.imports'; folder.mkdir(exist_ok=True)
        for old in folder.glob('*.json'):
            if old.stat().st_mtime<time.time()-3600:
                old.unlink()
        token=secrets.token_hex(24)
        (folder/f'{token}.json').write_text(json.dumps({'text':text,'fingerprint':mark,'owner':session['_csrf'],'created':time.time()}),encoding='utf-8')
        return render_template('import_preview.html',stats=stats,token=token)

    @app.post('/products/import/confirm')
    def confirm_import():
        token=request.form.get('import_token','')
        if not re.fullmatch(r'[a-f0-9]{48}',token):
            raise InputError('Import preview expired. Upload the CSV again.')
        path=DATA_DIR/'.imports'/f'{token}.json'
        if not path.exists():
            raise InputError('Import preview was already used or expired.')
        payload=json.loads(path.read_text(encoding='utf-8'))
        if payload['owner']!=session.get('_csrf') or time.time()-payload['created']>3600:
            raise InputError('Import preview expired. Upload the CSV again.')
        with closing(db()) as con:
            con.execute('BEGIN IMMEDIATE')
            try:
                if fingerprint(con)!=payload['fingerprint']:
                    raise InputError('The catalog or filament pools changed after preview. Upload the CSV again to review current changes.')
                stats=apply_csv(con,payload['text'])
                con.commit()
            except Exception:
                con.rollback(); raise
        path.unlink()
        flash(f"Imported {stats['products']} products and {stats['parts']} parts.",'success')
        return redirect(url_for('products'))
