"""Regenerate the reviewed source allowlist before rebuilding a modified release."""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
roots = {'templates', 'static', 'docs', 'tools', 'tests', '.github'}
exact = {'Dockerfile', 'LICENSE', 'COPYRIGHT', '.env.example', '.gitignore', '.dockerignore', 'SOURCE-MANIFEST.txt'}
allowed = {'.py', '.md', '.txt', '.html', '.js', '.css', '.svg', '.yml', '.yaml'}
names = {'SOURCE-MANIFEST.txt'}
for path in root.rglob('*'):
    relative = path.relative_to(root)
    if path.is_symlink() or not path.is_file() or '__pycache__' in relative.parts:
        continue
    if str(relative) in exact or (path.suffix in allowed and (len(relative.parts) == 1 or relative.parts[0] in roots)):
        names.add(relative.as_posix())
(root / 'SOURCE-MANIFEST.txt').write_text('\n'.join(sorted(names)) + '\n')
print(f'Source manifest: {len(names)} files. Review it for private content before distributing.')
