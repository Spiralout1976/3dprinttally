"""Negative security regression cases. All state comes from disposable test fixtures."""
import base64
import csv
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_release
from test_release import app, DB_PATH, PHOTO_DIR, PROJECT
from backup_tools import verify_backup, full_backup, _copy_bounded
from catalog_csv import encode_export
from validation import InputError
from werkzeug.security import generate_password_hash


class SecurityTests(unittest.TestCase):
    setUp = test_release.ReleaseTests.setUp
    post = test_release.ReleaseTests.post

    def test_host_validation_precedes_auth_and_static(self):
        app.app.config.update(BASIC_AUTH_USERNAME='owner', BASIC_AUTH_PASSWORD_HASH=generate_password_hash('test-password'))
        for path in ('/', '/backup', '/healthz', '/static/style.css', '/source'):
            self.assertEqual(self.client.get(path, base_url='http://evil.example').status_code, 400)

    def test_non_ascii_credentials_and_tokens_are_controlled(self):
        self.assertEqual(self.client.post('/settings', data={'csrf_token': 'é'}).status_code, 400)
        app.app.config.update(BASIC_AUTH_USERNAME='owner', BASIC_AUTH_PASSWORD_HASH=generate_password_hash('test-password'))
        header = 'Basic ' + base64.b64encode('é:test'.encode()).decode()
        self.assertEqual(self.client.get('/', headers={'Authorization': header}).status_code, 401)
        app.app.config['BASIC_AUTH_USERNAME'] = 'propriétaire'
        header = 'Basic ' + base64.b64encode('propriétaire:test-password'.encode()).decode()
        self.assertEqual(self.client.get('/', headers={'Authorization': header}).status_code, 200)

    def test_cross_scheme_origin_rejected(self):
        self.assertEqual(self.post('/settings', headers={'Origin': 'https://localhost'}).status_code, 400)

    def test_short_csv_and_large_field_are_bad_requests(self):
        for data in (b'SKU,Product Name,CSV Version\nTST-002,Test\n', b'SKU,Product Name\nTST-002,' + b'a'*140000):
            self.assertEqual(self.post('/products/import', {'file': (io.BytesIO(data), 'test.csv')}).status_code, 400)
        with closing(app.db()) as con:
            self.assertIsNone(con.execute("SELECT sku FROM products WHERE sku='TST-002'").fetchone())

    def test_formula_prefix_and_control_characters_rejected(self):
        for text in ('=1+1', '+1', '-1', '@SUM(A1)', '  =1+1', '\t=1', '\n=1', 'a\tb', 'a\x7fb'):
            with closing(app.db()) as con:
                con.execute('UPDATE products SET product_name=?', (text,)); con.commit()
                with self.assertRaises(InputError):
                    encode_export(con)
            self.assertEqual(self.client.get('/products/export').status_code, 400)
        with closing(app.db()) as con:
            con.execute("UPDATE products SET product_name='Manual name, unchanged'"); con.commit()
            self.assertEqual(next(csv.DictReader(io.StringIO(encode_export(con))))['Product Name'], 'Manual name, unchanged')

    def test_restore_stream_budget(self):
        with self.assertRaises(ValueError):
            _copy_bounded(io.BytesIO(b'x'*11), io.BytesIO(), 10)

    def test_restore_metadata_budgets_and_images(self):
        import tempfile
        def archive(photo=None):
            data = {'labels.db': DB_PATH.read_bytes()}
            if photo is not None: data['product-photos/test.png'] = photo
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
                for name, content in data.items(): z.writestr(name, content)
                z.writestr('manifest.json', json.dumps({'version': 1, 'files': {name: hashlib.sha256(content).hexdigest() for name,content in data.items()}}))
            buffer.seek(0)
            return buffer
        for limit in ('MAX_MANIFEST_BYTES', 'MAX_DATABASE_BYTES', 'MAX_RESTORE_BYTES'):
            with patch('backup_tools.' + limit, 1), self.assertRaises(ValueError): verify_backup(archive())
        with patch('backup_tools.MAX_PHOTO_BYTES', 1), self.assertRaises(ValueError): verify_backup(archive(b'xx'))
        with self.assertRaises(ValueError): verify_backup(archive(b'0' * (2*1024*1024)))
        self.assertTrue(verify_backup(archive()))

    def test_source_offer_is_complete_and_excludes_runtime_state(self):
        response = self.client.get('/source')
        self.assertEqual(response.status_code, 200)
        with tarfile.open(fileobj=io.BytesIO(response.data)) as archive:
            names = set(archive.getnames())
            self.assertIn('3dprinttally/LICENSE', names)
            self.assertIn('3dprinttally/app.py', names)
            self.assertIn('3dprinttally/tests/test_security.py', names)
            self.assertFalse(any('/data/' in n or n.endswith('/.env') or '/.git/' in n for n in names))
            self.assertEqual(archive.extractfile('3dprinttally/app.py').read(), (PROJECT/'app.py').read_bytes())
        self.assertEqual(response.headers['X-Source-SHA256'], hashlib.sha256(response.data).hexdigest())

    def test_retired_reset_never_changes_database(self):
        before = DB_PATH.read_bytes()
        result = subprocess.run([sys.executable, str(PROJECT/'reset-catalog.py'), '--yes'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'retired', result.stderr)
        self.assertEqual(DB_PATH.read_bytes(), before)

    def test_manual_html_is_escaped(self):
        payload = '<img src=x onerror="window.__injected=1">'
        with closing(app.db()) as con:
            con.execute('UPDATE products SET product_name=?', (payload,)); con.commit()
        page = self.client.get('/products').get_data(as_text=True)
        self.assertNotIn(payload, page)
        self.assertIn('&lt;img', page)
