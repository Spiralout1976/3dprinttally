"""Validate uploaded images before atomically replacing any existing photo."""
import os
import tempfile
import warnings
from PIL import Image, UnidentifiedImageError
from config import PHOTO_DIR


def save_product_photo(sku, file_storage):
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit('.', 1)[-1].lower()
    expected = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}
    if ext not in expected:
        return 'invalid'
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            image = Image.open(file_storage.stream)
            if image.format != expected[ext] or image.width * image.height > 20_000_000:
                return 'invalid'
            image.verify()
            file_storage.stream.seek(0)
            image = Image.open(file_storage.stream)
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return 'invalid'
    filename = f'{sku}.{ext}'
    target = PHOTO_DIR / filename
    if not target.resolve().is_relative_to(PHOTO_DIR.resolve()):
        return 'invalid'
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix='.photo-', dir=PHOTO_DIR)
    os.close(fd)
    try:
        # Re-encode validated pixels, excluding attached/trailing executable payloads.
        if expected[ext] == 'JPEG' and image.mode not in ('RGB', 'L'):
            image = image.convert('RGB')
        image.save(temp_name, format=expected[ext])
        os.replace(temp_name, target)
        for existing in PHOTO_DIR.iterdir():
            if existing.stem == sku and existing != target and existing.suffix.lower().lstrip('.') in expected:
                existing.unlink()
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return filename
