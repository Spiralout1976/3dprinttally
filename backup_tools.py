"""Consistent SQLite snapshots, photo-inclusive backups, and safe restore verification."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from filelock import FileLock
from config import DATA_DIR, DB_PATH, PHOTO_DIR


def database_snapshot():
    buffer = tempfile.SpooledTemporaryFile(max_size=8*1024*1024)
    with FileLock(str(DATA_DIR/'.operations.lock'),timeout=60), tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'labels.db'
        with closing(sqlite3.connect(DB_PATH)) as source, closing(sqlite3.connect(path)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('Database backup integrity check failed.')
        with path.open('rb') as handle:
            shutil.copyfileobj(handle,buffer)
    buffer.seek(0)
    return buffer


def full_backup():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    output=tempfile.SpooledTemporaryFile(max_size=8*1024*1024)
    # All application mutations use this same cross-process lock, so the database
    # and photo set refer to one point in time even with multiple workers.
    with FileLock(str(DATA_DIR/'.operations.lock'),timeout=60), tempfile.TemporaryDirectory() as temp:
        snapshot=Path(temp)/'labels.db'
        with closing(sqlite3.connect(DB_PATH)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('Database backup integrity check failed.')
        files=[('labels.db',snapshot)]
        if PHOTO_DIR.exists():
            files.extend((f'product-photos/{p.name}',p) for p in PHOTO_DIR.iterdir() if p.is_file() and not p.is_symlink() and not p.name.startswith('.'))
        manifest={'version':1,'created_at':datetime.now(timezone.utc).isoformat(),'files':{}}
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,path in files:
                with path.open('rb') as handle:
                    manifest['files'][name]=hashlib.file_digest(handle,'sha256').hexdigest()
                archive.write(path,name)
            archive.writestr('manifest.json',json.dumps(manifest,indent=2))
    output.seek(0)
    return output


def backup_status():
    try:
        status=json.loads((DATA_DIR/'.backup-status.json').read_text())
        status['age_hours']=round((time.time()-status['timestamp'])/3600,1)
        status['healthy']=status['age_hours']<=36
        return status
    except (OSError,ValueError,KeyError):
        return {'healthy':False,'last_success':None,'age_hours':None}


def save_backup(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path=directory/f'3dprinttally-{stamp}.zip'
    temp=path.with_suffix('.partial')
    try:
        with full_backup() as data, temp.open('wb') as handle:
            shutil.copyfileobj(data,handle)
        verify_backup(temp)
        os.replace(temp,path)
        status={'timestamp':time.time(),'last_success':datetime.now(timezone.utc).isoformat(),'filename':path.name}
        status_file=DATA_DIR/'.backup-status.json'
        status_temp=status_file.with_suffix('.tmp')
        status_temp.write_text(json.dumps(status));os.replace(status_temp,status_file)
        # Retain the newest 30 successfully completed backups created by this tool.
        for old in sorted(directory.glob('3dprinttally-*.zip'),reverse=True)[30:]:
            if old.is_file() and not old.is_symlink():old.unlink()
        return path
    finally:
        if temp.exists():temp.unlink()


# Fixed resource budgets for untrusted restore inputs. Total includes the database.
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BACKUP_ENTRIES = 10000
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_PHOTO_BYTES = 32 * 1024 * 1024
MAX_RESTORE_BYTES = 512 * 1024 * 1024


def _copy_bounded(source, output, limit):
    written = 0
    while True:
        chunk = source.read(min(64 * 1024, limit - written + 1))
        if not chunk:
            return written
        written += len(chunk)
        if written > limit:
            raise ValueError('Backup exceeds its decompressed size limit.')
        output.write(chunk)


def _verify_photo(path):
    import warnings
    from PIL import Image, UnidentifiedImageError
    formats = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.webp': 'WEBP'}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format != formats.get(path.suffix.lower()) or image.width * image.height > 20_000_000:
                    raise ValueError('Backup contains an invalid or oversized photo.')
                image.verify()
            with Image.open(path) as image:
                image.load()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError('Backup contains an invalid photo.') from error


def verify_backup(path, restore_to=None):
    """Verify a bounded archive in staging; never overwrite a live directory.

    Checksums prove consistency, not provenance. Only restore trusted backups.
    """
    destination = Path(restore_to).resolve() if restore_to else None
    if destination and destination.exists():
        raise ValueError('Restore destination must not already exist. Restore beside the live data, then switch while the app is stopped.')
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary)
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or len(names) > MAX_BACKUP_ENTRIES:
                raise ValueError('Duplicate or excessive backup entries.')
            if 'manifest.json' not in names:
                raise ValueError('Backup manifest is missing.')
            if archive.getinfo('manifest.json').file_size > MAX_MANIFEST_BYTES:
                raise ValueError('Backup manifest is too large.')
            with archive.open('manifest.json') as source, io.BytesIO() as buffer:
                _copy_bounded(source, buffer, MAX_MANIFEST_BYTES)
                manifest = json.loads(buffer.getvalue())
            if not isinstance(manifest, dict) or manifest.get('version') != 1:
                raise ValueError('Unsupported backup manifest.')
            files = manifest.get('files')
            if not isinstance(files, dict) or 'labels.db' not in files:
                raise ValueError('Invalid backup file list.')
            if set(names) != set(files) | {'manifest.json'}:
                raise ValueError('Backup entries do not match the manifest.')
            total = 0
            # Preflight all metadata before writing any member.
            for name, digest in files.items():
                parts = Path(name).parts
                if name != 'labels.db' and not (len(parts) == 2 and parts[0] == 'product-photos'
                        and parts[1] not in ('.', '..') and not parts[1].startswith('.')
                        and Path(name).suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')):
                    raise ValueError('Unexpected backup path.')
                if chr(92) in name or ':' in name or name.startswith('/') or not isinstance(digest, str) or len(digest) != 64:
                    raise ValueError('Unsafe backup path or checksum.')
                info = archive.getinfo(name)
                if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Backup links and directories are not supported.')
                limit = MAX_DATABASE_BYTES if name == 'labels.db' else MAX_PHOTO_BYTES
                total += info.file_size
                if info.file_size > limit or total > MAX_RESTORE_BYTES:
                    raise ValueError('Backup exceeds its decompressed size limit.')
            remaining = MAX_RESTORE_BYTES
            for name, digest in files.items():
                target = stage / name
                if not target.resolve().is_relative_to(stage.resolve()):
                    raise ValueError('Unsafe backup path.')
                target.parent.mkdir(parents=True, exist_ok=True)
                limit = min(remaining, MAX_DATABASE_BYTES if name == 'labels.db' else MAX_PHOTO_BYTES)
                with archive.open(name) as source, target.open('wb') as output:
                    remaining -= _copy_bounded(source, output, limit)
                with target.open('rb') as handle:
                    if hashlib.file_digest(handle, 'sha256').hexdigest() != digest:
                        raise ValueError(f'Checksum failed for {name}.')
                if name != 'labels.db':
                    _verify_photo(target)
        with closing(sqlite3.connect(f'file:{stage / "labels.db"}?mode=ro', uri=True)) as con:
            # Bound SQLite verification work as well as extraction volume.
            deadline = time.monotonic() + 10
            con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or con.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Restored database failed integrity checks.')
        if destination:
            shutil.copytree(stage, destination)
    return True


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='/backups')
    parser.add_argument('--scheduled',action='store_true')
    parser.add_argument('--verify')
    parser.add_argument('--restore-to')
    parser.add_argument('--health',action='store_true')
    args=parser.parse_args()
    if args.health:
        raise SystemExit(0 if backup_status()['healthy'] else 1)
    if args.verify:
        verify_backup(args.verify,args.restore_to);print('Backup verified.');return
    if args.restore_to:
        parser.error('--restore-to requires --verify BACKUP.zip')
    while True:
        print(f'Backup saved: {save_backup(args.output)}',flush=True)
        if not args.scheduled:break
        time.sleep(86400)


if __name__=='__main__':main()
