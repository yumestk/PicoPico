"""Bounded current-file pages shared by tools and the desktop reader."""

import os
from pathlib import Path, PurePosixPath

from .review import read_entry


def protected_text(text, secret_env_names=()):
    from .security import detected_secret_env_items
    for _, secret in sorted(detected_secret_env_items(secret_env_names=secret_env_names), key=lambda item: len(item[1]), reverse=True):
        # Preserve line and character positions, including secrets spanning page boundaries.
        text = text.replace(secret, ''.join(c if c in '\r\n' else '•' for c in secret))
    return text


def read_page(root, raw_path, start=1, end=200, column=0, secret_env_names=()):
    root = Path(root).resolve()
    parts = PurePosixPath(raw_path).parts
    if not parts or len(raw_path) > 1024 or raw_path.startswith('/') or any(p in ('..', '.git', '.pico') for p in parts):
        raise ValueError('Invalid workspace file path')
    raw_path = str(PurePosixPath(raw_path))
    if type(start) is not int or type(end) is not int or type(column) is not int or start < 1 or end < start or column < 0:
        raise ValueError('Invalid line range')
    blobs = []
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        entry = read_entry(descriptor, str(PurePosixPath(raw_path)), lambda data: blobs.append(data) or 'page')
    finally:
        os.close(descriptor)
    if not blobs:
        raise ValueError('Cannot read file: ' + (entry.get('reason') or 'unavailable'))
    lines = protected_text(blobs[0].decode('utf-8'), secret_env_names).splitlines()
    reserve = len(f'# {raw_path}\n\nReturned lines {start}-{end} of {len(lines)}. More content: read_file(path={raw_path!r}, start={end + 200}, end={end + 400}, column=524288).')
    budget = min(2800, 3800 - reserve)
    if budget < 128:
        raise ValueError('File path exceeds display budget')
    stop = min(end, start + 199, len(lines))
    rendered, used, next_line, next_column = [], 0, None, 0
    actual_end = start - 1
    for number in range(start, stop + 1):
        offset = column if number == start else 0
        if offset > len(lines[number - 1]):
            raise ValueError('Column exceeds line length')
        remaining = lines[number - 1][offset:]
        room = budget - used
        if room < 30:
            next_line = number
            break
        part = remaining[:max(1, room - 12)]
        rendered.append(f'{number:>4}: {part}')
        used += len(part) + 12
        actual_end = number
        if len(part) < len(remaining):
            next_line, next_column = number, offset + len(part)
            break
    if next_line is None and stop < len(lines):
        next_line = max(start, stop + 1)
    footer = f'\nReturned lines {start}-{actual_end} of {len(lines)}.'
    if next_line is not None:
        footer += f' More content: read_file(path={raw_path!r}, start={next_line}, end={next_line + 199}, column={next_column}).'
    return {'path': raw_path, 'start': start, 'end': actual_end, 'column': column,
            'total_lines': len(lines), 'next_line': next_line, 'next_column': next_column,
            'truncated': next_line is not None, 'revision': entry['fingerprint'],
            'text': f'# {raw_path}\n' + '\n'.join(rendered) + footer}
