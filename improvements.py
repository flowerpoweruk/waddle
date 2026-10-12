"""Page-first reviews grounded in immutable evidence and local Ollama."""
import datetime as dt
import json
import re
import threading
import time
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlsplit

from crawler import utc, normalize
from review_pipeline import Pipeline, InputRejected, readiness, VERSION

CATEGORIES = ('Question coverage', 'Original evidence', 'Answer clarity', 'Technical')
LABELS = ('Observed fact', 'Editorial suggestion', 'Hypothesis')
TASK_STATUS = ('To do', 'In progress', 'Blocked', 'Done')
SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['assessment', 'recommendations'],
    'properties': {'assessment': {'type': 'string'}, 'recommendations': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False,
        'required': ['category', 'label', 'title', 'rationale', 'change', 'evidence_ids', 'evidence_quotes'],
        'properties': {'category': {'type': 'string', 'enum': list(CATEGORIES[:-1])},
            'label': {'type': 'string', 'enum': list(LABELS[1:])},
            'title': {'type': 'string'}, 'rationale': {'type': 'string'}, 'change': {'type': 'string'},
            'evidence_ids': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}},
            'evidence_quotes': {'type': 'object', 'additionalProperties': {'type': 'string'}}}}}}}


def evidence_text(record):
    if not record['id'].startswith('capture:'):
        return record.get('content', '')
    overview = record['overview']
    if isinstance(overview, str):
        overview = json.loads(overview)
    def blocks(items):
        return '\n'.join(str(b.get('title', '')) + '\n' + str(b.get('snippet') or b.get('text') or b.get('code') or '') + '\n' + blocks(b.get('list', [])) for b in items if isinstance(b, dict))
    return blocks(overview.get('texts', []))


def compact_overview(value):
    """Keep answer passages and their source connections, never provider presentation data."""
    if isinstance(value, str):
        return re.sub(r'data:image/[^\s"<>]+', '', value).strip()
    if isinstance(value, list):
        return [clean for item in value if (clean := compact_overview(item)) not in ('', None, [], {})]
    if not isinstance(value, dict):
        return value
    result = {}
    for key in ('texts', 'type', 'title', 'snippet', 'text', 'code', 'list', 'reference_indexes', 'links'):
        if key not in value: continue
        if key == 'links':
            links = {}
            for item in value[key]:
                if not isinstance(item, dict): continue
                url = item.get('link') or item.get('href')
                if not url: continue
                try: url = normalize(url)
                except ValueError: continue
                links.setdefault(url, dict(link=url))
                if item.get('text') and (label := compact_overview(item['text'])): links[url]['text'] = label
            clean = list(links.values())
        else:
            clean = compact_overview(value[key])
        if clean not in ('', None, [], {}): result[key] = clean
    return result


def address(value):
    p = urlsplit(str(value).strip())
    try:
        port = p.port if p.port is not None else 11434
    except ValueError:
        raise ValueError('Enter a loopback Ollama address with a valid port.') from None
    if (p.scheme != 'http' or p.hostname not in ('127.0.0.1', 'localhost', '::1') or
            p.username is not None or p.password is not None or p.path not in ('', '/') or p.query or p.fragment or not 1 <= port <= 65535):
        raise ValueError('Ollama must use HTTP on this computer: http://127.0.0.1:11434.')
    return ('::1' if p.hostname == '::1' else '127.0.0.1'), port


def bounded(value, maximum=4000):
    if not isinstance(value, str) or (maximum is not None and len(value) > maximum):
        raise ValueError('Text is missing or exceeds the allowed length.')
    return value.strip()


