"""Validate links and assets in the generated static site without network access."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
import sys


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for key in ['href', 'src', 'poster']:
            if key in attrs:
                self.refs.append(attrs[key])
        if 'id' in attrs:
            self.ids.add(attrs['id'])


def check(root):
    errors = []
    count = 0
    pages = {}
    for path in root.rglob('*.html'):
        doc = References()
        doc.feed(path.read_text(encoding='utf-8'))
        pages[path.resolve()] = doc
    for path, doc in pages.items():
        for raw in doc.refs:
            url = urlsplit(raw)
            if url.scheme or url.netloc:
                continue
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            if target.is_dir():
                target /= 'index.html'
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                errors.append(f'{path.relative_to(root)}: missing or escaping reference {raw}')
            elif url.fragment and target in pages and unquote(url.fragment) not in pages[target].ids:
                errors.append(f'{path.relative_to(root)}: missing anchor {raw}')
            count += 1
    if not pages:
        errors.append('No built HTML pages')
    print('\n'.join(errors) if errors else f'Site verified: {len(pages)} pages, {count} local references')
    return bool(errors)


if __name__ == '__main__':
    sys.exit(check(Path(sys.argv[1] if len(sys.argv) > 1 else 'site-dist').resolve()))
