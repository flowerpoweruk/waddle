"""Bounded public-site crawl with an optional local browser-style HTTP fallback."""
import csv
import datetime as dt
import io
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

AGENT = 'AIOverviewTool'
LIMIT = 10000
BODY_LIMIT = 2 * 1024 * 1024


class Robots(RobotFileParser):
    """Add wildcard and longest-rule matching absent from RobotFileParser."""
    def parse(self, lines):
        super().parse(lines)
        self.groups = []
        agents, rules, directives = [], [], False
        for line in lines:
            key, sep, value = line.split('#', 1)[0].partition(':')
            if not sep: continue
            key, value = key.strip().lower(), value.strip()
            if key == 'user-agent':
                if directives:
                    self.groups.append((agents, rules)); agents, rules, directives = [], [], False
                agents.append(value.lower())
            elif agents:
                directives = True
                if key in ('allow', 'disallow') and value: rules.append((value, key == 'allow'))
        if agents: self.groups.append((agents, rules))

    def can_fetch(self, useragent, url):
        groups = []
        for agents, rules in self.groups:
            score = max([len(a) if a != '*' else 0 for a in agents if a == '*' or a in useragent.lower()] or [-1])
            if score >= 0: groups.append((score, rules))
        if not groups: return True
        best = max(g[0] for g in groups)
        p = urlsplit(url)
        path = unquote(p.path or '/') + ('?' + unquote(p.query) if p.query else '')
        matches = []
        for score, rules in groups:
            if score != best: continue
            for pattern, allow in rules:
                pattern = unquote(pattern)
                end = pattern.endswith('$')
                if end: pattern = pattern[:-1]
                regex = '^' + '.*'.join(re.escape(part) for part in pattern.split('*')) + ('$' if end else '')
                if re.search(regex, path): matches.append((len(pattern.replace('*','')), allow))
        return max(matches)[1] if matches else True


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def normalize(value, base='', scope=None):
    p = urlsplit(urljoin(base, str(value).strip()))
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Only public HTTP(S) website URLs are allowed.')
    host = p.hostname.rstrip('.').lower().encode('idna').decode('ascii')
    if p.port not in (None, 80 if p.scheme == 'http' else 443):
        raise ValueError('Only standard website ports are allowed.')
    if scope and host != scope and not host.endswith('.' + scope):
        raise ValueError('URL or redirect is outside the tracked domain.')
    if len(value) > 2048 or re.search(r'[\x00-\x20\x7f]', p.path + p.query):
        raise ValueError('URL is too long or contains invalid characters.')
    return urlunsplit((p.scheme, host, p.path or '/', p.query, ''))


def public_addresses(host, port):
    addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
        raise ValueError('Private, local and reserved network addresses are not crawled.')
    return addresses


def browser_fetch(url, scope, allowed=None):
    """Use Scrapling's HTTP engine without surrendering DNS/redirect/body guards."""
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Session
    for _ in range(6):
        url = normalize(url, scope=scope)
        if allowed is not None and not allowed(url, scope):
            raise ValueError('Redirect destination is disallowed by robots.txt or requires special crawl pacing.')
        p = urlsplit(url)
        port = 443 if p.scheme == 'https' else 80
        address = public_addresses(p.hostname, port)[0]
        pinned = '[' + address + ']' if ':' in address else address
        data = bytearray()
        oversized = False
        def receive(chunk):
            nonlocal oversized
            if len(data) + len(chunk) > BODY_LIMIT:
                oversized = True
                return 0
            data.extend(chunk)
            return len(chunk)
        # Disable environment proxies so the validated destination cannot be resolved elsewhere.
        with Session(trust_env=False, curl_options={CurlOpt.RESOLVE: [f'{p.hostname}:{port}:{pinned}'], CurlOpt.PROXY: ''}) as session:
            try:
                response = session.get(url, impersonate='chrome', allow_redirects=False, timeout=30,
                                       verify=True, accept_encoding='identity', content_callback=receive)
            except Exception:
                if oversized: raise ValueError('Page exceeds the 2 MB download limit.') from None
                raise OSError('Local browser-style HTTP retrieval failed; no paid service was used.') from None
        headers = {k.lower(): v for k, v in response.headers.items()}
        if response.status_code in (301, 302, 303, 307, 308):
            if not headers.get('location'): raise ValueError('Redirect has no destination.')
            url = normalize(headers['location'], url, scope)
            continue
        return url, response.status_code, headers, bytes(data)
    raise ValueError('Too many redirects.')


