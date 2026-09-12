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


def verify_backup(path, restore_to=None):
    """Always verifies to a new staging directory; never overwrites a live database."""
    destination=Path(restore_to).resolve() if restore_to else None
    if destination and destination.exists():
        raise ValueError('Restore destination must not already exist. Restore beside the live data, then switch while the app is stopped.')
    with tempfile.TemporaryDirectory() as temporary:
        stage=Path(temporary)
        with zipfile.ZipFile(path) as archive:
            names=archive.namelist()
            if len(names)!=len(set(names)) or len(names)>100000:
                raise ValueError('Duplicate or excessive backup entries.')
            manifest=json.loads(archive.read('manifest.json'))
            if manifest.get('version')!=1 or 'labels.db' not in manifest.get('files',{}):
                raise ValueError('Unsupported backup manifest.')
            if set(names)!=set(manifest['files'])|{'manifest.json'}:
                raise ValueError('Backup entries do not match the manifest.')
            for name,digest in manifest['files'].items():
                parts=Path(name).parts
                if name!='labels.db' and not (len(parts)==2 and parts[0]=='product-photos' and parts[1] not in ('.','..')):
                    raise ValueError('Unexpected backup path.')
                if '\\' in name or ':' in name or name.startswith('/'):
                    raise ValueError('Unsafe backup path.')
                target=stage/name
                if not target.resolve().is_relative_to(stage.resolve()):
                    raise ValueError('Unsafe backup path.')
                target.parent.mkdir(parents=True,exist_ok=True)
                with archive.open(name) as source,target.open('wb') as output:
                    shutil.copyfileobj(source,output)
                with target.open('rb') as handle:
                    if hashlib.file_digest(handle,'sha256').hexdigest()!=digest:
                        raise ValueError(f'Checksum failed for {name}.')
        with closing(sqlite3.connect(stage/'labels.db')) as con:
            if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or con.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Restored database failed integrity checks.')
        if destination:
            shutil.copytree(stage,destination)
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
