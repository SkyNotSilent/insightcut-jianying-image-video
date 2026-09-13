"""Build a static Pages site from tracked public docs, preserving legacy routes."""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = 'https://github.com/SkyNotSilent/insightcut-jianying-image-video'
ROOT_PAGES = {'README.md': 'readme.html', 'README_EN.md': 'readme-en.html',
              'CONTRIBUTING.md': 'contributing.html', 'SECURITY.md': 'security.html', 'LICENSE': 'license.html'}


def destination(name):
    if name in ROOT_PAGES:
        return ROOT_PAGES[name]
    if name.startswith('docs/'):
        name = name[5:]
        return str(Path(name).with_suffix('.html')) if name.endswith('.md') else name
    return None


def rewrite_reference(raw, source, output, known):
    parts = urlsplit(html.unescape(raw))
    if parts.scheme or parts.netloc or not parts.path:
        return raw
    source_target = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(parts.path)))
    target = destination(source_target)
    if source_target in {'docs', 'docs/'}:
        target = 'documentation.html'
    if target is not None and (source_target in known or source_target in {'docs', 'docs/'}):
        relative = posixpath.relpath(target, posixpath.dirname(output) or '.')
        return html.escape(urlunsplit(('', '', quote(relative), parts.query, parts.fragment)), quote=True)
    # Repository source links remain browsable without copying source or runtime data.
    return html.escape(f'{REPO_URL}/blob/master/{quote(source_target)}' + (f'#{parts.fragment}' if parts.fragment else ''), quote=True)


def page_shell(title, content, output):
    prefix = posixpath.relpath('.', posixpath.dirname(output) or '.') + '/'
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · InsightCut</title><link rel="icon" href="{prefix}assets/insightcut-mark.svg"><link rel="stylesheet" href="{prefix}assets/site.css"></head><body><a class="skip-link" href="#main">跳到正文</a><header class="site-header wrap"><a class="brand" href="{prefix}"><img src="{prefix}assets/insightcut-mark.svg" width="40" height="40" alt=""><span>InsightCut<span class="brand-dot">.</span></span></a><nav aria-label="主导航"><a href="{prefix}guide.html">开始使用</a><a href="{prefix}documentation.html">全部文档</a><a href="{REPO_URL}">GitHub ↗</a></nav></header><main id="main" class="document">{content}</main><footer class="footer wrap"><a class="brand" href="{prefix}">InsightCut.</a><p>给想法画面，给创作余地。</p><div><a href="{prefix}contributing.html">参与贡献</a><a href="{prefix}license.html">MIT License</a></div></footer></body></html>'''


def render_markdown(text):
    md = MarkdownIt('commonmark', {'html': True}).enable('table')
    tokens = md.parse(text)
    used = {}
    for index, token in enumerate(tokens):
        if token.type == 'heading_open':
            title = tokens[index + 1].content
            slug = re.sub(r'[^\w\-\s\u4e00-\u9fff]', '', title.lower()).replace(' ', '-')
            count = used.get(slug, 0)
            used[slug] = count + 1
            token.attrSet('id', slug + (f'-{count}' if count else ''))
    body = md.renderer.render(tokens, md.options, {})
    return body.replace('<table>', '<div class="table-scroll"><table>').replace('</table>', '</table></div>')


def build(output):
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    public = sorted(name for name in tracked if name and (name.startswith('docs/') or name in ROOT_PAGES))
    if output.exists():
        # Only the dedicated build directory may be replaced.
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for name in public:
        source = ROOT / name
        if source.is_symlink():
            raise ValueError(f'Public symlinks are not supported: {name}')
        target = destination(name)
        dest = output / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == '.md' or name == 'LICENSE':
            text = source.read_text(encoding='utf-8')
            title = next((line.lstrip('# ').strip() for line in text.splitlines() if line.startswith('#')), source.stem)
            body = render_markdown(text) if name != 'LICENSE' else '<h1>MIT License</h1><pre>' + html.escape(text) + '</pre>'
            body = re.sub(r'(href|src|poster)="([^"]+)"', lambda m: f'{m[1]}="{rewrite_reference(m[2], name, target, public)}"', body)
            dest.write_text(page_shell(title, body, target), encoding='utf-8')
        else:
            shutil.copyfile(source, dest)
    assert (output / 'index.html').is_file(), 'Track docs/index.html before building'
    listing = '<h1>项目文档</h1><p>从第一次启动，到批量创作、素材恢复与参与贡献。</p><ul>'
    for name in public:
        if name.endswith('.md'):
            title = next((line.lstrip('# ').strip() for line in (ROOT / name).read_text(encoding='utf-8').splitlines() if line.startswith('#')), Path(name).stem)
            listing += f'<li><a href="{html.escape(destination(name))}">{html.escape(title)}</a></li>'
    (output / 'documentation.html').write_text(page_shell('项目文档', listing + '</ul>', 'documentation.html'), encoding='utf-8')
    (output / '.nojekyll').touch()
    sha = os.environ.get('INSIGHTCUT_SITE_SHA') or subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    (output / 'build-info.json').write_text(json.dumps({'commit': sha, 'source': REPO_URL}), encoding='utf-8')
    print(f'Built {len(public)} public files into {output.name}; commit {sha}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='site-dist', choices=['site-dist', 'site-test-dist'])
    args = parser.parse_args()
    build(ROOT / args.output)
