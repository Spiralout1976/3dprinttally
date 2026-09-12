"""Offer corresponding source from an explicit release manifest, never runtime data."""
import hashlib
import io
import tarfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from flask import send_file
from config import APP_DIR


@lru_cache(maxsize=1)
def source_distribution():
    names = (APP_DIR / 'SOURCE-MANIFEST.txt').read_text().splitlines()
    if len(names) != len(set(names)):
        raise RuntimeError('Duplicate source manifest entry.')
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name in names:
            relative = PurePosixPath(name)
            if (relative.is_absolute() or '..' in relative.parts
                    or any(part in ('.git', 'data', 'backups', '.venv') for part in relative.parts)
                    or (relative.name.startswith('.env') and name != '.env.example')
                    or relative.suffix in ('.db', '.sqlite', '.key', '.pem')):
                raise RuntimeError('Unsafe source manifest entry.')
            path = APP_DIR / name
            if any(parent.is_symlink() for parent in [path, *path.parents]) or not path.is_file() or not path.resolve().is_relative_to(APP_DIR.resolve()):
                raise RuntimeError('Source manifest entry is missing or unsafe.')
            data = path.read_bytes()
            info = tarfile.TarInfo('3dprinttally/' + name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def register_source_distribution(app):
    # Fail at startup if deployment omitted corresponding source.
    source_distribution()

    @app.get('/source')
    def download_source():
        data = source_distribution()
        response = send_file(io.BytesIO(data), mimetype='application/gzip',
                             as_attachment=True, download_name='3dprinttally-source.tar.gz')
        response.headers['X-Source-SHA256'] = hashlib.sha256(data).hexdigest()
        return response
