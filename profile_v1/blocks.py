"""Stable, conservative semantic blocks from copied first-party HTML."""
import hashlib, json, re
from bs4 import BeautifulSoup
from profile_v1.store import uid

NOISE = re.compile(r'cookie|consent|navigation|navbar|menu|breadcrumb|social|newsletter', re.I)


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def extract_blocks(evidence):
    html = evidence.get('html') or ''
    if not html: return []
    soup = BeautifulSoup(html, 'html.parser')
    structured = []
    for index, node in enumerate(soup.find_all('script', attrs={'type':'application/ld+json'}), 1):
        try: structured.append((index, json.loads(node.string or '')))
        except (ValueError, TypeError): pass
    for node in soup(['script','style','noscript','template','svg','nav','footer','form','aside']):
        node.decompose()
    for node in list(soup.find_all(True)):
        marker = ' '.join(node.get('class', []))+' '+str(node.get('id') or '')
        if NOISE.search(marker): node.decompose()
    rows, seen = [], set()
    def add(kind, locator, value):
        text = clean_text(value)
        if len(text) < 3: return
        key = (kind, text)
        if key in seen: return
        seen.add(key)
        rows.append({'block_id': uid(), 'block_type': kind, 'source_locator': locator,
                     'text': text, 'text_hash': hashlib.sha256(text.encode()).hexdigest(),
                     'language': None})
    if soup.title: add('TITLE', 'title', soup.title.get_text(' ', strip=True))
    meta = soup.find('meta', attrs={'name': re.compile(r'^description$', re.I)})
    if meta: add('META_DESCRIPTION', 'meta[name=description]', meta.get('content'))
    counts = {}
    for node in soup.find_all(['h1','h2','h3','p','li']):
        kind = 'HEADING' if node.name.startswith('h') else 'PARAGRAPH' if node.name == 'p' else 'LIST_ITEM'
        counts[node.name] = counts.get(node.name, 0)+1
        add(kind, f'{node.name}:nth-of-type({counts[node.name]})', node.get_text(' ', strip=True))
    for index, value in structured:
        add('STRUCTURED_DATA', f'json-ld:{index}', json.dumps(value, ensure_ascii=False, sort_keys=True))
    return rows
