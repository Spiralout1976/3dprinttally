"""Same-origin forms, bounded requests, and optional HTTP Basic authentication."""
import hmac
import logging
import os
import secrets
import time
from urllib.parse import urlsplit
from flask import g, request, session, render_template, jsonify, Response
from filelock import FileLock, Timeout
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash
from config import DATA_DIR, DB_PATH
from validation import InputError, validate_form


def configure_security(app):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # A shared persisted key works across workers/restarts without a shipped secret.
    key = os.environ.get('SECRET_KEY', '')
    if not key or key == 'replace-this-secret':
        with FileLock(str(DATA_DIR / '.secret.lock'), timeout=30):
            path = DATA_DIR / '.session-key'
            if not path.exists():
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as handle:
                    handle.write(secrets.token_hex(32))
            key = path.read_text().strip()
    app.secret_key = key
    app.config.update(
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=512 * 1024,
        MAX_FORM_PARTS=2000,
        MAX_LABELS=3000,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '0') == '1',
    )
    # Validate Host before authentication, including on static/health requests.
    configured_hosts = os.environ.get('TRUSTED_HOSTS', '').strip()
    app.config['TRUSTED_HOSTS'] = list(dict.fromkeys([
        'localhost', '127.0.0.1', '[::1]',
        *[x.strip() for x in configured_hosts.split(',') if x.strip()],
    ]))
    app.config['BASIC_AUTH_USERNAME'] = os.environ.get('BASIC_AUTH_USERNAME', '')
    app.config['BASIC_AUTH_PASSWORD_HASH'] = os.environ.get('BASIC_AUTH_PASSWORD_HASH', '')
    if bool(app.config['BASIC_AUTH_USERNAME']) != bool(app.config['BASIC_AUTH_PASSWORD_HASH']):
        raise RuntimeError('Configure both BASIC_AUTH_USERNAME and BASIC_AUTH_PASSWORD_HASH, or neither.')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')

    def csrf_token():
        if '_csrf' not in session:
            session['_csrf'] = secrets.token_urlsafe(32)
        return session['_csrf']

    @app.context_processor
    def security_context():
        return {'csrf_token': csrf_token, 'csp_nonce': getattr(g, 'csp_nonce', '')}

    @app.before_request
    def protect_request():
        _ = request.host  # Force Flask's trusted-host validation before early responses.
        g.started_at = time.monotonic()
        g.csp_nonce = secrets.token_urlsafe(24)
        g.request_id = secrets.token_hex(8)
        if request.path not in ('/healthz',) and not request.path.startswith('/static/'):
            username, password_hash = app.config['BASIC_AUTH_USERNAME'], app.config['BASIC_AUTH_PASSWORD_HASH']
            if password_hash:
                auth = request.authorization
                if not auth or not hmac.compare_digest((auth.username or '').encode('utf-8'), username.encode('utf-8')) or not check_password_hash(password_hash, auth.password or ''):
                    return Response('Authentication required.', 401, {'WWW-Authenticate': 'Basic realm="3DPrintTally", charset="UTF-8"'})
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            token = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
            if not isinstance(token, str) or not token.isascii() or not token or not hmac.compare_digest(token, session.get('_csrf', '')):
                raise InputError('This form has expired. Reload the page, then submit again.')
            origin = request.headers.get('Origin')
            if origin and (urlsplit(origin).scheme.lower(), urlsplit(origin).netloc.lower()) != (request.scheme, request.host.lower()):
                raise InputError('Submit this form from the same website tab that opened it.')
            validate_form(request.form)
            lock = FileLock(str(DATA_DIR / '.operations.lock'), timeout=30)
            lock.acquire()
            g.write_lock = lock

    @app.teardown_request
    def unlock_request(error):
        lock = g.pop('write_lock', None)
        if lock:
            lock.release()

    @app.after_request
    def response_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data:; font-src 'self'; style-src 'self' 'unsafe-inline'; "
            f"script-src 'self' 'nonce-{getattr(g, 'csp_nonce', '')}'; script-src-attr 'unsafe-inline'"
        )
        if not request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store, private'
        response.headers['X-Request-ID'] = getattr(g, 'request_id', '')
        if response.status_code >= 500:
            app.logger.error('request failed id=%s method=%s path=%s status=%s',
                             getattr(g, 'request_id', ''), request.method, request.path, response.status_code)
        return response

    @app.errorhandler(InputError)
    def invalid_input(error):
        return render_template('request_error.html', message=str(error)), 400

    @app.errorhandler(413)
    def too_large(error):
        return render_template('request_error.html', message='Upload too large. The total request limit is 16 MB.'), 413

    @app.errorhandler(Timeout)
    def busy(error):
        return render_template('request_error.html', message='A backup or another update is running. Please try again shortly.'), 503

    @app.errorhandler(500)
    def failed(error):
        return render_template('request_error.html', message='The request could not be completed. Check the server log using the response request ID.'), 500

    @app.get('/healthz')
    def healthz():
        import sqlite3
        try:
            with sqlite3.connect(f'file:{DB_PATH.as_posix()}?mode=ro', uri=True, timeout=2) as con:
                con.execute('SELECT 1 FROM settings LIMIT 1').fetchone()
            return jsonify(status='ok')
        except sqlite3.Error:
            app.logger.exception('Database health check failed')
            return jsonify(status='unhealthy'), 503