def fetch(url, scope, allowed=None):
    """Validate every redirect and pin the socket to the checked public address."""
    for _ in range(6):
        url = normalize(url, scope=scope)
        if allowed is not None and not allowed(url, scope):
            raise ValueError('Redirect destination is disallowed by robots.txt or requires special crawl pacing.')
        p = urlsplit(url)
        port = 443 if p.scheme == 'https' else 80
        addresses = public_addresses(p.hostname, port)
        connection = HTTPSConnection(p.hostname, timeout=15) if p.scheme == 'https' else HTTPConnection(p.hostname, timeout=15)
        with closing(connection):
            sock = socket.create_connection((addresses[0], port), timeout=15)
            connection.sock = sock
            if p.scheme == 'https':
                connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
            connection.request('GET', urlunsplit(('', '', p.path, p.query, '')),
                               headers={'User-Agent': AGENT + '/1.0', 'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = connection.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            if headers.get('cf-mitigated', '').lower() == 'challenge' or (response.status == 403 and headers.get('server', '').lower() == 'cloudflare'):
                try:
                    import curl_cffi
                except ImportError:
                    pass
                else:
                    connection.close()
                    return browser_fetch(url, scope, allowed)
            if response.status in (301, 302, 303, 307, 308):
                if not headers.get('location'):
                    raise ValueError('Redirect has no destination.')
                url = normalize(headers['location'], url, scope)
                continue
            data = bytearray()
            deadline = time.monotonic() + 30
            while len(data) <= BODY_LIMIT:
                if time.monotonic() > deadline:
                    raise TimeoutError('Page exceeded the download time limit.')
                chunk = response.read1(min(65536, BODY_LIMIT + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > BODY_LIMIT:
                raise ValueError('Page exceeds the 2 MB download limit.')
            return url, response.status, headers, bytes(data)
    raise ValueError('Too many redirects.')


def decode(data, headers):
    encoding = re.search(r'charset=["\']?([\w-]+)', headers.get('content-type', ''), re.I)
    try:
        return data.decode(encoding[1] if encoding else 'utf-8', errors='replace')
    except LookupError:
        return data.decode('utf-8', errors='replace')


EDITORIAL_BOILERPLATE = re.compile(
    r'(?i)\b(?:book (?:a |your )?call|free quote check|register now|scroll to top|contact us|'
    r'get in touch|request (?:a )?quote|subscribe(?: now)?|follow us|share on \w+|'
    r'thank you for subscribing|this field is required|please leave this field empty)\b[.!]*')


class PageParser(HTMLParser):
    def __init__(self, url, scope, editorial=False):
        super().__init__(convert_charrefs=True)
        self.url, self.scope = url, scope
        self.stack, self.text, self.main, self.headings, self.links, self.structured = [], [], [], [], [], []
        self.title, self.description, self.canonical, self.author, self.date = '', '', '', '', ''
        self.robots, self.json_text = [], []
        self.heading = None
        self.anchor = None
        self.json_script = False
        self.partial_snippet = False
        self.editorial = editorial
        self.review = None if editorial else PageParser(url, scope, editorial=True)

    def feed(self, data):
        super().feed(data)
        if self.review is not None: self.review.feed(data)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-nosnippet' in attrs: self.partial_snippet = True
        hidden = 'hidden' in attrs or attrs.get('aria-hidden') == 'true' or 'display:none' in attrs.get('style', '').replace(' ', '').lower()
        if self.editorial:
            hidden |= tag in {'nav', 'footer', 'header', 'aside', 'form', 'select', 'option', 'label', 'button', 'textarea'}
            hidden |= attrs.get('role', '').lower() in {'navigation', 'banner', 'contentinfo', 'complementary', 'dialog', 'form', 'menu', 'button'}
            markers = ' '.join(attrs.get(k, '') for k in ('id', 'class', 'aria-label'))
            hidden |= bool(re.search(r'(?i)(?:^|[\s_-])(?:nav(?:igation)?|menu|footer|sidebar|newsletter|social|captcha|recaptcha|honeypot|cookie|consent|contact[-_ ]?form|scroll[-_ ]?to[-_ ]?top|cta)(?:$|[\s_-])|wpcf7|g-recaptcha', markers))
            if tag == 'a':
                hidden |= attrs.get('href', '').lower().startswith(('tel:', 'mailto:', '#top'))
        if tag not in ('meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'area', 'base', 'wbr', 'embed', 'param', 'col', 'track'):
            self.stack.append((tag, hidden or (self.stack[-1][1] if self.stack else False)))
        if tag == 'meta':
            name = (attrs.get('name') or attrs.get('property') or '').lower()
            content = attrs.get('content', '')[:2000]
            if name == 'description': self.description = content
            if name in ('robots', 'googlebot'): self.robots.append(content)
            if name in ('author', 'article:author'): self.author = content
            if name in ('article:published_time', 'date', 'datepublished'): self.date = content
        if tag == 'time' and not self.date: self.date = attrs.get('datetime', '')[:2000]
        if tag == 'link' and 'canonical' in attrs.get('rel', '').lower().split():
            try: self.canonical = normalize(attrs.get('href', ''), self.url)
            except ValueError: pass
        if re.fullmatch('h[1-6]', tag):
            self.heading = {'level': tag, 'text': ''}
        if tag == 'a' and len(self.links) < 500 and attrs.get('href'):
            try: self.anchor = {'url': normalize(attrs['href'], self.url, self.scope), 'text': ''}
            except ValueError: self.anchor = None
        if tag == 'script':
            self.json_script = attrs.get('type', '').lower() == 'application/ld+json'
            self.json_text = []

    def handle_endtag(self, tag):
        if re.fullmatch('h[1-6]', tag) and self.heading:
            self.heading['text'] = ' '.join(self.heading['text'].split())[:2000]
            if self.editorial: self.heading['text'] = EDITORIAL_BOILERPLATE.sub('', self.heading['text']).strip()
            if self.heading['text']: self.headings.append(self.heading)
            self.heading = None
        if tag == 'a' and self.anchor:
            self.anchor['text'] = ' '.join(self.anchor['text'].split())[:500]
            self.links.append(self.anchor)
            self.anchor = None
        if tag == 'script' and self.json_text:
            try:
                obj = json.loads(''.join(self.json_text))
                if len(self.structured) < 20: self.structured.append(obj)
            except ValueError: pass
            self.json_text = []
        if tag == 'script': self.json_script = False
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        tags = {x[0] for x in self.stack}
        if 'title' in tags: self.title += data[:2000]
        if 'script' in tags:
            if self.json_script and sum(map(len, self.json_text)) < 50000: self.json_text.append(data[:50000])
            return
        if self.stack and self.stack[-1][1]: return
        if tags & {'style', 'noscript', 'svg', 'template', 'head'}: return
        if self.heading: self.heading['text'] += ' ' + data
        if self.anchor: self.anchor['text'] += ' ' + data
        if not tags & {'nav', 'footer', 'header', 'aside'}:
            self.text.append(data)
            if tags & {'main', 'article'}: self.main.append(data)

    def result(self, header_robots=''):
        content = ' '.join(' '.join(self.main or self.text).split())
        if self.editorial: content = ' '.join(EDITORIAL_BOILERPLATE.sub('', content).split())
        robots = ', '.join(self.robots + ([header_robots] if header_robots else []))
        directives = robots.lower()
        restrictions = bool(re.search(r'\b(noindex|none|nosnippet)\b|max-snippet\s*:\s*0\b', directives))
        result = {'title': ' '.join(self.title.split())[:2000], 'description': self.description,
                'h1': [h['text'] for h in self.headings if h['level'] == 'h1'], 'headings': self.headings[:200],
                'content': content[:50000], 'content_truncated': len(content) > 50000,
                'word_count': len(content.split()), 'canonical': self.canonical, 'robots': robots,
                'restricted': restrictions, 'partial_snippet_restriction': self.partial_snippet, 'links': self.links, 'author': self.author, 'date': self.date,
                'structured_data': self.structured,
                'findings': (['Detected indexing or snippet restriction.'] if restrictions else []) +
                (['Some page sections use data-nosnippet. This is a partial snippet restriction, not a whole-page exclusion.'] if self.partial_snippet else []) +
                ([] if self.title.strip() else ['Missing meta title.']) +
                ([] if any(h['level'] == 'h1' for h in self.headings) else ['No H1 detected.']) +
                ([] if content else ['No readable HTML content. JavaScript is not executed.']) +
                (['Stored content truncated at 50,000 characters.'] if len(content) > 50000 else [])}
        if self.review is not None:
            clean = self.review.result()
            result['review_content'] = {k: clean[k] for k in ('content', 'headings', 'content_truncated')}
        return result


class Crawler:
    def __init__(self, app, fetcher=fetch):
        self.app, self.fetch = app, fetcher
        self.robots, self.robot_lock = {}, threading.Lock()
        self.thread = None
        with app.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS crawl_jobs(project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, started TEXT NOT NULL, message TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS crawl_pages(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    url TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'page', status TEXT NOT NULL DEFAULT 'queued',
                    title TEXT NOT NULL DEFAULT '', final_url TEXT NOT NULL DEFAULT '', http_status INTEGER,
                    crawled TEXT NOT NULL DEFAULT '', successful TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                    canonical_diff INTEGER NOT NULL DEFAULT 0, restricted INTEGER NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '{}', UNIQUE(project_id,url));
                CREATE INDEX IF NOT EXISTS crawl_queue ON crawl_pages(project_id,status,id);
                CREATE INDEX IF NOT EXISTS crawl_list ON crawl_pages(project_id,kind,id);
                CREATE TABLE IF NOT EXISTS inclusion_checks(project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, checked TEXT NOT NULL, note TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS crawl_retries(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'running', started TEXT NOT NULL, finished TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS crawl_retry_pages(run_id INTEGER NOT NULL REFERENCES crawl_retries(id) ON DELETE CASCADE,
                    page_id INTEGER NOT NULL REFERENCES crawl_pages(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'queued', evidence TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(run_id,page_id));
            ''')
            if 'targeted' not in {r[1] for r in db.execute('PRAGMA table_info(crawl_retries)')}:
                db.execute('ALTER TABLE crawl_retries ADD COLUMN targeted INTEGER NOT NULL DEFAULT 0')
            db.execute("UPDATE crawl_pages SET status='queued' WHERE status='running'")
            db.execute("UPDATE crawl_jobs SET status='paused',message='Interrupted crawl recovered. Choose Resume.' WHERE status='running'")
            db.execute("UPDATE crawl_jobs SET status='failed',message='Crawl failed: no pages were retrieved. The homepage was only added to start discovery. Review the sitemap notices and page details for access failures.' WHERE status='complete' AND NOT EXISTS (SELECT 1 FROM crawl_pages p WHERE p.project_id=crawl_jobs.project_id AND p.kind='page' AND p.status='done')")

    def start_worker(self):
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.thread.start()

    def add(self, db, project_id, url, scope, kind='page'):
        try: url = normalize(url, scope=scope)
        except (ValueError, UnicodeError): return
        if db.execute('SELECT 1 FROM crawl_pages WHERE project_id=? AND url=?', (project_id, url)).fetchone(): return
        if kind == 'page' and re.search(r'\.(?:jpg|jpeg|png|gif|svg|webp|mp4|mp3|zip|css|js|woff2?)(?:\?|$)', url, re.I): return
        count = db.execute('SELECT COUNT(*) FROM crawl_pages WHERE project_id=? AND kind=?', (project_id, kind)).fetchone()[0]
        if count < (LIMIT if kind == 'page' else 100):
            db.execute('INSERT OR IGNORE INTO crawl_pages(project_id,url,kind) VALUES (?,?,?)', (project_id, url, kind))

    def control(self, body):
        pid, action = int(body.get('project_id', 0)), body.get('action', '')
        if action not in ('start', 'pause', 'resume', 'recrawl', 'retry_failed'): raise ValueError('Choose a valid crawl action.')
        with self.app.lock, self.app.connect() as db:
            project = db.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
            if not project: raise ValueError('Choose a project first.')
            running = db.execute("SELECT 1 FROM crawl_pages WHERE project_id=? AND status='running'", (pid,)).fetchone()
            job = db.execute('SELECT status FROM crawl_jobs WHERE project_id=?', (pid,)).fetchone()
            retry = db.execute("SELECT id FROM crawl_retries WHERE project_id=? AND status='running'", (pid,)).fetchone()
            if action == 'pause':
                db.execute("UPDATE crawl_jobs SET status='paused',message='Paused. Any in-flight pages will finish.' WHERE project_id=?", (pid,))
            else:
                if running or (job and job['status'] == 'running'): raise ValueError('Wait for the current batch to finish first.')
                if retry and action != 'resume': raise ValueError('Resume and finish the paused retry before starting another crawl.')
                if action == 'retry_failed':
                    failed = db.execute("SELECT id FROM crawl_pages WHERE project_id=? AND kind='page' AND status IN ('blocked','error')", (pid,)).fetchall()
                    if not failed: raise ValueError('There are no blocked or unavailable pages to retry.')
                    run_id = db.execute('INSERT INTO crawl_retries(project_id,started) VALUES (?,?)', (pid, utc())).lastrowid
                    db.executemany('INSERT INTO crawl_retry_pages(run_id,page_id) VALUES (?,?)', [(run_id,r['id']) for r in failed])
                    db.executemany("UPDATE crawl_pages SET status='queued' WHERE id=?", [(r['id'],) for r in failed])
                    self.robots = {k:v for k,v in self.robots.items() if k[0] != pid}
                if action == 'recrawl':
                    db.execute("UPDATE crawl_pages SET status='queued',reason='' WHERE project_id=?", (pid,))
                    self.robots.clear()
                db.execute("INSERT INTO crawl_jobs(project_id,status,started) VALUES (?,'running',?) ON CONFLICT(project_id) DO UPDATE SET status='running',message=''", (pid, utc()))
                if action != 'retry_failed' and not retry:
                    self.add(db, pid, 'https://' + project['domain'] + '/', project['domain'])
                    self.add(db, pid, 'https://' + project['domain'] + '/sitemap.xml', project['domain'], 'sitemap')
        return {'ok': True}

    def robot(self, url, scope, pid):
        origin = urlunsplit((*urlsplit(url)[:2], '', '', ''))
        cache_key = (pid, origin)
        with self.robot_lock:
            if cache_key not in self.robots:
                final, status, headers, data = self.fetch(origin + '/robots.txt', scope)
                parser = Robots()
                if status in (404, 410): parser.parse([])
                elif status == 200: parser.parse(decode(data, headers).splitlines())
                else: raise ValueError('robots.txt could not be verified (HTTP ' + str(status) + '); crawl not attempted.')
                self.robots[cache_key] = parser
                with self.app.lock, self.app.connect() as db:
                    for sitemap in (parser.site_maps() or [])[:100]: self.add(db, pid, sitemap, scope, 'sitemap')
            return self.robots[cache_key]

    def page(self, row, scope):
        final, status, result = row['url'], None, None
        try:
            parser = self.robot(row['url'], scope, row['project_id'])
            if not parser.can_fetch(AGENT, row['url']):
                return self.save(row, 'blocked', final, None, 'Disallowed by robots.txt.', None)
            delay = parser.crawl_delay(AGENT) or 0
            rate = parser.request_rate(AGENT)
            if delay or rate:
                # Strict site pacing is not compatible with five-page concurrency; refuse rather than ignore it.
                return self.save(row, 'blocked', final, None, 'robots.txt requests special crawl pacing; automatic batches were not sent.', None)
            # Check robots on redirected destinations as well as the original URL.
            def guarded(url, host):
                policy = self.robot(url, host, row['project_id'])
                return policy.can_fetch(AGENT, url) and not policy.crawl_delay(AGENT) and not policy.request_rate(AGENT)
            if self.fetch is fetch:
                final, status, headers, data = fetch(row['url'], scope, guarded)
            else:
                final, status, headers, data = self.fetch(row['url'], scope)
            final = normalize(final, scope=scope)
            if headers.get('cf-mitigated', '').lower() == 'challenge' or (status == 403 and headers.get('server', '').lower() == 'cloudflare'):
                return self.save(row, 'blocked', final, status,
                    'Cloudflare blocked automated access. The website may permit crawling in robots.txt, but its security layer denied this request. Arrange crawler access with the site owner or configure an authorised website-fetching service.', None)
            if status in (401, 403):
                return self.save(row, 'blocked', final, status, 'The website denied access (HTTP ' + str(status) + '). No page content was retrieved.', None)
            if status != 200: return self.save(row, 'error', final, status, 'Page returned HTTP ' + str(status) + '.', None)
            if row['kind'] == 'sitemap':
                root = ET.fromstring(data)
                if root.tag.split('}')[-1] not in ('sitemapindex', 'urlset'): raise ValueError('Unsupported sitemap format.')
                kind = 'sitemap' if root.tag.split('}')[-1] == 'sitemapindex' else 'page'
                with self.app.lock, self.app.connect() as db:
                    for loc in root.iter():
                        if loc.tag.split('}')[-1] == 'loc' and loc.text: self.add(db, row['project_id'], loc.text, scope, kind)
                return self.save(row, 'done', final, status, '', {})
            if 'html' not in headers.get('content-type', '').lower():
                return self.save(row, 'unsupported', final, status, 'Not an HTML page; text was not analysed.', None)
            html = PageParser(final, scope)
            html.feed(decode(data, headers))
            result = html.result(headers.get('x-robots-tag', ''))
            result['googlebot_allowed_by_robots'] = self.robot(final, scope, row['project_id']).can_fetch('Googlebot', final)
            if not result['googlebot_allowed_by_robots']:
                result['restricted'] = True
                result['findings'].append('robots.txt disallows Googlebot for this URL. This does not establish current indexing status.')
            if result['canonical'] and result['canonical'] != final: result['findings'].append('Canonical points to a different URL. Verify the intended canonical.')
            if not row.get('no_discovery'):
                with self.app.lock, self.app.connect() as db:
                    for link in result['links']: self.add(db, row['project_id'], link['url'], scope)
            return self.save(row, 'done', final, status, '', result)
        except (OSError, ValueError, UnicodeError, ET.ParseError) as error:
            return self.save(row, 'error', final, status, str(error)[:300], None)
        except Exception:
            return self.save(row, 'error', final, status, 'The page could not be parsed safely.', None)

    def save(self, row, status, final, code, reason, result):
        with self.app.connect() as db:
            db.execute('UPDATE crawl_pages SET status=?,final_url=?,http_status=?,crawled=?,reason=? WHERE id=?',
                       (status, final, code, utc(), reason, row['id']))
            if result is not None:
                db.execute('UPDATE crawl_pages SET title=?,successful=?,canonical_diff=?,restricted=?,result=? WHERE id=?',
                           (result.get('title', ''), utc(), bool(result.get('canonical') and result['canonical'] != final),
                            result.get('restricted', False), json.dumps(result), row['id']))
            saved = dict(db.execute('SELECT * FROM crawl_pages WHERE id=?', (row['id'],)).fetchone())
            db.execute("UPDATE crawl_retry_pages SET status=?,evidence=? WHERE page_id=? AND run_id IN (SELECT id FROM crawl_retries WHERE project_id=? AND status='running')",
                       (status, json.dumps(saved), row['id'], row['project_id']))

    def batch(self):
        with self.app.lock, self.app.connect() as db:
            job = db.execute("SELECT j.project_id,p.domain FROM crawl_jobs j JOIN projects p ON p.id=j.project_id WHERE j.status='running' ORDER BY j.project_id LIMIT 1").fetchone()
            if not job: return False
            retry = db.execute("SELECT id,targeted FROM crawl_retries WHERE project_id=? AND status='running'", (job['project_id'],)).fetchone()
            cohort = ' AND id IN (SELECT page_id FROM crawl_retry_pages WHERE run_id=?)' if retry else ''
            rows = db.execute("SELECT * FROM crawl_pages WHERE project_id=? AND status='queued'" + cohort + " ORDER BY CASE kind WHEN 'sitemap' THEN 0 ELSE 1 END,id LIMIT 5", (job['project_id'], retry['id']) if retry else (job['project_id'],)).fetchall()
            if not rows:
                if retry:
                    total, successful = db.execute("SELECT COUNT(*),COALESCE(SUM(status='done'),0) FROM crawl_retry_pages WHERE run_id=?", (retry['id'],)).fetchone()
                    db.execute("UPDATE crawl_retries SET status='complete',finished=? WHERE id=?", (utc(),retry['id']))
                    db.execute("UPDATE crawl_jobs SET status='complete',message=? WHERE project_id=?", (f'Retry finished: {successful} of {total} pages successfully crawled; {total-successful} still unsuccessful.',job['project_id']))
                    return True
                counts = dict(db.execute("SELECT status,COUNT(*) FROM crawl_pages WHERE project_id=? AND kind='page' GROUP BY status", (job['project_id'],)).fetchall())
                successful = counts.get('done', 0)
                unavailable = sum(counts.get(s, 0) for s in ('blocked', 'error', 'unsupported'))
                sitemap_failures = db.execute("SELECT COUNT(*) FROM crawl_pages WHERE project_id=? AND kind='sitemap' AND status IN ('blocked','error')", (job['project_id'],)).fetchone()[0]
                status = 'complete' if successful else 'failed'
                message = (f'Crawl finished: {successful} pages retrieved; {unavailable} blocked, unavailable or non-HTML.' if successful
                           else f'Crawl failed: no pages were retrieved. {unavailable} page URL(s) could not be read. The seeded homepage is not a successfully discovered page.')
                if sitemap_failures: message += f' {sitemap_failures} sitemap(s) could not be read; discovery may be incomplete. Open the notices and page details for the cause.'
                db.execute('UPDATE crawl_jobs SET status=?,message=? WHERE project_id=?', (status, message, job['project_id']))
                return True
            db.executemany("UPDATE crawl_pages SET status='running' WHERE id=?", [(r['id'],) for r in rows])
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda row: self.page(dict(row, no_discovery=bool(retry and retry['targeted'])), job['domain']), rows))
        return True

    def work(self):
        sources_first = False
        while not self.app.stop_event.is_set():
            try:
                # One executor at a time shares the five-fetch ceiling across both queues.
                batches = (self.app.strategy.batch, self.batch) if sources_first else (self.batch, self.app.strategy.batch)
                busy = batches[0]() or batches[1]()
                sources_first = not sources_first
            except Exception:
                with self.app.connect() as db:
                    db.execute("UPDATE crawl_pages SET status='queued' WHERE status='running'")
                    db.execute("UPDATE crawl_jobs SET status='paused',message='Local crawl error. Check disk space and resume.' WHERE status='running'")
                    db.execute("UPDATE cited_pages SET status='queued' WHERE status='running'")
                    db.execute("UPDATE source_jobs SET paused=1,message='Local retrieval error. Check disk space and resume.' WHERE paused=0")
                busy = False
            if self.app.stop_event.wait(0.3 if busy else 1): break

    def listing(self, params):
        get = lambda name, default='': params.get(name, [default])[0]
        pid, page = int(get('project_id', '0')), max(1, int(get('page', '1')))
        conditions, args = ["project_id=?", "kind='page'"], [pid]
        search = str(get('search'))[:500].lower()
        if search:
            conditions.append('(instr(lower(url),?)>0 OR instr(lower(title),?)>0)'); args += [search, search]
        if get('path'): conditions.append('instr(url,?)>0'); args.append(str(get('path'))[:500])
        if get('status'):
            if get('status') not in ('queued', 'running', 'done', 'blocked', 'error', 'unsupported'): raise ValueError('Invalid crawl status.')
            conditions.append('status=?'); args.append(get('status'))
        if get('http'):
            code = int(get('http'))
            if not 100 <= code <= 599: raise ValueError('Enter an HTTP status from 100 to 599.')
            conditions.append('http_status=?'); args.append(code)
        if get('title') in ('missing', 'present'):
            conditions.append("successful<>'' AND title" + ("=''" if get('title') == 'missing' else "<>''"))
        for name in ('canonical_diff', 'restricted'):
            if get(name) == '1': conditions.append(name + '=1')
        for name, op in (('start', '>='), ('end', '<=')):
            if get(name):
                dt.date.fromisoformat(get(name)); conditions.append('substr(crawled,1,10)' + op + '?'); args.append(get(name))
        where = ' AND '.join(conditions)
        with self.app.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM crawl_pages WHERE ' + where, args).fetchone()[0]
            pages = max(1, (total + 99) // 100)
            page = min(page, pages)
            rows = [dict(r) for r in db.execute('SELECT id,url,status,title,final_url,http_status,crawled,successful,reason,canonical_diff,restricted FROM crawl_pages WHERE ' + where + ' ORDER BY id LIMIT 100 OFFSET ?', [*args, (page - 1) * 100])]
            counts = dict(db.execute("SELECT status,COUNT(*) FROM crawl_pages WHERE project_id=? AND kind='page' GROUP BY status", (pid,)).fetchall())
            job = db.execute('SELECT * FROM crawl_jobs WHERE project_id=?', (pid,)).fetchone()
            inclusion = db.execute('SELECT * FROM inclusion_checks WHERE project_id=?', (pid,)).fetchone()
            discovery = [dict(r) for r in db.execute("SELECT url,status,reason FROM crawl_pages WHERE project_id=? AND kind='sitemap' AND status IN ('error','blocked') ORDER BY id LIMIT 5", (pid,))]
            retry = db.execute("SELECT r.*,COUNT(p.page_id) AS total,COALESCE(SUM(p.status='done'),0) AS successful,COALESCE(SUM(p.status NOT IN ('done','queued')),0) AS unsuccessful FROM crawl_retries r JOIN crawl_retry_pages p ON p.run_id=r.id WHERE r.project_id=? GROUP BY r.id ORDER BY r.id DESC LIMIT 1", (pid,)).fetchone()
        return {'rows': rows, 'total': total, 'page': page, 'pages': pages, 'counts': counts,
                'job': dict(job) if job else None, 'inclusion': dict(inclusion) if inclusion else None,
                'discovery_warnings': discovery, 'limit': LIMIT, 'retry': dict(retry) if retry else None}

    def detail(self, pid, page_id):
        with self.app.connect() as db:
            row = db.execute("SELECT * FROM crawl_pages WHERE project_id=? AND id=? AND kind='page'", (int(pid), int(page_id))).fetchone()
        if not row: raise ValueError('Page not found in this project.')
        result = dict(row); result['result'] = json.loads(result['result'])
        return result

    def inclusion(self, body):
        status = body.get('status')
        if status not in ('unknown', 'included', 'excluded', 'inherited'): raise ValueError('Choose an inclusion status.')
        pid = int(body.get('project_id', 0))
        note = str(body.get('note', '')).strip()
        if len(note) > 2000: raise ValueError('Keep the note under 2,000 characters.')
        with self.app.connect() as db:
            if not db.execute('SELECT 1 FROM projects WHERE id=?', (pid,)).fetchone(): raise ValueError('Choose a project first.')
            db.execute('INSERT INTO inclusion_checks VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET status=excluded.status,checked=excluded.checked,note=excluded.note', (pid, status, utc(), note))
        return {'ok': True}

    def export(self, pid, retry_id=None):
        output = io.StringIO(newline=''); writer = csv.writer(output)
        writer.writerow(['URL', 'Meta title', 'Status', 'HTTP status', 'Final URL', 'Crawled UTC', 'Last successful UTC', 'Reason', 'Page information JSON'])
        with self.app.connect() as db:
            if retry_id is not None:
                run = db.execute("SELECT id FROM crawl_retries WHERE id=? AND project_id=? AND status='complete'", (int(retry_id),int(pid))).fetchone()
                if not run: raise ValueError('Completed retry not found in this project.')
                keys = ('url','title','status','http_status','final_url','crawled','successful','reason','result')
                records = (json.loads(r['evidence']) for r in db.execute("SELECT evidence FROM crawl_retry_pages WHERE run_id=? AND status<>'done' ORDER BY page_id", (run['id'],)))
                rows = ([r.get(k,'') for k in keys] for r in records)
            else:
                rows = db.execute("SELECT url,title,status,http_status,final_url,crawled,successful,reason,result FROM crawl_pages WHERE project_id=? AND kind='page' ORDER BY id", (int(pid),))
            for row in rows:
                writer.writerow([("'" + str(x)) if str(x).lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else x for x in row])
        return ('\ufeff' + output.getvalue()).encode(), 'text/csv; charset=utf-8', ('unsuccessful-retry-pages.csv' if retry_id is not None else 'website-pages.csv')
