"""Waddle: local browser UI, SQLite persistence and Bright Data SERP."""
import argparse
import base64
import csv
import ctypes
import datetime as dt
import hmac
import io
import json
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from contextlib import closing, contextmanager
from ctypes import wintypes
from http import cookies
from http.client import HTTPException, HTTPSConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from crawler import Crawler
from strategy import Strategy, query_key
from improvements import Improvements

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = Path(os.environ.get('LOCALAPPDATA', str(ROOT))) / 'AIOverviewTool'
MAX_RESPONSE = 32 * 1024 * 1024
GOOD = ('found', 'absent')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def domain(value):
    value = str(value).strip()
    parsed = urlsplit(value if '://' in value else 'https://' + value)
    if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password:
        raise ValueError('Enter a public domain such as example.com.')
    host = (parsed.hostname or '').rstrip('.').lower().encode('idna').decode('ascii')
    host = host.removeprefix('www.')
    if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host):
        raise ValueError('Enter a valid website domain, not an IP address or local host.')
    return host


def owned(url, host):
    try:
        actual = domain(url)
        return actual == host or actual.endswith('.' + host)
    except (ValueError, UnicodeError):
        return False


def project_values(body):
    name = str(body.get('name', '')).strip()
    host = domain(body.get('domain', ''))
    country = str(body.get('country', 'gb')).lower()
    language = str(body.get('language', 'en')).lower()
    device = body.get('device', 'desktop')
    if not name or len(name) > 100:
        raise ValueError('Enter a project name of 1 to 100 characters.')
    if not re.fullmatch('[a-z]{2}', country) or not re.fullmatch('[a-z]{2}(?:-[a-z]{2})?', language) or device not in ('desktop', 'mobile'):
        raise ValueError('Choose valid country, language and device settings.')
    return name, host, country, language, device, now()


def query_lines(value):
    if not isinstance(value, str) or len(value) > 100000:
        raise ValueError('Paste search phrases, one per line.')
    lines = list(dict.fromkeys(x.strip() for x in value.splitlines() if x.strip()))
    if not lines or len(lines) > 500 or any(len(x) > 500 for x in lines):
        raise ValueError('Add up to 500 queries at a time, each no longer than 500 characters.')
    return lines


def http_url(value):
    try:
        p = urlsplit(str(value))
        return str(value) if p.scheme in ('http', 'https') and p.hostname and not p.username else ''
    except ValueError:
        return ''


def text_blocks(blocks):
    result = []
    if not isinstance(blocks, list):
        return result
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for field in ('title', 'snippet', 'text', 'code'):
            if isinstance(block.get(field), str):
                result.append(block[field])
        result.extend(text_blocks(block.get('list', [])))
    return result


def parse_overview(raw, host):
    if not isinstance(raw, dict) or not any(k in raw for k in ('general', 'organic', 'ai_overview')):
        return 'incomplete', 'The provider response was not a recognizable Google SERP result.', {}, []
    general = raw.get('general', {})
    if isinstance(general, dict) and general.get('search_engine', 'google') != 'google':
        return 'incomplete', 'The provider returned results from a different search engine.', {}, []
    aio = raw.get('ai_overview')
    if aio is None:
        if not isinstance(raw.get('organic'), list):
            return 'incomplete', 'No AI Overview or valid search-results list was returned.', {}, []
        return 'absent', '', {}, []
    if not isinstance(aio, dict) or not text_blocks(aio.get('texts')):
        return 'incomplete', 'An AI Overview was reported, but its summary content is missing.', aio if isinstance(aio, dict) else {}, []
    refs, problems = [], []
    if not isinstance(aio.get('references'), list):
        problems.append('The AI Overview citation list is missing.')
    else:
        for i, ref in enumerate(aio['references']):
            if not isinstance(ref, dict):
                problems.append('A citation entry has an unsupported format.')
                continue
            href = http_url(ref.get('href', ''))
            if not href:
                problems.append('A citation is missing a valid source URL.')
                continue
            try:
                host_name = domain(href)
            except (ValueError, UnicodeError):
                host_name = urlsplit(href).hostname or ''
            refs.append({'href': href, 'title': str(ref.get('title', host_name)),
                         'source': str(ref.get('source', host_name)), 'domain': host_name,
                         'index': ref.get('index', i), 'owned': owned(href, host)})
    return ('incomplete' if problems else 'found'), ' '.join(dict.fromkeys(problems)), aio, refs