class Improvements:
    def __init__(self, app):
        self.app = app
        self.wake = threading.Event()
        self.worker = None
        with app.connect() as db:
            db.executescript('''
                BEGIN;
                CREATE TABLE IF NOT EXISTS page_reviews(id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    page_id INTEGER REFERENCES crawl_pages(id) ON DELETE SET NULL,
                    created TEXT NOT NULL,finished TEXT NOT NULL DEFAULT '',title TEXT NOT NULL,
                    status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',model TEXT NOT NULL,address TEXT NOT NULL,
                    snapshot TEXT NOT NULL,brief TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS review_project ON page_reviews(project_id,id);
                CREATE TABLE IF NOT EXISTS review_stages(review_id INTEGER NOT NULL REFERENCES page_reviews(id) ON DELETE CASCADE,
                    stage_key TEXT NOT NULL,fingerprint TEXT NOT NULL,status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',PRIMARY KEY(review_id,stage_key));
                CREATE TABLE IF NOT EXISTS recommendations(id INTEGER PRIMARY KEY,
                    review_id INTEGER NOT NULL REFERENCES page_reviews(id) ON DELETE CASCADE,
                    category TEXT NOT NULL,label TEXT NOT NULL,title TEXT NOT NULL,rationale TEXT NOT NULL,
                    change TEXT NOT NULL,evidence_ids TEXT NOT NULL,decision TEXT NOT NULL DEFAULT 'Pending',
                    correction TEXT NOT NULL DEFAULT '',origin TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS brief_versions(id INTEGER PRIMARY KEY,
                    review_id INTEGER NOT NULL REFERENCES page_reviews(id) ON DELETE CASCADE,
                    created TEXT NOT NULL,content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS improvement_tasks(id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    review_id INTEGER NOT NULL REFERENCES page_reviews(id) ON DELETE CASCADE,
                    recommendation_id INTEGER NOT NULL REFERENCES recommendations(id),
                    kind TEXT NOT NULL,title TEXT NOT NULL,details TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'To do',due TEXT NOT NULL DEFAULT '',
                    created TEXT NOT NULL);
            ''')
            for table, field, default in [('page_reviews', 'assessment', ''), ('recommendations', 'grounding', '{}'),
                    ('improvement_tasks', 'deployed', ''), ('improvement_tasks', 'deployment_note', ''),
                    ('page_reviews', 'execution', '{}'), ('page_reviews', 'predecessor', ''), ('page_reviews', 'refresh', '{}')]:
                if field not in {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}:
                    db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + field + " TEXT NOT NULL DEFAULT '" + default + "'")
            db.execute("UPDATE page_reviews SET status='failed',error='Application stopped before this queued review started. Retry manually.',finished=? WHERE status='queued'", (utc(),))
            db.execute("UPDATE page_reviews SET status='failed',error='Application stopped during generation; outcome unknown. Retry manually.',finished=? WHERE status='running'", (utc(),))
            db.execute("UPDATE review_stages SET status='failed',error='outcome-unknown-after-restart' WHERE status='running'")
            db.execute("UPDATE page_reviews SET status='failed',error='Evidence refresh interrupted. Refresh evidence again manually.',finished=? WHERE status='refreshing'", (utc(),))

    def request(self, endpoint, body=None, endpoint_address=None, timeout=15):
        if endpoint not in ('/api/version', '/api/tags', '/api/show', '/api/ps', '/api/chat'):
            raise ValueError('Unsupported Ollama action.')
        host, port = address(endpoint_address or self.app.setting('ollama_address', 'http://127.0.0.1:11434'))
        conn = HTTPConnection(host, port, timeout=15 if timeout is None else timeout)
        try:
            deadline = time.monotonic() + timeout if timeout is not None else None
            conn.connect()
            transport = conn.sock
            def remaining():
                if deadline is None:
                    transport.settimeout(None)
                    return
                seconds = deadline - time.monotonic()
                if seconds <= 0:
                    raise TimeoutError()
                transport.settimeout(seconds)
            remaining()
            conn.request('POST' if body is not None else 'GET', endpoint,
                body=json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type': 'application/json'})
            remaining()
            response = conn.getresponse()
            if not 200 <= response.status < 300:
                # Read only enough to classify a failure; never expose raw model/prompt bodies.
                error_data = response.read(8192).lower()
                if any(marker in error_data for marker in (b'context length', b'context window', b'context size', b'input length', b'prompt too long', b'context limit')):
                    counts = re.search(rb'request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)', error_data)
                    if counts:
                        required, available = (int(n) for n in counts.groups())
                        raise InputRejected(required, available)
                    raise ValueError('This stage exceeded context or generation capacity. Its dispatch outcome is not confirmed; resume manually. Completed stages and original evidence are preserved.')
                messages = {404: 'The selected model or Ollama endpoint is unavailable. Refresh models in Settings.',
                    500: 'Ollama could not run the model. Check available memory and the Ollama application.',
                    503: 'Ollama is busy or unavailable. Wait and retry manually.'}
                raise ValueError(messages.get(response.status, f'Ollama returned HTTP {response.status}. Check the local Ollama application.'))
            data = bytearray()
            while len(data) <= 2 * 1024 * 1024:
                remaining()
                chunk = response.read1(min(65536, 2 * 1024 * 1024 + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError('Ollama response exceeded the 2 MB limit.')
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError('Ollama returned an invalid response object.')
            return result
        except (TimeoutError, ConnectionError, OSError, HTTPException):
            raise ValueError('Ollama could not connect or timed out. Ensure Ollama is running at the saved address; check memory and retry manually.') from None
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError('Ollama returned unreadable JSON. No AI recommendations were saved.') from None
        finally:
            conn.close()

    @staticmethod
    def local_model(model):
        name = str(model.get('name', ''))
        return bool(name and not model.get('remote_host') and not model.get('remote_model') and 'cloud' not in name.lower())

    def settings(self, test=False):
        result = {'address': self.app.setting('ollama_address', 'http://127.0.0.1:11434'),
                  'model': self.app.setting('ollama_model', ''), 'models': [], 'message': '', 'version': ''}
        if test:
            result['version'] = str(self.request('/api/version').get('version', 'Unknown'))
            tags = self.request('/api/tags').get('models')
            if not isinstance(tags, list):
                raise ValueError('Ollama returned an invalid installed-model list.')
            for item in tags:
                if not isinstance(item, dict) or not self.local_model(item):
                    continue
                info = self.request('/api/show', {'model': item['name']})
                if self.local_model({**info, 'name': item['name']}) and 'completion' in info.get('capabilities', []):
                    result['models'].append(item['name'])
                    if item['name'] == result['model']:
                        result['model_context'] = max((v for k,v in info.get('model_info', {}).items() if k.endswith('.context_length') and type(v) is int), default=None)
            result['message'] = ('Connected. Choose an installed local model; no content was generated.' if result['models']
                                 else 'Connected, but no installed local text models are available. Install one in Ollama first.')
            if result['model'] and result['model'] not in result['models']:
                result['message'] += ' Your saved model is missing or is not a local text model.'
            active = self.request('/api/ps').get('models', [])
            result['active_context'] = next((m.get('context_length') for m in active if m.get('name') == result['model']), None)
        return result

    def configure(self, body):
        value = bounded(body.get('address', ''), 200)
        address(value)
        model = bounded(body.get('model', ''), 200)
        if model and 'cloud' in model.lower():
            raise ValueError('Choose an installed local model, not a cloud model.')
        with self.app.connect() as db:
            db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)', [('ollama_address', value), ('ollama_model', model)])
        return {'ok': True}

    def review(self, db, pid, rid):
        row = db.execute('SELECT * FROM page_reviews WHERE project_id=? AND id=?', (pid, rid)).fetchone()
        if not row:
            raise ValueError('Review not found in this project.')
        return dict(row)

    def prepare(self, pid, page_id=0, query_ids=None, start='', end='', source_ids=None, candidate_page=1):
        params = {k: [v] for k, v in [('start', start), ('end', end)] if v}
        _, start, end = self.app.strategy.window(params)
        with self.app.connect() as db:
            project, captures = self.app.strategy.evidence(db, pid, start, end)
            page = None
            if page_id:
                row = db.execute("SELECT * FROM crawl_pages WHERE project_id=? AND id=? AND kind='page'", (pid, page_id)).fetchone()
                if not row:
                    raise ValueError('Choose a website page from this project.')
                page = dict(row); page['result'] = json.loads(page['result'])
            if query_ids is None:
                query_ids = [r[0] for r in db.execute("SELECT query_id FROM query_plans WHERE target='existing' AND page_id=?", (page_id,))] if page else []
            if not isinstance(query_ids, list):
                raise ValueError('Select tracked queries for this review.')
            query_ids = list(dict.fromkeys(int(q) for q in query_ids))
            queries = []
            for qid in query_ids:
                q = self.app.strategy.query(db, pid, qid)
                plan = db.execute('SELECT * FROM query_plans WHERE query_id=?', (qid,)).fetchone()
                q['plan'] = dict(plan) if plan else {}
                if not page and q['plan'].get('target') != 'planned':
                    raise ValueError('A new-page brief requires a confirmed planned-page query.')
                latest = db.execute('SELECT id,status,reason,created FROM captures WHERE query_id=? ORDER BY id DESC LIMIT 1', (qid,)).fetchone()
                q['latest_check'] = dict(latest) if latest else None
                queries.append(q)
            matching = [c for c in captures if c['query_id'] in query_ids]
            chosen = matching
            sources = {}
            for c in chosen:
                for ref in c['refs']:
                    if self.app.strategy.owned(ref['href'], project['domain']):
                        continue
                    url = normalize(ref['href'])
                    row = db.execute('SELECT * FROM cited_pages WHERE project_id=? AND url=?', (pid, url)).fetchone()
                    key = str(row['id']) if row else url
                    if key not in sources:
                        sources[key] = dict(row) if row else dict(id=0, url=url, status='not retrieved', result='{}', retrieved='', reason='Use Retrieve selected pages to obtain comparison content.')
                        sources[key]['capture_ids'] = []; sources[key]['frequency'] = 0
                    s = sources[key]
                    if c['id'] not in s['capture_ids']:
                        s['capture_ids'].append(c['id']); s['frequency'] += 1
            candidates = sorted(sources.values(), key=lambda s: (-s['frequency'], -max(s['capture_ids'])))
            for s in candidates:
                s['result'] = json.loads(s['result'])
                s['key'] = str(s['id']) if s['id'] else s['url']
            if source_ids is not None:
                if not isinstance(source_ids, list) or len(source_ids) > 3:
                    raise ValueError('Choose up to three cited comparison pages.')
                if any(str(key) not in {s['key'] for s in candidates} for key in source_ids):
                    raise ValueError('A selected comparison URL is not cited in this query evidence window.')
                selected = [s for s in candidates if s['key'] in {str(k) for k in source_ids}]
            else:
                selected = candidates[:3]
            # Keep snapshots independent of later capture deletion and page refreshes.
            for c in chosen:
                c.pop('screenshot', None); c.pop('raw', None)
            candidate_page = max(1, min(int(candidate_page), max(1, (len(candidates)+99)//100)))
            snapshot = dict(project=project, page=page, queries=queries, captures=chosen,
                model=self.app.setting('ollama_model', ''),
                sources=selected, candidates=candidates[(candidate_page-1)*100:candidate_page*100],
                candidate_page=candidate_page, candidate_total=len(candidates),
                internal_links=[], start=start, end=end, saved=utc(), input_policy='adaptive_saved_content', review_policy='overview_grounded', evidence_policy='editorial_content_v1',
                limitations='All selected queries and matching usable Overview captures in the chosen window are included. All saved editorial sections are processed using one request or independent bounded passes. Older extractions require recrawling or retrieval before their content can be supplied.', matching_capture_count=len(matching))
            snapshot['readiness'] = readiness(snapshot)
            return snapshot

    def create(self, body):
        pid = int(body.get('project_id', 0))
        settings = self.settings()
        if not settings['model']:
            raise ValueError('Choose an installed local Ollama model in Settings first.')
        address(settings['address'])
        snap = self.prepare(pid, int(body.get('page_id', 0)), body.get('query_ids'), str(body.get('start', '')), str(body.get('end', '')), body.get('source_ids'))
        if not snap['queries']:
            raise ValueError('Select at least one tracked query for this page review.')
        ready = readiness(snap)
        if not ready['ready']: raise ValueError(' '.join(ready['errors']))
        title = snap['page']['title'] or snap['page']['url'] if snap['page'] else snap['queries'][0]['plan']['planned_title']
        snap.pop('candidates')
        snap['model_evidence'] = self.inputs(snap)
        facts = self.guided(snap)
        with self.app.lock, self.app.connect() as db:
            rid = db.execute('INSERT INTO page_reviews(project_id,page_id,created,title,status,model,address,snapshot) VALUES (?,?,?,?,?,?,?,?)',
                (pid, snap['page']['id'] if snap['page'] else None, utc(), title, 'queued', settings['model'], settings['address'], json.dumps(snap))).lastrowid
            self.insert_recommendations(db, rid, facts, 'guided')
        self.wake.set()
        return {'id': rid}

    @staticmethod
    def guided(snap):
        p = snap['page']
        if not p:
            return []
        result = p['result']; recs = []
        def add(category, title, reason, change, label='Observed fact', ids=None):
            recs.append(dict(category=category, label=label, title=title, rationale=reason, change=change, evidence_ids=ids or ['owned']))
        if result:
            directives = str(result.get('robots', ''))
            if re.search(r'\b(noindex|none|nosnippet)\b|max-snippet\s*:\s*0\b', directives.lower()) or result.get('googlebot_allowed_by_robots') is False:
                add('Technical', 'Verify Google access or snippet eligibility', 'Observed robots/snippet directives: ' + directives + '; robots Googlebot allowance: ' + str(result.get('googlebot_allowed_by_robots', 'unknown')), 'Confirm the scope and intent of these controls in Search Console and the rendered page. They may affect eligibility; this does not establish current indexing or predict citation uplift.')
            if result.get('partial_snippet_restriction') or any('data-nosnippet' in f for f in result.get('findings', [])):
                add('Technical', 'Some sections are excluded from snippets', 'data-nosnippet was observed on part of the page; this is not a whole-page exclusion.', 'Check whether the excluded section contains information relevant to the selected query. Keep intentional restrictions; verify eligibility separately.')
            if result.get('canonical') and result['canonical'] != (p.get('final_url') or p['url']):
                add('Technical', 'Canonical points to another URL', 'Observed canonical: ' + result['canonical'], 'Check whether the canonical destination is intentional. Only change it after confirmation, then re-crawl to verify.')
        return recs

    @staticmethod
    def insert_recommendations(db, rid, recs, origin):
        db.executemany('INSERT INTO recommendations(review_id,category,label,title,rationale,change,evidence_ids,origin,grounding) VALUES (?,?,?,?,?,?,?,?,?)',
            [(rid, r['category'], r['label'], r['title'], r['rationale'], r['change'], json.dumps(r['evidence_ids']), origin, json.dumps(r.get('evidence_quotes', {}))) for r in recs])

    @staticmethod
    def inputs(snap):
        records = []
        def page_record(page, key, retrieved, fields):
            r = page['result']; clean = r.get('review_content')
            record = dict(id=key, url=page['url'], status=page['status'])
            if retrieved: record['retrieved'] = retrieved
            if page.get('reason'): record['reason'] = page['reason']
            if clean is None:
                if r.get('content'):
                    record['content_unavailable'] = 'Recrawl or retrieve this page again: the saved extraction predates editorial filtering.'
            else:
                if clean.get('content') and (key == 'owned' or page['status'] == 'done'): record['content'] = clean['content']
                characteristics = {k:r[k] for k in fields if r.get(k) not in ('', None, [], {})}
                headings = [h for h in clean.get('headings', []) if h.get('text') and h['text'] in clean.get('content', '')]
                if headings: characteristics['headings'] = headings
                if clean.get('content_truncated'): characteristics['content_truncated'] = True
                if characteristics: record['characteristics'] = characteristics
            return record
        if snap['page']:
            p = snap['page']
            records.append(page_record(p, 'owned', p.get('successful'), ('title','canonical','robots','googlebot_allowed_by_robots','partial_snippet_restriction')))
        for s in snap['sources']:
            record = page_record(s, 'source:' + s['key'], s['retrieved'], ('title',))
            record['cited_in'] = s.get('capture_ids', [])
            records.append(record)
        for q in snap['queries']:
            records.append(dict(id='query:' + str(q['id']), query=q['text']))
        for c in snap['captures']:
            refs = [{k:v for k,v in ref.items() if k in ('href','title','index','domain') and v not in ('', None)} for ref in c['refs']]
            for ref in refs:
                if 'title' in ref:
                    ref['title'] = compact_overview(re.sub(r'(?i)\s*Opens in a new tab\.?', '', ref['title']))
                    if not ref['title']: del ref['title']
            overview = json.loads(c['overview']) if isinstance(c['overview'], str) else c['overview']
            records.append(dict(id='capture:' + str(c['id']), query_id=c['query_id'], date=c['created'], refs=refs, overview=compact_overview(overview)))
        return records

    @staticmethod
    def validate_output(value, records):
        if not isinstance(value, dict) or set(value) != {'assessment', 'recommendations'}:
            raise ValueError('The model did not return the required review structure. Retry manually or choose another local model.')
        if not isinstance(value['assessment'], str) or not value['assessment'].strip():
            raise ValueError('The model omitted its evidence assessment.')
        evidence = {r['id']: r for r in records}
        recs = value['recommendations']
        if not isinstance(recs, list):
            raise ValueError('The model returned an invalid recommendation list.')
        for r in recs:
            if not isinstance(r, dict) or set(r) != set(SCHEMA['properties']['recommendations']['items']['required']):
                raise ValueError('The model returned incomplete recommendation fields.')
            if r['category'] not in CATEGORIES[:-1] or r['label'] not in LABELS[1:]:
                raise ValueError('The model attempted to present an unverified fact or technical issue as confirmed.')
            for field in ('title', 'rationale', 'change'):
                if not isinstance(r[field], str) or not r[field].strip():
                    raise ValueError('The model returned an empty recommendation.')
                r[field] = r[field].strip()
            refs = r['evidence_ids']
            if not isinstance(refs, list) or not refs or any(not isinstance(x, str) or x not in evidence for x in refs):
                raise ValueError('The model referenced evidence outside this review. No AI recommendations were saved.')
            queries = {key for key in refs if key.startswith('query:')}
            captures = [evidence[key] for key in refs if key.startswith('capture:')]
            if not queries or not captures or any('query:' + str(c['query_id']) not in queries for c in captures):
                raise ValueError('Each recommendation must identify its selected query and a matching captured AI Overview.')
            if 'owned' in evidence and 'owned' not in refs:
                raise ValueError('An existing-page recommendation must reference the reviewed page.')
            quotes = r['evidence_quotes']
            expected = {key for key in refs if not key.startswith('query:')}
            if not isinstance(quotes, dict) or set(quotes) != expected:
                raise ValueError('Each referenced page or Overview must include a supporting quotation.')
            for key, quote in quotes.items():
                if key.startswith('source:') and not any(c['id']=='capture:'+str(cid) for c in captures for cid in evidence[key].get('cited_in', [])):
                    raise ValueError('A comparison page must be cited in the matching captured Overview.')
                if not isinstance(quote, str) or not quote.strip() or ' '.join(quote.split()) not in ' '.join(evidence_text(evidence[key]).split()):
                    raise ValueError('A supporting quotation was not found in the supplied content. No AI recommendations were saved.')
        return recs

    def process_one(self):
        if self.finish_refresh(): return True
        with self.app.lock, self.app.connect() as db:
            row = db.execute("SELECT * FROM page_reviews WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return False
            row = dict(row)
            db.execute("UPDATE page_reviews SET status='running',error='' WHERE id=?", (row['id'],))
        try:
            version = self.request('/api/version', endpoint_address=row['address']).get('version', '')
            numbers = re.match(r'^(\d+)\.(\d+)\.(\d+)', str(version))
            if not numbers or tuple(map(int, numbers.groups())) < (0, 31, 2):
                raise ValueError('Full-input review requires Ollama 0.31.2 or later, whose no-truncation/no-context-shifting controls have been verified. Update Ollama before retrying; this review was not sent for generation.')
            info = self.request('/api/show', {'model': row['model']}, row['address'])
            installed = self.request('/api/tags', endpoint_address=row['address']).get('models', [])
            info['_digest'] = next((m.get('digest') for m in installed if m.get('name') == row['model']), None)
            if not self.local_model({**info, 'name': row['model']}) or 'completion' not in info.get('capabilities', []):
                raise ValueError('The saved model is not an installed local text model. Choose a local model in Settings.')
            snap = json.loads(row['snapshot']); records = self.inputs(snap)
            # Queued reviews from older app versions must use the same dispatch filter.
            snap['model_evidence'] = records
            snap['evidence_policy'] = 'editorial_content_v1'
            with self.app.connect() as db:
                db.execute('UPDATE page_reviews SET snapshot=? WHERE id=?', (json.dumps(snap), row['id']))
            pipeline = Pipeline(self, row, snap, info)
            output = pipeline.run(records)
            recs = self.validate_output(output, records)
            with self.app.lock, self.app.connect() as db:
                current = db.execute('SELECT status FROM page_reviews WHERE id=?', (row['id'],)).fetchone()
                if not current or current['status'] != 'running':
                    return True
                self.insert_recommendations(db, row['id'], recs, 'Ollama')
                db.execute("UPDATE page_reviews SET status='completed',assessment=?,finished=? WHERE id=?", (output['assessment'].strip(), utc(), row['id']))
        except (ValueError, TypeError, KeyError, OSError) as error:
            with self.app.connect() as db:
                db.execute("UPDATE page_reviews SET status='failed',error=?,finished=? WHERE id=? AND status='running'", (str(error), utc(), row['id']))
        return True

    def refresh_review(self, pid, rid):
        with self.app.lock, self.app.connect() as db:
            original = self.review(db, pid, rid)
            if original['status'] not in ('failed', 'cancelled'):
                raise ValueError('Refresh a failed or cancelled review, or prepare a new review.')
            interrupted = json.loads(original['refresh'])
            if interrupted:
                run = db.execute('SELECT * FROM crawl_retries WHERE id=? AND project_id=?',
                    (interrupted.get('crawl_run', 0), pid)).fetchone()
                if run and run['status'] == 'running':
                    db.execute("UPDATE crawl_jobs SET status='running',message='' WHERE project_id=?", (pid,))
                db.execute('INSERT INTO source_jobs(project_id,paused) VALUES (?,0) ON CONFLICT(project_id) DO UPDATE SET paused=0', (pid,))
                db.execute("UPDATE page_reviews SET status='refreshing',error='',finished='' WHERE id=?", (rid,))
                self.wake.set()
                return {'id': rid}
            existing = db.execute("SELECT id FROM page_reviews WHERE predecessor=? AND project_id=? AND status IN ('refreshing','queued','running')", (str(rid), pid)).fetchone()
            if existing: return {'id': existing[0]}
            snap = json.loads(original['snapshot'])
            refresh = dict(page_id=snap['page']['id'] if snap['page'] else 0, source_urls=[s['url'] for s in snap['sources']])
            if snap['page']:
                if db.execute("SELECT 1 FROM crawl_jobs WHERE project_id=? AND status IN ('running','paused')", (pid,)).fetchone() or db.execute("SELECT 1 FROM crawl_retries WHERE project_id=? AND status='running'", (pid,)).fetchone():
                    raise ValueError('Finish or resume the existing crawl before refreshing this page.')
                page = db.execute('SELECT id FROM crawl_pages WHERE id=? AND project_id=?', (refresh['page_id'], pid)).fetchone()
                if not page: raise ValueError('The reviewed page was removed. Prepare a new review.')
                run = db.execute('INSERT INTO crawl_retries(project_id,started,targeted) VALUES (?,?,1)', (pid, utc())).lastrowid
                db.execute('INSERT INTO crawl_retry_pages(run_id,page_id) VALUES (?,?)', (run, refresh['page_id']))
                db.execute("UPDATE crawl_pages SET status='queued',reason='' WHERE id=?", (refresh['page_id'],))
                db.execute("INSERT INTO crawl_jobs(project_id,status,started) VALUES (?,'running',?) ON CONFLICT(project_id) DO UPDATE SET status='running',started=excluded.started,message=''", (pid, utc()))
                refresh['crawl_run'] = run
            source_ids = []
            for source in snap['sources']:
                db.execute('INSERT OR IGNORE INTO cited_pages(project_id,url) VALUES (?,?)', (pid, source['url']))
                current = db.execute('SELECT * FROM cited_pages WHERE project_id=? AND url=?', (pid, source['url'])).fetchone()
                source_ids.append(current['id'])
                content = json.loads(current['result']).get('review_content', {}).get('content')
                if current['status'] != 'done' or not content:
                    db.execute("UPDATE cited_pages SET status='queued',reason='' WHERE id=? AND status<>'running'", (current['id'],))
            refresh['source_ids'] = source_ids
            db.execute('INSERT INTO source_jobs(project_id,paused) VALUES (?,0) ON CONFLICT(project_id) DO UPDATE SET paused=0', (pid,))
            replacement = db.execute('INSERT INTO page_reviews(project_id,page_id,created,title,status,model,address,snapshot,predecessor,refresh) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (pid, refresh['page_id'] or None, utc(), original['title'], 'refreshing', self.app.setting('ollama_model', original['model']),
                 self.app.setting('ollama_address', original['address']), original['snapshot'], str(rid), json.dumps(refresh))).lastrowid
        self.wake.set()
        return {'id': replacement}

    def finish_refresh(self):
        with self.app.lock, self.app.connect() as db:
            rows = db.execute("SELECT * FROM page_reviews WHERE status='refreshing' ORDER BY id").fetchall()
            for row in rows:
                refresh = json.loads(row['refresh']); old = json.loads(row['snapshot'])
                if refresh.get('crawl_run'):
                    run = db.execute('SELECT status FROM crawl_retries WHERE id=?', (refresh['crawl_run'],)).fetchone()
                    if run and run['status'] == 'running': continue
                sources = [db.execute('SELECT status FROM cited_pages WHERE id=?', (sid,)).fetchone() for sid in refresh['source_ids']]
                if any(s and s['status'] in ('queued', 'running') for s in sources): continue
                try:
                    snap = self.prepare(row['project_id'], refresh['page_id'], [q['id'] for q in old['queries']], old['start'], old['end'], [str(sid) for sid in refresh['source_ids']])
                    ready = readiness(snap)
                    if snap['page'] and snap['page']['status'] != 'done':
                        raise ValueError('Owned page refresh failed: ' + (snap['page'].get('reason') or snap['page']['status']))
                    if not ready['ready']: raise ValueError(' '.join(ready['errors']))
                    snap.pop('candidates'); snap['model_evidence'] = self.inputs(snap)
                    self.insert_recommendations(db, row['id'], self.guided(snap), 'guided')
                    db.execute("UPDATE page_reviews SET snapshot=?,status='queued',refresh='{}' WHERE id=? AND status='refreshing'", (json.dumps(snap), row['id']))
                except (ValueError, TypeError, KeyError) as error:
                    db.execute("UPDATE page_reviews SET status='failed',error=?,finished=? WHERE id=? AND status='refreshing'", (str(error), utc(), row['id']))
                return True
        return False

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        def work():
            while not self.app.stop_event.is_set():
                try:
                    if self.process_one():
                        continue
                except Exception:
                    # A worker failure must not leave a successful-looking or stuck review.
                    with self.app.connect() as db:
                        db.execute("UPDATE page_reviews SET status='failed',error='Local review processing failed. Your evidence is preserved; retry manually.',finished=? WHERE status='running'", (utc(),))
                self.wake.wait(1); self.wake.clear()
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def listing(self, params):
        get = lambda k, default='': str(params.get(k, [default])[0])
        pid = int(get('project_id', '0')); rid = int(get('id', '0'))
        if get('kind') == 'prepare':
            ids = [int(x) for x in get('query_ids').split(',') if x] if 'query_ids' in params else None
            return self.prepare(pid, int(get('page_id', '0')), ids, get('start'), get('end'), candidate_page=int(get('candidate_page','1')))
        with self.app.connect() as db:
            self.app.strategy.project(db, pid)
            if rid:
                r = self.review(db, pid, rid); r['snapshot'] = json.loads(r['snapshot'])
                r['execution'] = json.loads(r['execution'])
                r['readiness'] = readiness(r['snapshot'])
                r['replacements'] = [x[0] for x in db.execute('SELECT id FROM page_reviews WHERE predecessor=? AND project_id=? ORDER BY id', (str(rid), pid))]
                r['recommendations'] = [dict(x, evidence_ids=json.loads(x['evidence_ids'])) for x in db.execute('SELECT * FROM recommendations WHERE review_id=? ORDER BY id', (rid,))]
                for rec in r['recommendations']:
                    rec['grounding'] = json.loads(rec['grounding'])
                r['versions'] = [dict(x) for x in db.execute('SELECT id,created FROM brief_versions WHERE review_id=? ORDER BY id DESC LIMIT 100', (rid,))]
                if get('version'):
                    v = db.execute('SELECT content FROM brief_versions WHERE review_id=? AND id=?', (rid, int(get('version')))).fetchone()
                    if not v: raise ValueError('Brief version not found.')
                    r['version_content'] = v[0]
                return r
            page = max(1, int(get('page', '1')))
            table = 'improvement_tasks' if get('kind') == 'tasks' else 'page_reviews'
            where = 'project_id=?'; args = [pid]
            if get('kind') == 'briefs':
                where += " AND brief<>''"
            if get('search'):
                where += ' AND (instr(lower(title),lower(?))>0)'; args.append(get('search'))
            if get('status'):
                where += ' AND status=?'; args.append(get('status'))
            total = db.execute('SELECT count(*) FROM ' + table + ' WHERE ' + where, args).fetchone()[0]
            fields = '*' if table == 'improvement_tasks' else '''id,title,status,error,created,finished,model,page_id,
                (SELECT count(*) FROM recommendations WHERE review_id=page_reviews.id AND origin<>'invalidated' AND decision IN ('Accepted','Rejected','Corrected')) AS decided,
                (SELECT count(*) FROM recommendations WHERE review_id=page_reviews.id AND origin<>'invalidated') AS assignments,
                (SELECT count(*) FROM brief_versions WHERE review_id=page_reviews.id) AS brief_versions,
                (SELECT max(created) FROM brief_versions WHERE review_id=page_reviews.id) AS brief_updated,
                (SELECT count(*) FROM improvement_tasks WHERE review_id=page_reviews.id) AS task_count'''
            rows = [dict(r) for r in db.execute('SELECT ' + fields + ' FROM ' + table + ' WHERE ' + where + ' ORDER BY id DESC LIMIT 100 OFFSET ?', [*args, (page-1)*100])]
            if table == 'improvement_tasks':
                for task in rows:
                    task['outcome'] = self.outcome(db, task) if task['deployed'] else None
            return dict(rows=rows, total=total, page=page)

    def outcome(self, db, task):
        deployed = dt.date.fromisoformat(task['deployed'])
        days = min(30, max(0, (dt.datetime.now(dt.timezone.utc).date() - deployed).days - 1))
        snap = json.loads(self.review(db, task['project_id'], task['review_id'])['snapshot'])
        result = dict(days=days, deployed=task['deployed'], queries=[], limitation='Sampled citation observations only; a change in citation rate does not establish that this assignment caused it. Deployment day and the current incomplete UTC day are excluded.')
        if not days:
            return result
        page = snap['page']
        urls = {normalize(u) for u in ([page['url'], page.get('final_url'), page['result'].get('canonical')] if page else []) if u}
        context = snap['project']
        rec = db.execute('SELECT evidence_ids FROM recommendations WHERE id=? AND review_id=?', (task['recommendation_id'], task['review_id'])).fetchone()
        query_ids = {key for key in json.loads(rec['evidence_ids']) if key.startswith('query:')}
        for query in snap['queries']:
            if query_ids and 'query:'+str(query['id']) not in query_ids:
                continue
            item = dict(query=query['text'], target='Reviewed page' if page else 'Project domain (planned page)', before={}, after={})
            for label, start, end in [('before', deployed-dt.timedelta(days=days), deployed-dt.timedelta(days=1)), ('after', deployed+dt.timedelta(days=1), deployed+dt.timedelta(days=days))]:
                rows = db.execute("SELECT context,status,refs FROM captures WHERE query_id=? AND status IN ('found','absent') AND substr(created,1,10) BETWEEN ? AND ?", (query['id'], start.isoformat(), end.isoformat())).fetchall()
                usable = [r for r in rows if all(json.loads(r['context']).get(k)==context[k] for k in ('domain','country','language','device')) and json.loads(r['context']).get('query')==query['text']]
                cited = sum(any(normalize(r['href']) in urls if page else self.app.strategy.owned(r['href'],context['domain']) for r in json.loads(c['refs'])) for c in usable)
                item[label] = dict(start=start.isoformat(), end=end.isoformat(), usable=len(usable), overviews=sum(c['status']=='found' for c in usable), cited=cited)
            result['queries'].append(item)
        return result

    def mutate(self, body):
        action = body.get('action', '')
        if action == 'create':
            return self.create(body)
        pid = int(body.get('project_id', 0))
        if action == 'refresh-review':
            return self.refresh_review(pid, int(body.get('id', 0)))
        if action == 'retrieve':
            snap = self.prepare(pid, int(body.get('page_id', 0)), body.get('query_ids'), str(body.get('start', '')), str(body.get('end', '')), body.get('source_ids', []))
            with self.app.lock, self.app.connect() as db:
                for s in snap['sources']:
                    url = normalize(s['url'])
                    db.execute('INSERT OR IGNORE INTO cited_pages(project_id,url) VALUES (?,?)', (pid, url))
                    sid = db.execute('SELECT id FROM cited_pages WHERE project_id=? AND url=?', (pid, url)).fetchone()[0]
                    db.executemany('INSERT OR IGNORE INTO cited_evidence VALUES (?,?)', [(sid, cid) for cid in s['capture_ids']])
                    if s['status'] != 'done' or not s['result'].get('review_content', {}).get('content'):
                        db.execute("UPDATE cited_pages SET status='queued',reason='' WHERE id=? AND status<>'running'", (sid,))
                db.execute("INSERT INTO source_jobs(project_id,paused) VALUES (?,0) ON CONFLICT(project_id) DO UPDATE SET paused=0", (pid,))
            return {'ok': True}
        rid = int(body.get('id', 0))
        with self.app.lock, self.app.connect() as db:
            if action == 'task-update':
                task = db.execute('SELECT * FROM improvement_tasks WHERE project_id=? AND id=?', (pid, rid)).fetchone()
                if not task: raise ValueError('Task not found in this project.')
                owner = bounded(body.get('owner', ''), 150); due = bounded(body.get('due', ''), 10)
                if due:
                    dt.date.fromisoformat(due)
                status = body.get('status', 'To do')
                if status not in TASK_STATUS: raise ValueError('Choose a valid task status.')
                deployed = bounded(body.get('deployed', task['deployed']), 10)
                note = bounded(body.get('deployment_note', task['deployment_note']), 4000)
                if deployed and dt.date.fromisoformat(deployed) > dt.datetime.now(dt.timezone.utc).date():
                    raise ValueError('Deployment date cannot be in the future.')
                if deployed and not note:
                    raise ValueError('Record what actually changed when entering a deployment date.')
                db.execute('UPDATE improvement_tasks SET owner=?,status=?,due=?,deployed=?,deployment_note=? WHERE id=?', (owner, status, due, deployed, note, rid))
                return {'ok': True}
            r = self.review(db, pid, rid)
            if action == 'cancel':
                db.execute("UPDATE page_reviews SET status='cancelled',error='Cancelled. Local output will be discarded; in-flight retrieval or Ollama computation may finish.',finished=? WHERE id=? AND status IN ('queued','running','refreshing')", (utc(), rid))
            elif action in ('retry', 'resume'):
                if r['status'] not in ('failed', 'cancelled'): raise ValueError('Only failed or cancelled reviews can be retried.')
                model = self.app.setting('ollama_model', '')
                if not model: raise ValueError('Choose a model in Settings first.')
                snapshot = json.loads(r['snapshot'])
                ready = readiness(snapshot)
                if not ready['ready']: raise ValueError(' '.join(ready['errors']))
                db.execute("UPDATE page_reviews SET status='queued',error='',assessment='',finished='',execution='{}',model=?,address=? WHERE id=?", (model, self.app.setting('ollama_address', 'http://127.0.0.1:11434'), rid))
                self.wake.set()
            elif action == 'decision':
                rec = db.execute('SELECT * FROM recommendations WHERE id=? AND review_id=?', (int(body.get('recommendation_id', 0)), rid)).fetchone()
                if not rec: raise ValueError('Recommendation not found in this review.')
                if rec['origin'] == 'invalidated': raise ValueError('This assignment failed validation. Use the corrected replacement review.')
                decision = body.get('decision')
                if decision not in ('Accepted', 'Rejected', 'Corrected', 'Pending'): raise ValueError('Choose a valid decision.')
                correction = bounded(body.get('correction', ''), None)
                if decision == 'Corrected' and not correction: raise ValueError('Enter your corrected assignment.')
                db.execute('UPDATE recommendations SET decision=?,correction=? WHERE id=?', (decision, correction, rec['id']))
            elif action in ('draft', 'brief-save'):
                if action == 'draft':
                    if r['status'] != 'completed': raise ValueError('Complete the AI review before creating a brief. Guided evidence remains available separately.')
                    text = self.brief(db, r)
                else:
                    text = bounded(body.get('content', ''), None)
                    if not text: raise ValueError('Enter brief content before saving.')
                db.execute('INSERT INTO brief_versions(review_id,created,content) VALUES (?,?,?)', (rid, utc(), text))
                db.execute('UPDATE page_reviews SET brief=? WHERE id=?', (text, rid))
            elif action == 'task-create':
                rec = db.execute("SELECT * FROM recommendations WHERE id=? AND review_id=? AND origin<>'invalidated' AND decision IN ('Accepted','Corrected')", (int(body.get('recommendation_id', 0)), rid)).fetchone()
                if not rec: raise ValueError('Accept or correct this recommendation before creating a task.')
                snap = json.loads(r['snapshot']); p = snap['page']
                technical = rec['category'] == 'Technical' and rec['origin'] == 'guided' and rec['label'] == 'Observed fact'
                details = ('Observed issue: ' if technical else 'Assignment: ') + rec['rationale'] + '\n\n' + (rec['correction'] or rec['change'])
                details += '\n\nTarget: ' + (p['url'] if p else r['title']) + '\nReview #' + str(rid) + '\nEvidence: ' + rec['evidence_ids'] + '\nSnapshot: ' + snap['saved']
                if technical:
                    details += '\nObservation date: ' + (p.get('successful') or p.get('crawled') or 'Unknown') + '\nVerification: inspect the original HTML/directives, confirm intended behaviour, apply the confirmed change and re-crawl. Do not infer indexing or ranking outcomes.'
                db.execute('INSERT INTO improvement_tasks(project_id,review_id,recommendation_id,kind,title,details,created) VALUES (?,?,?,?,?,?,?)', (pid, rid, rec['id'], 'Technical' if technical else 'Writing', rec['title'], details, utc()))
            else:
                raise ValueError('Unknown page-improvement action.')
        self.app.last_saved = utc()
        return {'ok': True}

    def brief(self, db, review):
        snap = json.loads(review['snapshot']); page = snap['page']
        purpose = '\n'.join(q['plan'].get('notes', '') for q in snap['queries'] if q['plan'].get('notes'))
        lines = ['# ' + review['title'], '', '## Your target page', page['url'] if page else 'Planned page — no existing owned page was reviewed.',
                 '', '## Query intent and user notes', purpose or '[Describe what the selected query needs answered.]', '', '## Queries']
        lines += ['- ' + q['text'] for q in snap['queries']]
        lines += ['', '## Evidence', 'Snapshot: ' + snap['saved'], 'Window: ' + snap['start'] + ' to ' + snap['end'], 'Local model: ' + review['model']]
        if page: lines += ['Owned content retrieved: ' + (page.get('successful') or 'Unknown')]
        lines += ['- Capture #' + str(c['id']) + ' (' + c['created'] + ')' for c in snap['captures']]
        lines += ['- ' + s['url'] + ' — ' + s['status'] + ', retrieved ' + (s['retrieved'] or 'not retrieved') for s in snap['sources']]
        lines += ['', '## Proposed assignments']
        recs = db.execute("SELECT * FROM recommendations WHERE review_id=? AND decision IN ('Accepted','Corrected') ORDER BY id", (review['id'],)).fetchall()
        for rec in recs:
            lines += ['', '### ' + rec['category'] + ': ' + rec['title'], rec['label'] + ' · ' + rec['decision'], rec['rationale'], rec['correction'] or rec['change'], 'Evidence: ' + rec['evidence_ids']]
        if not recs: lines += ['No recommendations accepted yet. Review and accept assignments before finalising.']
        structure = [r['correction'] or r['change'] for r in recs if r['category'] in ('Question coverage','Answer clarity')]
        originals = [r['correction'] or r['change'] for r in recs if r['category']=='Original evidence']
        if structure: lines += ['', '## Proposed structure', '\n'.join('- ' + x for x in structure)]
        if originals: lines += ['', '## Original evidence to obtain', '\n'.join('- ' + x for x in originals)]
        lines += ['', '## Completion checks', '- Answer the selected reader questions accurately.', '- Verify all factual claims and source links.',
            '- Explain each change using the captured Overview and reviewed-page passages.', '- Record deployment in the task and collect comparable checks manually; citation changes do not prove causation.', '- Verify technical changes separately; no ranking guarantee.', '',
            '## Limitations', ('Saved editorial sections processed with bounded independent passes; menus, forms and interface boilerplate excluded. Original snapshots and stage quotations preserved.' if snap.get('evidence_policy')=='editorial_content_v1' else 'All available saved page content supplied together; no excerpts or splitting.' if snap.get('input_policy')=='full_saved_content' else 'Legacy review used bounded excerpts; create a new review for full saved content.') + ' HTML only, no JavaScript. Saved crawler content can be incomplete or truncated; see the snapshot. Blocked and missing content cannot establish gaps. Suggestions and hypotheses require human review. Capture references may outlive deleted capture records; their original evidence remains in this snapshot.']
        return '\n'.join(lines)
