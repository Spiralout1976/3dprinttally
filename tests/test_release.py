"""Run with python -m unittest discover -s tests -v. All writes use temporary data."""
import concurrent.futures
import csv
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
TEMP=tempfile.TemporaryDirectory(prefix='3dprinttally-tests-')
os.environ['TALLY_DATA_DIR']=str(Path(TEMP.name)/'data')
os.environ.pop('BASIC_AUTH_USERNAME',None)
os.environ.pop('BASIC_AUTH_PASSWORD_HASH',None)
import app
from config import DB_PATH, PHOTO_DIR, DEFAULT_SETTINGS
from catalog_csv import encode_export, apply_csv
from backup_tools import save_backup, verify_backup, backup_status
from pypdf import PdfReader
from PIL import Image
from werkzeug.security import generate_password_hash

app.app.config['TESTING']=True
app.app.test_client().get('/')
BASE=Path(TEMP.name)/'base.db'
shutil.copyfile(DB_PATH,BASE)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        shutil.copyfile(BASE,DB_PATH)
        for p in PHOTO_DIR.glob('*'):
            if p.is_file():p.unlink()
        app.app.config['BASIC_AUTH_USERNAME']=''
        app.app.config['BASIC_AUTH_PASSWORD_HASH']=''
        self.client=app.app.test_client()
        self.client.get('/settings')
        with self.client.session_transaction() as session:self.token=session['_csrf']
        with closing(app.db()) as con:
            con.execute("INSERT INTO products(sku,product_name,department) VALUES('TST-001','Test','Bakery')")
            con.execute("INSERT INTO product_parts(sku,part_name,qty_per_product,qty_on_plate,filament_used_g,filament_cost,print_time_text) VALUES('TST-001','Main',1,6,60,2,'1h')")
            for fid,brand,cost in [(1,'A',20),(2,'B',30)]:
                con.execute("INSERT INTO filament_types(id,material,color_name,brand) VALUES(?,'PLA','White',?)",(fid,brand))
                con.execute("INSERT INTO filament_transactions(filament_type_id,created_at,kind,grams,cost) VALUES(?,'2026-09-08','purchase',1000,?)",(fid,cost))
            con.commit()

    def post(self,path,data=None,**kwargs):
        return self.client.post(path,data={**(data or {}),'csrf_token':self.token},**kwargs)

    def job(self,quantity='6',**extra):
        base=dict(job_label='Test quote',action='save',order_quantity=quantity,selling_price_per_item='10',
                    cp_name_0='Main',cp_plate_0='6',cp_grams_0='60',cp_cost_0='2',cp_time_0='1h',
                    cp_qty_per_0='1',cp_filp_0_0='1')
        base.update(extra)
        return base

    def test_pages_templates_and_aliases(self):
        for rule in app.app.url_map.iter_rules():
            if 'GET' in rule.methods and not rule.arguments:
                response=self.client.get(rule.rule)
                self.assertEqual(response.status_code,400 if rule.rule=='/api/next-sku' else 200,rule.rule)
                response.close()
        # Any Host header is served when TRUSTED_HOSTS is unset, so a deployment
        # reachable under several names does not need a canonical-host redirect.
        for name in ('localhost','example.invalid','tally.example.test'):
            self.assertEqual(self.client.get('/',base_url='https://'+name).status_code,200)
        for template in (PROJECT/'templates').glob('*.html'):app.app.jinja_env.get_template(template.name)

    def test_csrf_and_auth(self):
        self.assertEqual(self.client.post('/settings',data={'labor_rate':'0'}).status_code,400)
        self.assertEqual(self.post('/settings',{'labor_rate':'20'},headers={'Origin':'https://untrusted.example'}).status_code,400)
        app.app.config.update(BASIC_AUTH_USERNAME='owner',BASIC_AUTH_PASSWORD_HASH=generate_password_hash('audit-password'))
        self.assertEqual(self.client.get('/backup').status_code,401)
        self.assertEqual(self.client.get('/healthz').status_code,200)
        self.assertEqual(self.client.get('/',auth=('owner','audit-password')).status_code,200)

    def test_headers_and_form_tokens(self):
        response=self.client.get('/products')
        self.assertEqual(response.headers['X-Frame-Options'],'DENY')
        self.assertIn('nonce-',response.headers['Content-Security-Policy'])
        self.assertIn('no-store',response.headers['Cache-Control'])
        html=response.get_data(as_text=True)
        self.assertIn('name="csrf_token"',html)

    def test_invalid_input_never_persists(self):
        for path,form in [('/generate',{'start_position':'abc'}),('/generate',{'start_position':'31'}),
                          ('/job-calculator',self.job('nan')),('/job-calculator',self.job('1.5')),
                          ('/settings',{'labor_rate':'abc'}),('/settings',{'scrap_rate':'100'}),
                          ('/design/add',{'minutes':'nan'}),('/filament/1/purchase',{'price_per_spool':'-1'}),
                          ('/job-calculator',{**self.job(),'cp_time_0':'-2h'})]:
            self.assertEqual(self.post(path,form).status_code,400,(path,form))
        with closing(app.db()) as con:
            self.assertEqual(con.execute("SELECT value FROM settings WHERE key='labor_rate'").fetchone()[0],'25')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM job_costs').fetchone()[0],0)

    def test_labels_limits_and_pdf(self):
        for n,start,pages in [(1,1,1),(30,1,1),(31,1,2),(31,30,2)]:
            response=self.post('/generate',{'sku[]':'TST-001','qty[]':'1','copies[]':str(n),'start_position':str(start)})
            self.assertEqual(response.status_code,200)
            pdf=PdfReader(io.BytesIO(response.data))
            self.assertEqual(len(pdf.pages),pages)
            self.assertEqual(list(pdf.pages[0].mediabox),[0,0,612,792])
        self.assertEqual(self.post('/generate',{'sku[]':'TST-001','qty[]':'1','copies[]':'3001'}).status_code,400)
        self.assertEqual(self.client.get('/photos/../labels.db').status_code,302)

    def test_stock_edit_delete_and_frozen_history(self):
        with closing(app.db()) as con:
            stock=lambda:con.execute('SELECT SUM(grams) FROM filament_transactions WHERE filament_type_id=1').fetchone()[0]
            self.assertEqual(stock(),1000)
            self.assertEqual(self.post('/job-calculator',self.job()).status_code,302)
            jid=con.execute('SELECT MAX(id) FROM job_costs').fetchone()[0]
            self.assertEqual(stock(),940)
            saved=con.execute('SELECT payload_json FROM job_costs WHERE id=?',(jid,)).fetchone()[0]
            con.execute("UPDATE product_parts SET filament_cost=500 WHERE sku='TST-001'");con.commit()
            self.client.get('/job-history')
            self.assertEqual(saved,con.execute('SELECT payload_json FROM job_costs WHERE id=?',(jid,)).fetchone()[0])
            self.assertEqual(self.client.get(f'/job-history/{jid}/pdf').status_code,200)
            self.assertEqual(self.post('/job-calculator',self.job('7',edit_job_id=str(jid))).status_code,302)
            self.assertEqual(stock(),880)
            self.assertEqual(self.post(f'/job-history/{jid}/delete').status_code,302)
            self.assertEqual(stock(),1000)

    def test_material_colour_pool_merges_brands_and_allocates_stock(self):
        from filament import pools_for_picker, pooled_stock, record_pooled_movement
        with closing(app.db()) as con:
            con.execute("INSERT INTO filament_types(id,material,color_name,brand) VALUES(10,'PETG','Black','Overture')")
            con.execute("INSERT INTO filament_types(id,material,color_name,brand) VALUES(11,'PETG','Black','Bambu')")
            con.execute("INSERT INTO filament_transactions(filament_type_id,created_at,kind,grams,cost,spools) VALUES(10,'2026-09-08','purchase',1000,17.90,1)")
            con.execute("INSERT INTO filament_transactions(filament_type_id,created_at,kind,grams,cost,spools) VALUES(11,'2026-09-08','purchase',1000,24.99,1)")
            con.commit()
            pools=[p for p in pools_for_picker(con) if p['material']=='PETG' and p['color_name']=='Black']
            self.assertEqual(len(pools),1)
            self.assertEqual(set(pools[0]['brands']),{'Overture','Bambu'})
            self.assertAlmostEqual(pools[0]['cost_per_gram'],(17.90+24.99)/2000,8)
            self.assertEqual(pooled_stock(con,10),2000)
            record_pooled_movement(con,10,'job',-1500,job_cost_id=999,note='pooled test')
            con.commit()
            self.assertEqual(pooled_stock(con,10),500)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM filament_transactions WHERE job_cost_id=999").fetchone()[0],2)
            pooled_job=self.job('1',cp_filp_0_0='10',cp_cost_0='2')
            self.assertEqual(self.post('/job-calculator',pooled_job).status_code,302)
            job_id=con.execute("SELECT MAX(id) FROM job_costs").fetchone()[0]
            self.assertEqual(pooled_stock(con,10),440)
            payload=json.loads(con.execute("SELECT payload_json FROM job_costs WHERE id=?",(job_id,)).fetchone()[0])
            self.assertAlmostEqual(payload['parts_snapshot'][0]['filament_cost'],60*((17.90+24.99)/2000),8)
    def test_cost_invariants_and_scrap_guard(self):
        part={'qty_per_product':1,'qty_on_plate':6,'filament_used_g':60,'filament_cost':2,'print_time_text':'1h','active_labor_min':2}
        for n in (1,6,7,40):
            result=app.calculate_job_cost_from_parts(n,10,0,0,[part],0,DEFAULT_SETTINGS)
            plan=result['parts_plan'][0]
            self.assertAlmostEqual(plan['units_produced']*plan['cost_per_unit'],plan['part_total_cost'])
            self.assertGreaterEqual(plan['units_produced'],n)
        self.assertIn('error',app.calculate_job_cost_from_parts(6,10,0,0,[part],0,{**DEFAULT_SETTINGS,'scrap_rate':'100'}))

    def test_csv_roundtrip_preserves_links_overrides_assemblies(self):
        with closing(app.db()) as con:
            draws=json.dumps([{'filament_type_id':1,'grams':40},{'filament_type_id':2,'grams':20}])
            con.execute("UPDATE product_parts SET filament_type_id=1,filament_cost_override=1,filament_draws_json=?,notes='part note',material='PETG'",(draws,))
            con.execute("INSERT INTO products(sku,product_name) VALUES('KIT-001','Kit')")
            self.assertTrue(app._add_assembly_component_row(con,'KIT-001','TST-001',2)[0]);con.commit()
            before=dict(con.execute("SELECT * FROM product_parts WHERE sku='TST-001'").fetchone())
            text=encode_export(con)
        response=self.post('/products/import',{'file':(io.BytesIO(text.encode()),'catalog.csv')})
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        token=re.search(r'name="import_token" value="([a-f0-9]+)"',response.get_data(as_text=True)).group(1)
        self.assertEqual(self.post('/products/import/confirm',{'import_token':token}).status_code,302)
        self.assertEqual(self.post('/products/import/confirm',{'import_token':token}).status_code,400)
        with closing(app.db()) as con:
            after=dict(con.execute("SELECT * FROM product_parts WHERE sku='TST-001'").fetchone())
            for key in before.keys()-{'id','filament_draws_json'}:self.assertEqual(before[key],after[key],key)
            self.assertEqual(json.loads(before['filament_draws_json']),json.loads(after['filament_draws_json']))
            self.assertEqual(con.execute('SELECT qty FROM assembly_components').fetchone()[0],2)

    def test_csv_preview_stale_and_invalid_atomic(self):
        with closing(app.db()) as con:text=encode_export(con)
        response=self.post('/products/import',{'file':(io.BytesIO(text.encode()),'catalog.csv')})
        token=re.search(r'name="import_token" value="([a-f0-9]+)"',response.get_data(as_text=True)).group(1)
        with closing(app.db()) as con:
            con.execute("UPDATE products SET notes='changed'");con.commit()
        self.assertEqual(self.post('/products/import/confirm',{'import_token':token}).status_code,400)
        bad='SKU,Product Name,Qty on Plate,Print Time\nNEW-001,New,6,1h\nBAD,Invalid,6,1h\n'
        self.assertEqual(self.post('/products/import',{'file':(io.BytesIO(bad.encode()),'bad.csv')}).status_code,400)
        with closing(app.db()) as con:self.assertFalse(con.execute("SELECT 1 FROM products WHERE sku='NEW-001'").fetchone())

    def test_csv_legacy_preserves_link(self):
        with closing(app.db()) as con:
            con.execute('UPDATE product_parts SET filament_type_id=1,filament_cost_override=1');con.commit()
            apply_csv(con,'SKU,Product Name,Qty on Plate,Print Time\nTST-001,Test,6,1h\n')
            row=con.execute('SELECT filament_type_id,filament_cost_override FROM product_parts').fetchone()
            self.assertEqual(tuple(row),(1,1))
            con.rollback()

    def test_csv_pool_identity_maps_different_ids(self):
        with closing(app.db()) as source, closing(sqlite3.connect(':memory:')) as target:
            source.execute('UPDATE product_parts SET filament_type_id=1');source.commit()
            text=encode_export(source)
            source.backup(target);target.row_factory=sqlite3.Row
            target.execute('UPDATE product_parts SET filament_type_id=NULL')
            target.execute('DELETE FROM filament_transactions')
            target.execute('UPDATE filament_types SET id=id+100')
            apply_csv(target,text)
            self.assertEqual(target.execute('SELECT filament_type_id FROM product_parts').fetchone()[0],101)

    def test_job_pdf_wraps_and_paginates(self):
        detail={'order_quantity':1,'parts_snapshot':[
            {'part_name':'A long part description ' * 12,'qty_on_plate':6,'print_time_text':'1h'} for _ in range(60)
        ],'result':{}}
        pdf=PdfReader(app.make_job_cost_pdf('Large job','2026-09-08',detail))
        self.assertGreater(len(pdf.pages),1)
        self.assertIn('Break-Even',pdf.pages[-1].extract_text())

    def test_photo_validation_preserves_old_file(self):
        from photos import save_product_photo
        from werkzeug.datastructures import FileStorage
        image=io.BytesIO();Image.new('RGB',(10,10)).save(image,'PNG');image.seek(0)
        self.assertEqual(save_product_photo('TST-001',FileStorage(image,filename='valid.png')),'TST-001.png')
        before=(PHOTO_DIR/'TST-001.png').read_bytes()
        self.assertEqual(save_product_photo('TST-001',FileStorage(io.BytesIO(b'not an image'),filename='bad.png')),'invalid')
        self.assertEqual((PHOTO_DIR/'TST-001.png').read_bytes(),before)

    def test_backup_restore_and_tamper_detection(self):
        (PHOTO_DIR/'test.png').write_bytes(b'photo fixture')
        with tempfile.TemporaryDirectory() as temp:
            backup=save_backup(Path(temp)/'backups')
            restored=Path(temp)/'restored'
            self.assertTrue(verify_backup(backup,restored))
            self.assertEqual((restored/'product-photos/test.png').read_bytes(),b'photo fixture')
            self.assertTrue(backup_status()['healthy'])
            with self.assertRaises(ValueError):verify_backup(backup,restored)
            corrupt=Path(temp)/'corrupt.zip'
            with zipfile.ZipFile(backup) as source,zipfile.ZipFile(corrupt,'w') as target:
                for name in source.namelist():target.writestr(name,b'bad' if name=='labels.db' else source.read(name))
            with self.assertRaises(ValueError):verify_backup(corrupt)

    def test_concurrent_clean_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            env={**os.environ,'TALLY_DATA_DIR':str(Path(temp)/'fresh')}
            def start(_):
                return subprocess.run([sys.executable,'-c','import app; assert app.app.test_client().get("/").status_code==200'],cwd=PROJECT,env=env,capture_output=True,text=True)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                for result in pool.map(start,range(3)):self.assertEqual(result.returncode,0,result.stderr)

    def test_collections_create_filter_and_move_preserve_sku(self):
        for sku, name, collection in [('ORG-001','Pen Holder','  office   organization '),
                                       ('ORG-002','Toothbrush Holder','Bathroom Organization')]:
            result=self.post('/products', {'sku':sku,'product_name':name,'category':collection})
            self.assertEqual(result.status_code,200)
        with closing(app.db()) as con:
            self.assertEqual(con.execute("SELECT category FROM products WHERE sku='ORG-001'").fetchone()[0], 'Office Organization')
        html=self.client.get('/products',query_string={'collection':'OFFICE organization'}).get_data(as_text=True)
        self.assertIn('Pen Holder',html)
        self.assertNotIn('Toothbrush Holder',html)
        self.assertIn('Toothbrush Holder',self.client.get('/products?q=Bathroom').get_data(as_text=True))
        result=self.post('/products/ORG-001/edit', {'sku':'ORG-001','product_name':'Pen Holder','category':'Craft Room'})
        self.assertEqual(result.status_code,302)
        html=self.client.get('/products?collection=Craft+Room').get_data(as_text=True)
        self.assertIn('Pen Holder',html)
        self.assertIn('<option value="Craft Room">',html)
        self.assertEqual(self.client.get('/api/next-sku?type_code=ORG').json['sku'],'ORG-003')
        self.assertEqual(self.client.get('/api/next-sku?type_code=CUST').json['sku'],'CUST-001')
        with closing(app.db()) as con:
            export=encode_export(con)
            con.execute("UPDATE products SET category='' WHERE sku='ORG-001'")
            apply_csv(con,export)
            self.assertEqual(con.execute("SELECT category FROM products WHERE sku='ORG-001'").fetchone()[0],'Craft Room')

    def test_quickbooks_product_rows_and_roundtrip_text(self):
        from catalog_csv import fingerprint
        with closing(app.db()) as con:
            con.execute('UPDATE products SET product_name=?,category=?,notes=? WHERE sku=?',
                        ('Café holder, "wide"','Office Organization','Private customer details','TST-001'))
            con.execute("INSERT INTO product_parts(sku,part_name) VALUES('TST-001','Second part')")
            con.execute("INSERT INTO products(sku,product_name,active) VALUES('TST-002','Archived',0)")
            con.commit()
            before=fingerprint(con)
        response=self.post('/products/quickbooks', {'sku':['TST-001'],'export_format':'columns'})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.data.startswith(b'\xef\xbb\xbf'))
        self.assertIn('attachment;',response.headers['Content-Disposition'])
        rows=list(csv.DictReader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(rows,[{'Product/Service Name':'Café holder, "wide"','SKU':'TST-001','Category':'Office Organization'}])
        self.assertNotIn(b'Private customer',response.data)
        self.assertNotIn(b'Second part',response.data)
        with closing(app.db()) as con:self.assertEqual(fingerprint(con),before)
        legacy=self.post('/products/quickbooks',{'sku':'TST-001','export_format':'legacy'})
        row=next(csv.DictReader(io.StringIO(legacy.data.decode('utf-8-sig'))))
        self.assertEqual(row,{'Product/Service Name':'Office Organization:Café holder, "wide"','SKU':'TST-001'})
        plain=self.post('/products/quickbooks',{'sku':'TST-001','export_format':'identity'})
        row=next(csv.DictReader(io.StringIO(plain.data.decode('utf-8-sig'))))
        self.assertEqual(row,{'Product/Service Name':'Café holder, "wide"','SKU':'TST-001'})

    def test_quickbooks_selection_filter_and_request_guards(self):
        with closing(app.db()) as con:
            con.execute("UPDATE products SET category='Office Organization'")
            con.execute("INSERT INTO products(sku,product_name,category) VALUES('ORG-002','Kitchen Tray','Kitchen Organization')")
            con.execute("INSERT INTO products(sku,product_name,active) VALUES('ORG-003','Old Tray',0)")
            con.commit()
        html=self.client.get('/products/quickbooks?collection=Office+Organization').get_data(as_text=True)
        self.assertIn('value="TST-001"',html)
        self.assertNotIn('Kitchen Tray',html)
        self.assertNotIn('Old Tray',html)
        for payload in ({}, {'sku':'ORG-003'}, {'sku':'MISSING-001'},
                        {'sku':'ORG-002','collection':'Office Organization'},
                        {'sku':'TST-001','export_format':'bad'}):
            self.assertEqual(self.post('/products/quickbooks',payload).status_code,400,payload)
        self.assertEqual(self.client.post('/products/quickbooks',data={'sku':'TST-001'}).status_code,400)
        self.assertEqual(self.post('/products/quickbooks',{'sku':'TST-001','collection':'Office Organization'}).status_code,200)

    def test_quickbooks_duplicate_and_unsafe_names(self):
        from quickbooks_export import encode_quickbooks
        from validation import InputError
        row={'sku':'ORG-001','product_name':'Tray','category':'Office Organization'}
        duplicate={**row,'sku':'ORG-002','product_name':'TRAY'}
        with self.assertRaisesRegex(InputError,'same QuickBooks product name'):
            encode_quickbooks([row,duplicate])
        different={**duplicate,'category':'Kitchen Organization'}
        self.assertTrue(encode_quickbooks([row,different]))
        with self.assertRaises(InputError):encode_quickbooks([row,different],'identity')
        for name in ('=1+1',' +SUM(1,2)','Line\nBreak','Category:Product'):
            with self.assertRaises(InputError):encode_quickbooks([{**row,'product_name':name}])
        with self.assertRaises(InputError):encode_quickbooks([{**row,'category':'=1+1'}])
        with self.assertRaises(InputError):encode_quickbooks([{**row,'product_name':'x'*513}])
        with self.assertRaises(InputError):encode_quickbooks([{**row,'product_name':'x'*90}],'legacy')

    def test_quickbooks_batch_limit(self):
        from quickbooks_export import encode_quickbooks
        from validation import InputError
        rows=[{'sku':f'ORG-{i:03d}','product_name':f'Organizer {i}','category':''} for i in range(1001)]
        self.assertEqual(len(list(csv.DictReader(io.StringIO(encode_quickbooks(rows[:1000]).decode('utf-8-sig'))))),1000)
        with self.assertRaisesRegex(InputError,'1,000'):encode_quickbooks(rows)

    def test_collection_text_escaped_in_catalog_and_export_preview(self):
        collection='<img src=x onerror="window.collectionXss=1">'
        self.assertEqual(self.post('/products',{'sku':'ORG-001','product_name':'Safe holder','category':collection}).status_code,200)
        for path in ('/products','/products/quickbooks'):
            html=self.client.get(path).get_data(as_text=True)
            self.assertNotIn(collection,html)
            self.assertIn('&lt;img',html)
        html=self.client.get('/job-calculator').get_data(as_text=True)
        self.assertNotIn(collection,html)
        self.assertIn('collectionXss',html)


if __name__=='__main__':unittest.main()
