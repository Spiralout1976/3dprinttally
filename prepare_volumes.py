"""One-shot container volume ownership setup; never follows symlinks."""
import os
from pathlib import Path

uid=int(os.environ.get('APP_UID','1000'));gid=int(os.environ.get('APP_GID','1000'))
for root in (Path('/app/data'),Path('/backups')):
    root.mkdir(parents=True,exist_ok=True)
    if root.is_symlink():raise RuntimeError('Data roots may not be symlinks.')
    os.chown(root,uid,gid)
    for directory,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[name for name in dirs if not (Path(directory)/name).is_symlink()]
        for name in dirs+files:
            path=Path(directory)/name
            if not path.is_symlink():os.chown(path,uid,gid,follow_symlinks=False)
