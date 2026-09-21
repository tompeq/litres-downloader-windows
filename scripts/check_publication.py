"""Small publication guard; no network, credentials, or source-content logging."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {
    '.gitignore', '.gitattributes', 'README.md', 'SECURITY.md', 'LICENSE',
    'book_model.py', 'ebook_export.py', 'text_downloader.py', 'download-text.ps1',
    'requirements-text.txt', 'test_book_model.py', 'test_ebook_export.py',
    'scripts/check_publication.py',
}

PATTERNS = {
    'github-token': r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b',
    'aws-access-key': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'private-key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'bearer-value': r'(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}',
    'literal-secret': r'''(?im)^\s*(?:password|passwd|api_key|access_token|refresh_token|client_secret|cookie)\s*=\s*['"][^'"\r\n]{5,}['"]''',
    'private-user-path': r'(?i)\b[A-Z]:[\\/]+Users[\\/]+(?!Public\b|Example\b)[^\s"\'<>]+',
}
EMAIL = re.compile(r'\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b')


def inspect_text(text: str):
    findings = [name for name, pattern in PATTERNS.items() if re.search(pattern,text)]
    for match in EMAIL.finditer(text):
        host = match.group(1).lower()
        if host not in {'example.com','example.org','example.net'} and not host.endswith('.invalid'):
            findings.append('email-address')
            break
    return findings


def main():
    tracked = subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode('utf-8').split('\0')
    names = [name for name in tracked if name]
    if not names:
        raise SystemExit('No tracked files. Stage the intended source files first.')
    errors = []
    for name in names:
        if name not in ALLOWED:
            errors.append((name,'unexpected-tracked-file'))
            continue
        # Inspect staged bytes, not only the working tree: a cleaned working copy
        # must not conceal sensitive content already staged for commit.
        data = subprocess.check_output(['git','show',':'+name],cwd=ROOT)
        if len(data) > 250_000 or b'\x00' in data:
            errors.append((name,'unexpected-binary-or-large-file'))
            continue
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            errors.append((name,'non-utf8-content'))
            continue
        errors.extend((name,kind) for kind in inspect_text(text))
    for name,kind in errors:
        print(f'{name}: {kind}')  # Never echo the matching secret.
    if errors:
        raise SystemExit(1)
    print(f'Publication guard passed: {len(names)} tracked text files; no matching secrets.')


if __name__ == '__main__':
    main()