def protect(value, decrypt=False):
    if os.name != 'nt':
        raise ValueError('Protected credential storage requires Windows.')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(value)
    src = Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out)):
        raise ValueError('Windows could not access the protected API key.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        kernel.LocalFree(out.data)


class ProviderError(Exception):
    def __init__(self, message, unknown=False, diagnostics=None):
        super().__init__(message)
        self.unknown = unknown
        self.diagnostics = diagnostics or {}


class Provider:
    def __init__(self, app):
        self.app = app

    @staticmethod
    def response_metadata(headers):
        headers = {str(k).lower(): str(v) for k, v in headers.items()}
        result = {}
        for field, names in {
            'request_id': ('x-request-id', 'x-brd-request-id', 'x-response-id'),
            'provider_error_code': ('x-brd-error-code', 'x-brd-err-code'),
            'content_type': ('content-type',), 'transfer_encoding': ('transfer-encoding',),
            'content_encoding': ('content-encoding',),
        }.items():
            value = next((headers[n] for n in names if headers.get(n)), '')
            pattern = r'[a-zA-Z0-9_.:\-]{1,150}' if field in ('request_id', 'provider_error_code') else r'[a-zA-Z0-9_.:;/= +\-]{1,150}'
            if value and re.fullmatch(pattern, value):
                result[field] = value
        for field, name in (('content_length', 'content-length'), ('retry_after_seconds', 'retry-after')):
            value = headers.get(name, '')
            if re.fullmatch(r'[0-9]{1,12}', value):
                result[field] = int(value)
        if headers.get('x-brd-status-code'):
            value = headers['x-brd-status-code']
            if re.fullmatch(r'[1-5][0-9]{2}', value):
                result['upstream_http_status'] = int(value)
            else:
                result['provider_error_reported'] = True
        message = headers.get('x-brd-error') or headers.get('x-brd-err-msg', '')
        if message:
            result['provider_error_reported'] = True
            match = re.fullmatch(r'waiting for selector "([#.a-zA-Z0-9_\- >:]{1,200})" failed: timeout ([0-9]{1,9})ms exceeded', message)
            if match:
                result['expected_selector'] = match[1]
                result['selector_timeout_ms'] = int(match[2])
        if 'request_id' not in result:
            match = re.search(r'(?:^|[;,\s])req_id=([a-zA-Z0-9_.:\-]{1,150})(?=$|[;,\s])', headers.get('x-brd-debug', ''))
            if match:
                result['request_id'] = match[1]
        return result

    @staticmethod
    def check_status(status, diagnostics):
        prefix = 'inner_' if 'inner_http_status' in diagnostics else ''
        code = diagnostics.get(prefix + 'provider_error_code')
        status = diagnostics.get(prefix + 'upstream_http_status', status)
        if code or diagnostics.get(prefix + 'provider_error_reported') or not 200 <= status < 300:
            messages = {400: 'Bright Data rejected the request or its zone configuration.',
                        401: 'Bright Data did not accept the API key.',
                        403: 'The API key lacks access to this Bright Data service.',
                        429: 'Bright Data rate-limited this request.',
                        502: 'Bright Data reported a service failure.',
                        503: 'Bright Data is unavailable.'}
            details = {'no_ready_cookies': 'Bright Data reported "No ready cookies" for this Google search.',
                       'expect_element': 'Bright Data could not find an expected element in the Google page.',
                       'captcha': 'Bright Data returned a CAPTCHA failure instead of Google results.'}
            message = details.get(code, messages.get(status, f'Bright Data returned HTTP {status}.'))
            if status == 200:
                message = details.get(code, 'Bright Data reported a collection error despite HTTP 200.')
            if diagnostics.get(prefix + 'expected_selector'):
                message += ' Expected page element: ' + diagnostics[prefix + 'expected_selector'] + '.'
            if code:
                message += ' Provider error: ' + code + '.'
            raise ProviderError(message +
                                ' The request was not automatically retried.', diagnostics=diagnostics)

    def request(self, endpoint, body=None, collection=False):
        key = self.app.key()
        if not key:
            raise ProviderError('Save a Bright Data API key in Settings first.')
        if endpoint not in ('/request', '/zone/get_active_zones'):
            raise ProviderError('Unsupported provider endpoint.')
        connection = HTTPSConnection('api.brightdata.com', timeout=180)
        if collection:
            self.app.record_request()
        diagnostics = {}
        started = time.monotonic()
        try:
            with closing(connection):
                connection.request('POST' if body is not None else 'GET', endpoint,
                                   body=json.dumps(body).encode() if body is not None else None,
                                   headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                diagnostics.update(self.response_metadata(dict(response.getheaders())))
                diagnostics['http_status'] = response.status
                data = response.read(MAX_RESPONSE + 1)
                diagnostics['bytes_received'] = len(data)
                diagnostics['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                if len(data) > MAX_RESPONSE:
                    raise ProviderError('The response exceeded the 32 MB capture limit.', diagnostics=diagnostics)
                # Bounded HTTPResponse.read does not report a short Content-Length body.
                if not response.chunked and diagnostics.get('content_length', len(data)) != len(data):
                    diagnostics['response_shape'] = 'truncated'
                    raise ProviderError('The response ended before its declared length. The request outcome is unknown; it was not automatically retried.', unknown=collection, diagnostics=diagnostics)
                self.check_status(response.status, diagnostics)
                warning = (response.getheader('x-brd-warning') or '')[:2000]
                try:
                    parsed = json.loads(data)
                except (ValueError, UnicodeError):
                    parsed = None
                if isinstance(parsed, dict) and 'status_code' in parsed:
                    status = parsed['status_code']
                    if type(status) is not int or not 100 <= status <= 599:
                        raise ProviderError('Bright Data returned an invalid response status envelope.', diagnostics=diagnostics)
                    diagnostics['inner_http_status'] = status
                    if isinstance(parsed.get('headers'), dict):
                        diagnostics.update({'inner_' + k: v for k, v in self.response_metadata(parsed['headers']).items()})
                    self.check_status(status, diagnostics)
                    if 'body' not in parsed or not isinstance(parsed['body'], (str, dict, list)):
                        raise ProviderError('Bright Data returned a response envelope without a readable body.', diagnostics=diagnostics)
                    parsed = parsed['body']
                    data = parsed.encode() if isinstance(parsed, str) else json.dumps(parsed).encode()
                    if isinstance(parsed, str):
                        try:
                            parsed = json.loads(parsed)
                        except (ValueError, UnicodeError):
                            parsed = None
                if isinstance(parsed, dict) and any(parsed.get(k) for k in ('error', 'errors', 'error_code')):
                    raise ProviderError('Bright Data returned a provider error instead of a SERP result. Inspect the safe response metadata; the error body was not retained.', diagnostics=diagnostics)
                return data, warning, diagnostics
        except (HTTPException, TimeoutError, ConnectionError, OSError) as error:
            diagnostics['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            diagnostics['transport_error'] = type(error).__name__
            if isinstance(error, IncompleteRead):
                diagnostics['bytes_received'] = len(error.partial)
                diagnostics['response_shape'] = 'truncated'
            raise ProviderError('The connection was interrupted. The request outcome is unknown; it was not automatically retried.', unknown=collection, diagnostics=diagnostics) from None

    def discover(self):
        data, _, _ = self.request('/zone/get_active_zones')
        try:
            zones = json.loads(data)
        except (ValueError, UnicodeError):
            raise ProviderError('Bright Data returned an unreadable zone list.') from None
        if isinstance(zones, dict):
            zones = zones.get('zones', [])
        if not isinstance(zones, list):
            raise ProviderError('Bright Data returned an unsupported zone list.')
        matching = [z['name'] for z in zones if isinstance(z, dict) and isinstance(z.get('name'), str)
                    and z.get('type') in ('serp', 'serp_api')]
        if not matching:
            raise ProviderError('No active SERP API service was found for this account. Enable a SERP service in Bright Data, then test again.')
        current = self.app.setting('zone', '')
        selected = current if current in matching else min(matching)
        self.app.set_setting('zone', selected)
        return selected

    def collect(self, project, query, screenshot=False):
        params = {'q': query, 'gl': project['country'], 'hl': project['language'],
                  'brd_ai_overview': '2', 'pws': '0'}
        if project['device'] == 'mobile':
            params['brd_mobile'] = '1'
        if not screenshot:
            params['brd_json'] = '1'
        body = {'zone': self.app.setting('zone', ''),
                'url': 'https://www.google.com/search?' + urlencode(params),
                'format': 'raw' if screenshot else 'json', 'country': project['country']}
        if screenshot:
            body['data_format'] = 'screenshot'
        return self.request('/request', body, collection=True)


class Application:
    def __init__(self, folder=DEFAULT_DATA, provider_factory=Provider):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = self.folder / 'workspace.sqlite3'
        if self.db.exists():
            with closing(sqlite3.connect(self.db)) as source:
                m3_backup = self.folder / 'pre-milestone3.sqlite3'
                if not source.execute("SELECT 1 FROM sqlite_master WHERE name='page_reviews'").fetchone() and not m3_backup.exists():
                    with closing(sqlite3.connect(m3_backup)) as destination:
                        source.backup(destination)
                m2_backup = self.folder / 'pre-milestone2.sqlite3'
                if not source.execute("SELECT 1 FROM sqlite_master WHERE name='query_plans'").fetchone() and not m2_backup.exists():
                    with closing(sqlite3.connect(m2_backup)) as destination:
                        source.backup(destination)
                columns = {r[1] for r in source.execute('PRAGMA table_info(captures)')}
                migration_backup = self.folder / 'pre-milestone1.sqlite3'
                if columns and 'diagnostics' not in columns and not migration_backup.exists():
                    with closing(sqlite3.connect(migration_backup)) as destination:
                        source.backup(destination)
        self.lock = threading.RLock()
        self.jobs = queue.Queue()
        self.pending = set()
        self.stop_event = threading.Event()
        self.last_saved = now()
        self.last_backup = ''
        self.worker = None
        self.provider = provider_factory(self)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT NOT NULL,domain TEXT NOT NULL,
                    country TEXT NOT NULL,language TEXT NOT NULL,device TEXT NOT NULL,created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS queries(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    text TEXT NOT NULL,paused INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL,UNIQUE(project_id,text));
                CREATE TABLE IF NOT EXISTS captures(id INTEGER PRIMARY KEY,query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
                    created TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',context TEXT NOT NULL,
                    raw TEXT,overview TEXT NOT NULL DEFAULT '{}',refs TEXT NOT NULL DEFAULT '[]',warning TEXT NOT NULL DEFAULT '',
                    screenshot BLOB,screenshot_time TEXT,screenshot_error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY,month TEXT NOT NULL,created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS capture_query ON captures(query_id,id);
            ''')
            db.execute("UPDATE captures SET status='failed',reason='Application stopped during collection; outcome unknown. Run another check manually.' WHERE status IN ('queued','running')")
            if 'diagnostics' not in {r['name'] for r in db.execute('PRAGMA table_info(captures)')}:
                db.execute("ALTER TABLE captures ADD COLUMN diagnostics TEXT NOT NULL DEFAULT '{}'")
        self.crawler = Crawler(self)
        self.strategy = Strategy(self, domain, owned)
        self.improvements = Improvements(self)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, key, default):
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row['value'] if row else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))

    def key(self):
        path = self.folder / 'credential.bin'
        return protect(path.read_bytes(), decrypt=True).decode() if path.exists() else ''

    def save_key(self, key):
        if not isinstance(key, str) or not 10 <= len(key.strip()) <= 512 or re.search(r'\s', key.strip()):
            raise ValueError('Enter a valid Bright Data API key.')
        value = protect(key.strip().encode())
        path = self.folder / 'credential.tmp'
        path.write_bytes(value)
        path.replace(self.folder / 'credential.bin')
        self.set_setting('zone', '')
        self.set_setting('connection', 'Key saved. Test the connection to discover the SERP service.')

    def record_request(self):
        with self.connect() as db:
            db.execute('INSERT INTO usage(month,created) VALUES (?,?)', (dt.datetime.now(dt.timezone.utc).astimezone().strftime('%Y-%m'), now()))

    def backup(self):
        with self.lock:
            target = self.folder / 'backup.tmp'
            with self.connect() as source, closing(sqlite3.connect(target)) as destination:
                source.backup(destination)
            target.replace(self.folder / 'workspace-backup.sqlite3')
            self.last_backup = now()
            self.last_saved = self.last_backup
        return {'saved': self.last_saved}

    def start(self):
        self.worker = threading.Thread(target=self.work, daemon=True)
        self.worker.start()
        threading.Thread(target=self.autosave, daemon=True).start()
        self.crawler.start_worker()
        self.improvements.start()

    def autosave(self):
        while not self.stop_event.wait(300):
            try:
                self.backup()
            except (OSError, sqlite3.Error):
                self.set_setting('save_error', 'The local backup could not be written. Check free disk space.')

    def add_project(self, body):
        values = project_values(body)
        with self.connect() as db:
            cur = db.execute('INSERT INTO projects(name,domain,country,language,device,created) VALUES (?,?,?,?,?,?)',
                             values)
            return {'id': cur.lastrowid}

    def setup_project(self, body):
        setup_id = body.get('setup_id', '')
        if not isinstance(setup_id, str) or not re.fullmatch('[a-zA-Z0-9-]{16,64}', setup_id):
            raise ValueError('The setup draft is invalid. Start a new draft.')
        key = 'setup:' + setup_id
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            if saved:
                result = json.loads(saved['value'])
                guidance = db.execute('SELECT value FROM settings WHERE key=?', ('onboarding:' + str(result['id']),)).fetchone()
                if not db.execute('SELECT 1 FROM projects WHERE id=?', (result['id'],)).fetchone() or not guidance or json.loads(guidance['value']).get('first_request') != setup_id:
                    raise ValueError('This draft created a project that was subsequently deleted. Discard the draft to begin again.')
                return result
            values = project_values(body)
            text = body.get('text', '')
            if not isinstance(text, str):
                raise ValueError('Paste search phrases, one per line.')
            lines = query_lines(text) if text else []
            pid = db.execute('INSERT INTO projects(name,domain,country,language,device,created) VALUES (?,?,?,?,?,?)', values).lastrowid
            ids, seen = [], set()
            for line in lines:
                if query_key(line) in seen:
                    continue
                seen.add(query_key(line))
                ids.append(db.execute('INSERT INTO queries(project_id,text,created) VALUES (?,?,?)', (pid, ' '.join(line.split()), now())).lastrowid)
            result = {'id': pid, 'query_ids': ids}
            db.execute('INSERT INTO settings VALUES (?,?)', (key, json.dumps(result)))
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('onboarding:' + str(pid), json.dumps({'dismissed': False, 'result_seen': False, 'first_request': setup_id})))
        self.last_saved = now()
        return result

    def onboarding_guidance(self, body):
        pid = int(body.get('project_id', 0))
        with self.lock, self.connect() as db:
            if not db.execute('SELECT 1 FROM projects WHERE id=?', (pid,)).fetchone():
                raise ValueError('Choose a project first.')
            key = 'onboarding:' + str(pid)
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            value = json.loads(row['value']) if row else {'dismissed': True, 'result_seen': False}
            for field in ('dismissed', 'result_seen'):
                if field in body:
                    if type(body[field]) is not bool:
                        raise ValueError('Invalid guidance preference.')
                    value[field] = body[field]
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))
        return value

    def add_queries(self, body):
        project = int(body.get('project_id', 0))
        lines = query_lines(body.get('text', ''))
        with self.lock, self.connect() as db:
            if not db.execute('SELECT id FROM projects WHERE id=?', (project,)).fetchone():
                raise ValueError('Choose a project first.')
            added = 0
            existing = {query_key(r['text']) for r in db.execute('SELECT text FROM queries WHERE project_id=?', (project,))}
            for line in lines:
                key = query_key(line)
                if key in existing: continue
                added += db.execute('INSERT OR IGNORE INTO queries(project_id,text,created) VALUES (?,?,?)', (project, ' '.join(line.split()), now())).rowcount
                existing.add(key)
        self.last_saved = now()
        return {'added': added, 'duplicates': len(lines) - added}

    def enqueue(self, body):
        if not self.setting('zone', ''):
            self.provider.discover()
        with self.lock, self.connect() as db:
            project = db.execute('SELECT * FROM projects WHERE id=?', (int(body.get('project_id', 0)),)).fetchone()
            if not project:
                raise ValueError('Choose a project first.')
            request_id = body.get('request_id', '')
            if not isinstance(request_id, str) or request_id and not re.fullmatch('[a-zA-Z0-9-]{16,80}', request_id):
                raise ValueError('Invalid check request.')
            request_key = 'first-check:' + str(project['id']) + ':' + request_id
            if request_id:
                saved = db.execute('SELECT value FROM settings WHERE key=?', (request_key,)).fetchone()
                if saved:
                    return json.loads(saved['value'])
            selected = body.get('query_ids')
            if selected is not None and (not isinstance(selected, list) or len(selected) > 500 or any(type(x) is not int for x in selected)):
                raise ValueError('Invalid query selection.')
            queries = db.execute('SELECT * FROM queries WHERE project_id=? AND paused=0', (project['id'],)).fetchall()
            queued = []
            for query in queries:
                if query['id'] in self.pending or (selected is not None and query['id'] not in selected):
                    continue
                context = {k: project[k] for k in ('domain', 'country', 'language', 'device')}
                context['query'] = query['text']
                capture = db.execute('INSERT INTO captures(query_id,created,status,context) VALUES (?,?,?,?)',
                                     (query['id'], now(), 'queued', json.dumps(context))).lastrowid
                self.pending.add(query['id'])
                queued.append((query['id'], capture, dict(project), query['text']))
            if request_id:
                db.execute('INSERT INTO settings VALUES (?,?)', (request_key, json.dumps({'queued': len(queued)})))
            # Commit before the worker can see these capture IDs.
        for job in queued:
            self.jobs.put(job)
        return {'queued': len(queued)}

    def work(self):
        while not self.stop_event.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            query_id, capture_id, project, query = job
            try:
                with self.connect() as db:
                    current = db.execute('SELECT paused FROM queries WHERE id=?', (query_id,)).fetchone()
                    if not current or current['paused']:
                        db.execute("UPDATE captures SET status='cancelled',reason='Paused before dispatch.' WHERE id=?", (capture_id,))
                        continue
                    db.execute("UPDATE captures SET status='running',created=? WHERE id=?", (now(), capture_id))
                self.capture(capture_id, project, query)
            except (OSError, sqlite3.Error, ValueError, UnicodeError):
                with self.connect() as db:
                    db.execute("UPDATE captures SET status='failed',reason='A local storage or credential error prevented collection. Check Settings and retry manually.' WHERE id=?", (capture_id,))
            finally:
                with self.lock:
                    self.pending.discard(query_id)
                self.jobs.task_done()

    def capture(self, capture_id, project, query):
        try:
            data, warning, diagnostics = self.provider.collect(project, query)
            try:
                raw = json.loads(data)
            except (ValueError, UnicodeError):
                diagnostics['response_shape'] = 'empty' if not data.strip() else 'HTML' if data.lstrip().startswith(b'<') else 'non-JSON'
                raise ProviderError('Bright Data returned ' + diagnostics['response_shape'] + ' instead of readable SERP JSON. Inspect the safe response metadata below; no response body or credentials were retained.', diagnostics=diagnostics) from None
            status, reason, overview, refs = parse_overview(raw, project['domain'])
            general = raw.get('general', {}) if isinstance(raw, dict) else {}
            if isinstance(general, dict) and general.get('detected_query') and str(general['detected_query']).casefold() != query.casefold():
                status, reason = 'incomplete', 'Google returned results for a different query: ' + str(general['detected_query'])[:500]
            quality = []
            if any(urlsplit(r['href']).path in ('', '/') for r in refs): quality.append('Some citations identify only a homepage; article paths cannot be inferred.')
            if status == 'found' and not refs: quality.append('The provider supplied an empty citation list. This result is incomplete for citation measurement.'); status = 'incomplete'
            if overview and not any('reference_indexes' in b for b in overview.get('texts', []) if isinstance(b, dict)): quality.append('Passage-to-source mappings were not supplied. No mappings inferred.')
            if overview.get('truncated') or overview.get('is_partial'):
                status, reason = 'incomplete', 'The provider marked the AI Overview as partial or truncated.'
            warning = ' '.join([warning, *quality]).strip()
            with self.connect() as db:
                db.execute('UPDATE captures SET status=?,reason=?,raw=?,overview=?,refs=?,warning=? WHERE id=?',
                           (status, reason, json.dumps(raw), json.dumps(overview), json.dumps(refs), warning, capture_id))
                db.execute('UPDATE captures SET diagnostics=? WHERE id=?', (json.dumps(diagnostics), capture_id))
            if status == 'found':
                try:
                    self.strategy.enqueue_capture(capture_id)
                except (ValueError, sqlite3.Error):
                    with self.connect() as db:
                        db.execute("UPDATE captures SET warning=warning || ' Cited-page queue could not be saved; use Analyse cited pages to retry.' WHERE id=?", (capture_id,))
            try:
                image, _, screenshot_diagnostics = self.provider.collect(project, query, screenshot=True)
                if not image.startswith(b'\x89PNG\r\n\x1a\n'):
                    screenshot_diagnostics['response_shape'] = 'empty' if not image.strip() else 'non-PNG'
                    raise ProviderError('The screenshot request did not return a PNG image.', diagnostics=screenshot_diagnostics)
                diagnostics.update({'screenshot_' + k: v for k, v in screenshot_diagnostics.items()})
                with self.connect() as db:
                    db.execute('UPDATE captures SET screenshot=?,screenshot_time=?,diagnostics=? WHERE id=?', (image, now(), json.dumps(diagnostics), capture_id))
            except ProviderError as error:
                diagnostics.update({'screenshot_' + k: v for k, v in error.diagnostics.items()})
                with self.connect() as db:
                    db.execute('UPDATE captures SET screenshot_error=?,diagnostics=? WHERE id=?', (str(error), json.dumps(diagnostics), capture_id))
        except ProviderError as error:
            with self.connect() as db:
                db.execute("UPDATE captures SET status='failed',reason=?,diagnostics=? WHERE id=?", (str(error), json.dumps(error.diagnostics), capture_id))
        self.last_saved = now()

    def details(self, capture_id, include_raw=False):
        with self.connect() as db:
            record = db.execute('SELECT id,query_id,created,status,reason,context,raw,overview,refs,warning,screenshot_time,screenshot_error,screenshot IS NOT NULL AS has_screenshot FROM captures WHERE id=?', (int(capture_id),)).fetchone()
            if not record:
                raise ValueError('Capture not found.')
            result = dict(record)
            result['diagnostics'] = json.loads(db.execute('SELECT diagnostics FROM captures WHERE id=?', (int(capture_id),)).fetchone()[0])
            result['has_screenshot'] = bool(result['has_screenshot'])
            for field in ('context', 'overview', 'refs'):
                result[field] = json.loads(result[field])
            raw = result.pop('raw')
            if include_raw:
                result['raw'] = json.loads(raw) if raw else None
            previous = db.execute("SELECT id,status,refs,overview FROM captures WHERE query_id=? AND id<? AND context=? AND status IN ('found','absent') ORDER BY id DESC LIMIT 1", (record['query_id'], record['id'], record['context'])).fetchone()
        result['previous_id'] = previous['id'] if previous else None
        result['changes'] = None
        if result['status'] in GOOD and previous:
            before = dict(previous)
            before['refs'] = json.loads(before['refs'])
            before['overview'] = json.loads(before['overview'])
            old, new = {r['href'] for r in before['refs']}, {r['href'] for r in result['refs']}
            old_domains, new_domains = {r['domain'] for r in before['refs']}, {r['domain'] for r in result['refs']}
            result['changes'] = {'gained_urls': sorted(new - old), 'lost_urls': sorted(old - new),
                                 'gained_domains': sorted(new_domains - old_domains), 'lost_domains': sorted(old_domains - new_domains),
                                 'overview_changed': result['status'] != before['status'],
                                 'owned_before': any(r['owned'] for r in before['refs']), 'owned_now': any(r['owned'] for r in result['refs']),
                                 'text_changed': text_blocks(before['overview'].get('texts')) != text_blocks(result['overview'].get('texts'))}
        return result

    def state(self, project_id=0):
        with self.connect() as db:
            projects = [dict(x) for x in db.execute('SELECT * FROM projects ORDER BY id')]
            queries = [dict(x) for x in db.execute('SELECT * FROM queries WHERE project_id=? ORDER BY id', (project_id,))]
            for query in queries:
                plan = db.execute('''SELECT qp.*,p.title AS page_title,p.url AS page_url
                    FROM query_plans qp LEFT JOIN crawl_pages p ON p.id=qp.page_id AND p.project_id=?
                    WHERE qp.query_id=?''', (project_id, query['id'])).fetchone()
                query['plan'] = dict(plan) if plan else None
                latest = db.execute('SELECT id FROM captures WHERE query_id=? ORDER BY id DESC LIMIT 1', (query['id'],)).fetchone()
                query['latest'] = self.details(latest['id']) if latest else None
                query['pending'] = query['id'] in self.pending
                # A failed check must not fabricate a loss of a previously observed citation.
                good = db.execute("SELECT id FROM captures WHERE query_id=? AND status IN ('found','absent') ORDER BY id DESC LIMIT 1", (query['id'],)).fetchone()
                query['last_usable'] = self.details(good['id']) if good else None
            month = dt.datetime.now(dt.timezone.utc).astimezone().strftime('%Y-%m')
            usage = db.execute('SELECT COUNT(*) FROM usage WHERE month=?', (month,)).fetchone()[0]
            metrics = {'usable': 0, 'overviews': 0, 'cited': 0, 'failed': 0, 'incomplete': 0, 'start': None, 'end': None}
            for row in db.execute('SELECT c.status,c.refs,c.created FROM captures c JOIN queries q ON q.id=c.query_id WHERE q.project_id=?', (project_id,)):
                if row['status'] in GOOD:
                    metrics['usable'] += 1
                    metrics['overviews'] += row['status'] == 'found'
                    metrics['cited'] += any(r.get('owned') for r in json.loads(row['refs']))
                    metrics['start'] = min(metrics['start'] or row['created'], row['created'])
                    metrics['end'] = max(metrics['end'] or row['created'], row['created'])
                elif row['status'] in ('failed', 'incomplete'): metrics[row['status']] += 1
            metrics['stale_queries'] = sum(bool(q['last_usable'] and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(q['last_usable']['created'])).days >= 7) for q in queries)
            preference = db.execute('SELECT value FROM settings WHERE key=?', ('onboarding:' + str(project_id),)).fetchone()
            onboarding = json.loads(preference['value']) if preference else None
            if onboarding is not None:
                onboarding['pages'] = db.execute("SELECT COUNT(*) FROM crawl_pages WHERE project_id=? AND kind='page' AND status='done'", (project_id,)).fetchone()[0]
                job = db.execute('SELECT status FROM crawl_jobs WHERE project_id=?', (project_id,)).fetchone()
                onboarding['crawl_status'] = job['status'] if job else ''
                review = db.execute('SELECT id,status FROM page_reviews WHERE project_id=? ORDER BY id DESC LIMIT 1', (project_id,)).fetchone()
                onboarding['review'] = dict(review) if review else None
                onboarding['briefs'] = db.execute('SELECT COUNT(*) FROM brief_versions b JOIN page_reviews r ON r.id=b.review_id WHERE r.project_id=?', (project_id,)).fetchone()[0]
                onboarding['tasks'] = db.execute('SELECT COUNT(*) FROM improvement_tasks WHERE project_id=?', (project_id,)).fetchone()[0]
        return {'milestone': 3, 'projects': projects, 'queries': queries, 'usage': usage, 'month': month, 'guide': 500,
                'warning': usage >= 400, 'key_saved': (self.folder / 'credential.bin').exists(),
                'connection': self.setting('connection', 'Not tested'), 'zone_ready': bool(self.setting('zone', '')),
                'saved': self.last_saved, 'backup': self.last_backup, 'data_folder': str(self.folder),
                'save_error': self.setting('save_error', ''), 'pending_count': len(self.pending), 'metrics': metrics, 'onboarding': onboarding}

    def history(self, query_id):
        with self.connect() as db:
            ids = db.execute('SELECT id FROM captures WHERE query_id=? ORDER BY id DESC', (int(query_id),)).fetchall()
        return [self.details(x['id']) for x in ids]

    def mutate_query(self, body, delete=False):
        query_id = int(body.get('id', 0))
        with self.lock, self.connect() as db:
            if query_id in self.pending:
                raise ValueError('Wait for this query check to finish before changing it.')
            if delete:
                db.execute('DELETE FROM queries WHERE id=?', (query_id,))
            else:
                db.execute('UPDATE queries SET paused=? WHERE id=?', (1 if body.get('paused') else 0, query_id))
        self.last_saved = now()
        return {'ok': True}

    def export(self, params):
        project_id = int(params.get('project_id', ['0'])[0])
        kind = params.get('kind', ['queries'])[0]
        if kind == 'pages': return self.crawler.export(project_id)
        if kind in ('opportunities', 'competitors'): return self.strategy.export(params)
        if kind == 'crawl-retry': return self.crawler.export(project_id, params.get('run_id', ['0'])[0])
        start, end = params.get('start', [''])[0], params.get('end', [''])[0]
        for value in (start, end):
            if value:
                dt.date.fromisoformat(value)
        if start and end and start > end:
            raise ValueError('The start date must be before the end date.')
        with self.connect() as db:
            records = db.execute('SELECT c.id,q.text FROM captures c JOIN queries q ON q.id=c.query_id WHERE q.project_id=? ORDER BY c.id', (project_id,)).fetchall()
        data = []
        for row in records:
            capture = self.details(row['id'], include_raw=kind == 'json')
            day = capture['created'][:10]
            if (start and day < start) or (end and day > end):
                continue
            capture['query'] = row['text']
            if kind == 'json' and capture['has_screenshot']:
                with self.connect() as db:
                    image = db.execute('SELECT screenshot FROM captures WHERE id=?', (row['id'],)).fetchone()[0]
                capture['screenshot_png_base64'] = base64.b64encode(image).decode()
            data.append(capture)
        if kind == 'json':
            return json.dumps(data, indent=2).encode(), 'application/json', 'captures.json'
        if kind not in ('queries', 'citations'):
            raise ValueError('Choose a valid export type.')
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        fields = ['Query', 'Captured UTC', 'Result', 'Reason', 'Country', 'Language', 'Device']
        writer.writerow(fields + (['Domain', 'URL', 'Title', 'Owned'] if kind == 'citations' else ['Owned citation', 'Citation count']))
        def safe(value):
            value = str(value)
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value
        for capture in data:
            context = capture['context']
            prefix = [capture['query'], capture['created'], capture['status'], capture['reason'], context['country'], context['language'], context['device']]
            rows = [[*prefix, r['domain'], r['href'], r['title'], r['owned']] for r in capture['refs']] if kind == 'citations' else [[*prefix, any(r['owned'] for r in capture['refs']), len(capture['refs'])]]
            for row in rows:
                writer.writerow([safe(x) for x in row])
        return ('\ufeff' + output.getvalue()).encode(), 'text/csv; charset=utf-8', kind + '.csv'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Search text, credentials and provider bodies never enter request logs.

    def send(self, data, content_type='application/json', status=200, headers=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, api=False, mutation=False):
        origin = self.server.origin
        if self.headers.get('Host') != urlsplit(origin).netloc:
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return False
        if self.headers.get('Origin') not in (None, origin):
            return False
        if api:
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get('Cookie', ''))
            except cookies.CookieError:
                return False
            item = jar.get('workspace_' + str(self.server.server_port))
            if not item or not hmac.compare_digest(item.value, self.server.token):
                return False
        return not mutation or hmac.compare_digest(self.headers.get('X-Workspace-Token', ''), self.server.token)

    def do_GET(self):
        path = urlsplit(self.path).path
        api = path.startswith('/api/')
        if not self.allowed(api=api):
            return self.send({'error': 'Local workspace access denied.'}, status=403)
        app = self.server.app
        params = parse_qs(urlsplit(self.path).query)
        try:
            if path == '/api/state':
                result = app.state(int(params.get('project_id', ['0'])[0]))
                result['token'] = self.server.token
                return self.send(result)
            if path == '/api/history':
                return self.send(app.history(params.get('query_id', ['0'])[0]))
            if path == '/api/pages':
                return self.send(app.crawler.listing(params))
            if path == '/api/strategy':
                return self.send(app.strategy.listing(params))
            if path == '/api/improvements':
                return self.send(app.improvements.listing(params))
            if path == '/api/ollama':
                return self.send(app.improvements.settings())
            if path == '/api/brief-export':
                review = app.improvements.listing(params)
                if not review.get('brief'):
                    raise ValueError('Save a brief before downloading it.')
                return self.send(review['brief'].encode('utf-8'), 'text/markdown; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="page-brief-' + str(review['id']) + '.md"'})
            if path == '/api/page-suggestions':
                return self.send(app.strategy.suggestions(int(params.get('project_id',['0'])[0]), int(params.get('query_id',['0'])[0])))
            if path == '/api/page':
                return self.send(app.crawler.detail(params.get('project_id', ['0'])[0], params.get('id', ['0'])[0]))
            if path == '/api/capture':
                return self.send(app.details(params.get('id', ['0'])[0]))
            if path == '/api/screenshot':
                with app.connect() as db:
                    row = db.execute('SELECT screenshot FROM captures WHERE id=?', (int(params.get('id', ['0'])[0]),)).fetchone()
                if not row or row['screenshot'] is None:
                    return self.send({'error': 'Screenshot not available.'}, status=404)
                return self.send(row['screenshot'], 'image/png')
            if path == '/api/export':
                data, content_type, name = app.export(params)
                return self.send(data, content_type, headers={'Content-Disposition': 'attachment; filename="' + name + '"'})
            static = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/onboarding.js': ('onboarding.js', 'text/javascript; charset=utf-8'),
                      '/pages.js': ('pages.js', 'text/javascript; charset=utf-8'),
                      '/strategy.js': ('strategy.js', 'text/javascript; charset=utf-8'),
                      '/improvements.js': ('improvements.js', 'text/javascript; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8'),
                      '/assets/waddle-horizontal-colour.png': ('assets/waddle-horizontal-colour.png', 'image/png'),
                      '/assets/waddle-penguin-icon.png': ('assets/waddle-penguin-icon.png', 'image/png'),
                      '/assets/favicon-16.png': ('assets/favicon-16.png', 'image/png'),
                      '/assets/favicon-32.png': ('assets/favicon-32.png', 'image/png'),
                      '/assets/fonts/PlusJakartaSans.ttf': ('assets/fonts/PlusJakartaSans.ttf', 'font/ttf'),
                      '/assets/fonts/OFL.txt': ('assets/fonts/OFL.txt', 'text/plain; charset=utf-8')}
            for name in ('01-welcome', '02-your-website', '03-your-searches', '04-ready-to-check', '05-check-in-progress', '06-first-result', '07-project-ready', '08-needs-attention', '09-explore-your-website', '10-improve-a-page'):
                static['/assets/onboarding/' + name + '.png'] = ('assets/onboarding/' + name + '.png', 'image/png')
            if path not in static:
                return self.send({'error': 'Not found.'}, status=404)
            filename, mime = static[path]
            headers = {'Set-Cookie': 'workspace_' + str(self.server.server_port) + '=' + self.server.token + '; HttpOnly; SameSite=Strict; Path=/'} if path == '/' else {}
            return self.send((ROOT / 'web' / filename).read_bytes(), mime, headers=headers)
        except (ValueError, TypeError, UnicodeError) as error:
            return self.send({'error': str(error)}, status=400)
        except (OSError, sqlite3.Error):
            return self.send({'error': 'The local workspace could not be read.'}, status=500)

    def do_POST(self):
        if not self.allowed(api=True, mutation=True):
            return self.send({'error': 'Local workspace access denied.'}, status=403)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 1 or size > 200000:
                raise ValueError('Request size is invalid.')
            if self.headers.get('Content-Type') != 'application/json':
                raise ValueError('Expected a JSON request.')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise TypeError('Expected a JSON object.')
            app = self.server.app
            path = urlsplit(self.path).path
            if path == '/api/projects':
                result = app.add_project(body)
            elif path == '/api/project-setup':
                result = app.setup_project(body)
            elif path == '/api/onboarding-guidance':
                result = app.onboarding_guidance(body)
            elif path == '/api/queries':
                result = app.add_queries(body)
            elif path == '/api/run':
                result = app.enqueue(body)
            elif path == '/api/crawl':
                result = app.crawler.control(body)
            elif path == '/api/strategy':
                result = app.strategy.mutate(body)
            elif path == '/api/improvements':
                result = app.improvements.mutate(body)
            elif path == '/api/ollama':
                result = app.improvements.settings(test=True) if body.get('action') == 'test' else app.improvements.configure(body)
            elif path == '/api/inclusion':
                result = app.crawler.inclusion(body)
            elif path in ('/api/pause', '/api/delete-query'):
                result = app.mutate_query(body, delete=path == '/api/delete-query')
            elif path == '/api/save':
                result = app.backup()
            elif path == '/api/key':
                if app.pending:
                    raise ValueError('Wait for active checks to finish before replacing the API key.')
                app.save_key(body.get('key', ''))
                result = {'ok': True}
            elif path == '/api/test-connection':
                try:
                    app.provider.discover()
                except ProviderError as error:
                    app.set_setting('zone', '')
                    app.set_setting('connection', str(error))
                    raise
                app.set_setting('connection', 'Connected to Bright Data SERP API.')
                result = {'message': 'Connected to Bright Data SERP API. No search requests used.'}
            elif path == '/api/delete-project':
                with app.lock, app.connect() as db:
                    project_id = int(body.get('id', 0))
                    ids = {r['id'] for r in db.execute('SELECT id FROM queries WHERE project_id=?', (project_id,))}
                    if ids & app.pending:
                        raise ValueError('Wait for active checks to finish before deleting this project.')
                    if db.execute("SELECT 1 FROM page_reviews WHERE project_id=? AND status IN ('queued','running','refreshing')", (project_id,)).fetchone():
                        raise ValueError('Cancel active page reviews before deleting this project.')
                    if db.execute("SELECT 1 FROM cited_pages WHERE project_id=? AND status='running'", (project_id,)).fetchone():
                        raise ValueError('Pause cited-page retrieval and wait for its batch to finish before deleting the project.')
                    if db.execute("SELECT 1 FROM crawl_jobs WHERE project_id=? AND status='running'", (project_id,)).fetchone() or db.execute("SELECT 1 FROM crawl_pages WHERE project_id=? AND status='running'", (project_id,)).fetchone():
                        raise ValueError('Pause the website crawl and wait for the batch to finish before deleting the project.')
                    db.execute('DELETE FROM projects WHERE id=?', (project_id,))
                    db.execute('DELETE FROM settings WHERE key=?', ('onboarding:' + str(project_id),))
                result = {'ok': True}
            elif path == '/api/close':
                if app.pending:
                    raise ValueError('Wait for active checks to finish before closing the app.')
                with app.connect() as db:
                    if db.execute("SELECT 1 FROM page_reviews WHERE status IN ('queued','running','refreshing')").fetchone():
                        raise ValueError('Cancel active page reviews before closing the app. Your evidence will be preserved.')
                    if db.execute("SELECT 1 FROM cited_pages WHERE status='running'").fetchone():
                        raise ValueError('Pause cited-page retrieval and wait for its batch to finish before closing the app.')
                    if db.execute("SELECT 1 FROM crawl_jobs WHERE status='running'").fetchone() or db.execute("SELECT 1 FROM crawl_pages WHERE status='running'").fetchone():
                        raise ValueError('Pause website crawls and wait for in-flight pages to finish before closing the app.')
                app.backup()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                result = {'ok': True}
            else:
                return self.send({'error': 'Not found.'}, status=404)
            return self.send(result)
        except ProviderError as error:
            self.server.app.set_setting('connection', str(error))
            return self.send({'error': str(error)}, status=400)
        except (ValueError, TypeError, UnicodeError) as error:
            return self.send({'error': str(error)}, status=400)
        except (OSError, sqlite3.Error):
            return self.send({'error': 'The local workspace could not be saved. Check free disk space.'}, status=500)


def make_server(app, port=8765):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.app = app
    server.origin = 'http://127.0.0.1:' + str(server.server_port)
    server.token = secrets.token_urlsafe(32)
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--configure-key-stdin', action='store_true')
    args = parser.parse_args()
    app = Application(args.data_dir)
    if args.configure_key_stdin:
        app.save_key(sys.stdin.read().strip())
        print('API key saved using Windows protected storage.')
        return
    try:
        server = make_server(app, args.port)
    except OSError:
        print('The local port is already in use. Close the existing instance or choose another port.')
        return
    app.start()
    if not args.no_browser:
        webbrowser.open(server.origin)
    print('Waddle is running at ' + server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_event.set()
        app.backup()
        server.server_close()


if __name__ == '__main__':
    main()
