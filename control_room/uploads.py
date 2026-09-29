"""Bounded, deterministic CSV/XLSX parsing and safe upload storage."""
import csv
import hashlib
import io
import os
import re
import uuid
from pathlib import Path

from openpyxl import load_workbook


FIELDS = ('company_name', 'tax_number', 'registration_number', 'address', 'municipality')
HEADER_ALIASES = {
    'company_name': {'naziv', 'naziv podjetja', 'podjetje', 'firma', 'company', 'company name'},
    'tax_number': {'davcna', 'davcna stevilka', 'davcna st', 'tax number', 'vat number'},
    'registration_number': {'maticna', 'maticna stevilka', 'maticna st', 'registration number'},
    'address': {'naslov', 'address', 'ulica'},
    'municipality': {'obcina', 'posta', 'municipality', 'city', 'kraj'},
}


class UploadError(ValueError):
    pass


def _plain(value):
    return '' if value is None else str(value).strip()


def header_key(value):
    import unicodedata
    text = unicodedata.normalize('NFKD', _plain(value)).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def suggest_mapping(headers):
    suggestions = {}
    for field, aliases in HEADER_ALIASES.items():
        matches = [index for index, header in enumerate(headers) if header_key(header) in aliases]
        suggestions[field] = matches[0] if len(matches) == 1 else None
    return suggestions


def _validate(headers, rows, max_rows, max_columns=100):
    if not headers or not any(_plain(value) for value in headers):
        raise UploadError('The uploaded file has no header row')
    if len(headers) > max_columns:
        raise UploadError(f'The upload exceeds the {max_columns}-column limit')
    if len(rows) > max_rows:
        raise UploadError(f'The upload exceeds the {max_rows}-row limit')
    if not rows:
        raise UploadError('The uploaded file has no company rows')
    width = len(headers)
    if any(len(row) != width for row in rows):
        raise UploadError('Malformed file: rows have inconsistent column counts')
    return [_plain(value) for value in headers], [[_plain(value) for value in row] for row in rows]


def parse_csv(data, max_rows):
    decoded = None
    for encoding in ('utf-8-sig', 'cp1250'):
        try:
            decoded = data.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if decoded is None:
        raise UploadError('CSV must use UTF-8 or Windows-1250 encoding')
    sample = decoded[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
    except csv.Error as exc:
        raise UploadError('Could not determine CSV delimiter') from exc
    try:
        parsed = list(csv.reader(io.StringIO(decoded, newline=''), dialect, strict=True))
    except csv.Error as exc:
        raise UploadError(f'Malformed CSV: {exc}') from exc
    if not parsed:
        raise UploadError('The uploaded file is empty')
    return (*_validate(parsed[0], parsed[1:], max_rows), None)


def parse_xlsx(path, max_rows):
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise UploadError('Invalid XLSX workbook') from exc
    try:
        visible = [sheet for sheet in workbook.worksheets if sheet.sheet_state == 'visible']
        if not visible:
            raise UploadError('The workbook has no visible worksheet')
        sheet = visible[0]
        iterator = sheet.iter_rows(values_only=True)
        headers = next(iterator, None)
        if headers is None:
            raise UploadError('The worksheet is empty')
        rows = []
        for row in iterator:
            if not any(value is not None and _plain(value) for value in row):
                continue
            rows.append(list(row))
            if len(rows) > max_rows:
                raise UploadError(f'The upload exceeds the {max_rows}-row limit')
        headers, rows = _validate(list(headers), rows, max_rows)
        return headers, rows, sheet.title
    finally:
        workbook.close()


def store_and_parse(file_storage, storage_root, max_bytes, max_rows):
    original = Path(file_storage.filename or '').name
    extension = Path(original).suffix.lower()
    if extension not in ('.csv', '.xlsx'):
        raise UploadError('Only .csv and .xlsx files are accepted')
    data = file_storage.stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UploadError(f'The file exceeds the {max_bytes // (1024*1024)} MB limit')
    if not data:
        raise UploadError('The uploaded file is empty')
    upload_id = uuid.uuid4().hex
    root = Path(storage_root).expanduser().absolute()
    directory = root/'uploads'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or directory.is_symlink():
        raise UploadError('Upload storage may not use symbolic links')
    filename = f'{upload_id}{extension}'
    path = (directory/filename).absolute()
    if directory not in path.parents:
        raise UploadError('Invalid upload storage path')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            target.write(data)
        headers, rows, sheet = parse_csv(data, max_rows) if extension == '.csv' else parse_xlsx(path, max_rows)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {
        'upload_id': upload_id, 'original_filename': original, 'relative_path': f'uploads/{filename}',
        'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data),
        'format': extension[1:].upper(), 'worksheet_name': sheet,
        'headers': headers, 'rows': rows,
    }
