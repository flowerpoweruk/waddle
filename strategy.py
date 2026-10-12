"""Project planning and evidence-led comparisons; no paid provider requests."""
import csv
import datetime as dt
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from crawler import normalize, Robots, PageParser, utc, AGENT, decode

IMPORTANCE = ('High', 'Medium', 'Low', 'Unspecified')
TYPES = ('Guide', 'Comparison', 'Product/Service', 'Case Study', 'Research', 'Discussion', 'Other', 'Unknown')


def query_key(value):
    return ' '.join(value.split()).casefold()


def query_terms(value):
    common = {'the','and','for','with','from','that','this','what','which','where','when','why','how','are','was','were','your','you','our','can','does','into','about','have','has'}
    return list(dict.fromkeys(w for w in re.findall(r'\w{3,}', value.casefold()) if w not in common))


class Strategy:
    def __init__(self, app, domain, owned):
        self.app, self.domain, self.owned = app, domain, owned
        self.robots = {}
        with app.connect() as db:
            fresh = not db.execute("SELECT 1 FROM sqlite_master WHERE name='query_plans'").fetchone()
            db.executescript('''
                BEGIN;
                CREATE TABLE IF NOT EXISTS query_plans(query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
                    importance TEXT NOT NULL DEFAULT 'Unspecified',notes TEXT NOT NULL DEFAULT '',target TEXT NOT NULL DEFAULT 'unreviewed',
                    page_id INTEGER REFERENCES crawl_pages(id) ON DELETE SET NULL,planned_title TEXT NOT NULL DEFAULT '',planned_url TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS topic_groups(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,name TEXT NOT NULL,UNIQUE(project_id,name));
                CREATE TABLE IF NOT EXISTS query_groups(query_id INTEGER REFERENCES queries(id) ON DELETE CASCADE,group_id INTEGER REFERENCES topic_groups(id) ON DELETE CASCADE,PRIMARY KEY(query_id,group_id));
                CREATE TABLE IF NOT EXISTS competitors(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,domain TEXT NOT NULL,UNIQUE(project_id,domain));
                CREATE TABLE IF NOT EXISTS opportunities(query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,notes TEXT NOT NULL,effort TEXT NOT NULL,capture_ids TEXT NOT NULL,saved TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS saved_views(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,name TEXT NOT NULL,filters TEXT NOT NULL,UNIQUE(project_id,name));
                CREATE TABLE IF NOT EXISTS cited_pages(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',final_url TEXT NOT NULL DEFAULT '',http_status INTEGER,retrieved TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',result TEXT NOT NULL DEFAULT '{}',suggested TEXT NOT NULL DEFAULT 'Unknown',confirmed TEXT NOT NULL DEFAULT '',UNIQUE(project_id,url));
                CREATE INDEX IF NOT EXISTS cited_queue ON cited_pages(status,project_id,id);
                CREATE TABLE IF NOT EXISTS cited_evidence(page_id INTEGER REFERENCES cited_pages(id) ON DELETE CASCADE,capture_id INTEGER REFERENCES captures(id) ON DELETE CASCADE,PRIMARY KEY(page_id,capture_id));
                CREATE TABLE IF NOT EXISTS source_jobs(project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,paused INTEGER NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS publisher_notes(project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,domain TEXT NOT NULL,shortlisted INTEGER NOT NULL DEFAULT 0,classification TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',PRIMARY KEY(project_id,domain));
                CREATE VIRTUAL TABLE IF NOT EXISTS page_search USING fts5(url,title,headings,content);
                CREATE TRIGGER IF NOT EXISTS page_search_insert AFTER INSERT ON crawl_pages BEGIN
                    INSERT INTO page_search(rowid,url,title,headings,content) VALUES(new.id,new.url,new.title,json_extract(new.result,'$.headings'),json_extract(new.result,'$.content')); END;
                CREATE TRIGGER IF NOT EXISTS page_search_update AFTER UPDATE ON crawl_pages BEGIN
                    DELETE FROM page_search WHERE rowid=old.id;
                    INSERT INTO page_search(rowid,url,title,headings,content) VALUES(new.id,new.url,new.title,json_extract(new.result,'$.headings'),json_extract(new.result,'$.content')); END;
                CREATE TRIGGER IF NOT EXISTS page_search_delete AFTER DELETE ON crawl_pages BEGIN DELETE FROM page_search WHERE rowid=old.id; END;
            ''')
            if fresh:
                db.execute('DELETE FROM page_search')
                db.execute("INSERT INTO page_search(rowid,url,title,headings,content) SELECT id,url,title,json_extract(result,'$.headings'),json_extract(result,'$.content') FROM crawl_pages")
            db.execute("UPDATE cited_pages SET status='queued',reason='Interrupted retrieval; ready to resume.' WHERE status='running'")
            db.execute("UPDATE source_jobs SET paused=1,message='Interrupted queue recovered. Resume when ready.' WHERE project_id IN (SELECT project_id FROM cited_pages WHERE status='queued')")

    def project(self, db, pid):
        row = db.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
        if not row:
            raise ValueError('Choose a project first.')
        return dict(row)

    def query(self, db, pid, qid):
        row = db.execute('SELECT * FROM queries WHERE project_id=? AND id=?', (pid, qid)).fetchone()
        if not row:
            raise ValueError('Query not found in this project.')
        return dict(row)

    def window(self, params):
        get = lambda key, default='': str(params.get(key, [default])[0])
        today = dt.datetime.now(dt.timezone.utc).date()
        start = get('start', (today - dt.timedelta(days=29)).isoformat())
        end = get('end', today.isoformat())
        dt.date.fromisoformat(start); dt.date.fromisoformat(end)
        if start > end:
            raise ValueError('Start date must be before end date.')
        return get, start, end

    def evidence(self, db, pid, start, end):
        p = self.project(db, pid)
        captures = []
        for row in db.execute("SELECT c.* FROM captures c JOIN queries q ON q.id=c.query_id WHERE q.project_id=? AND c.status='found' AND substr(c.created,1,10) BETWEEN ? AND ? ORDER BY c.id", (pid, start, end)):
            c = dict(row); context = json.loads(c['context'])
            if any(context.get(k) != p[k] for k in ('domain', 'country', 'language', 'device')):
                continue
            c['refs'] = json.loads(c['refs'])
            captures.append(c)
        return p, captures

    def groups(self, db, pid, qid=None):
        if qid is None:
            return [dict(r) for r in db.execute('SELECT * FROM topic_groups WHERE project_id=? ORDER BY name', (pid,))]
        return [dict(r) for r in db.execute('SELECT g.* FROM topic_groups g JOIN query_groups qg ON qg.group_id=g.id WHERE g.project_id=? AND qg.query_id=? ORDER BY g.name', (pid, qid))]

    def opportunity_rows(self, db, pid, captures, selected=''):
        competitors = [r['domain'] for r in db.execute('SELECT domain FROM competitors WHERE project_id=? ORDER BY domain', (pid,))]
        if selected:
            competitors = [d for d in competitors if d in selected.split(',')]
        rows = []
        for q in db.execute('SELECT * FROM queries WHERE project_id=? ORDER BY text', (pid,)):
            q = dict(q); qid = q['id']
            plan = db.execute('SELECT * FROM query_plans WHERE query_id=?', (qid,)).fetchone()
            q.update(dict(plan) if plan else dict(importance='Unspecified', notes='', target='unreviewed', page_id=None, planned_title='', planned_url=''))
            page = db.execute('SELECT id,url,title,status,final_url,result FROM crawl_pages WHERE id=? AND project_id=?', (q['page_id'], pid)).fetchone()
            q['page'] = dict(page) if page else None
            if page: q['page'].pop('result')
            q['groups'] = self.groups(db, pid, qid)
            checks = [c for c in captures if c['query_id'] == qid]
            gaps = [c for c in checks if not any(r.get('owned') for r in c['refs']) and any(self.owned(r['href'], d) for r in c['refs'] for d in competitors)]
            q['checks'], q['gap_checks'], q['recurring'] = len(checks), len(gaps), len(gaps) >= 2
            uncited = [c for c in checks if not any(r.get('owned') for r in c['refs'])]
            q['uncited_checks'] = len(uncited)
            q['uncited_capture_ids'] = [c['id'] for c in uncited]
            q['capture_ids'] = [c['id'] for c in gaps]
            q['competitors'] = sorted({d for c in gaps for r in c['refs'] for d in competitors if self.owned(r['href'], d)})
            latest = db.execute('SELECT id,created,status,reason FROM captures WHERE query_id=? ORDER BY id DESC LIMIT 1', (qid,)).fetchone()
            q['latest_check'] = dict(latest) if latest else None
            q['latest_evidence'] = {'id': checks[-1]['id'], 'created': checks[-1]['created']} if checks else None
            q['older_evidence'] = None
            if not checks:
                p = self.project(db, pid)
                for old in db.execute("SELECT id,created,context FROM captures WHERE query_id=? AND status='found' ORDER BY id DESC", (qid,)):
                    context = json.loads(old['context'])
                    if all(context.get(k)==p[k] for k in ('domain','country','language','device')):
                        q['older_evidence'] = dict(id=old['id'],created=old['created'])
                        break
            q['owned_status'] = 'Unknown — no usable Overview in this window'
            if checks:
                refs = [r for r in checks[-1]['refs'] if r.get('owned')]
                q['owned_status'] = 'No owned URL cited' if not refs else ('Owned URL cited; mapping unreviewed' if q['target']=='unreviewed' else 'Owned URL cited; no existing target selected')
                if page:
                    equivalents = {page['url'], page['final_url']}
                    canonical = json.loads(page['result']).get('canonical')
                    if canonical: equivalents.add(canonical)
                    match = any(r['href'] in equivalents for r in refs)
                    q['owned_status'] = 'Mapped URL cited' if match else 'Different owned URL cited' if refs else 'Mapped page not cited'
                    q['equivalence_note'] = 'Matches include only recorded final/canonical URLs; original citation URLs are retained.'
            saved = db.execute('SELECT * FROM opportunities WHERE query_id=?', (qid,)).fetchone()
            q['saved'] = dict(saved) if saved else None
            if saved: q['saved']['capture_ids'] = json.loads(saved['capture_ids'])
            rows.append(q)
        return rows

    def listing(self, params):
        get, start, end = self.window(params)
        pid, page, kind = int(get('project_id', '0')), max(1, int(get('page', '1'))), get('kind', 'opportunities')
        with self.app.connect() as db:
            p, captures = self.evidence(db, pid, start, end)
            competitors = [dict(r) for r in db.execute('SELECT * FROM competitors WHERE project_id=? ORDER BY domain', (pid,))]
            base = dict(start=start, end=end, page=page, groups=self.groups(db, pid), competitors=competitors,
                        views=[dict(r) for r in db.execute('SELECT * FROM saved_views WHERE project_id=? ORDER BY name', (pid,))])
            if kind == 'competitors':
                domains = [p['domain']] + [r['domain'] for r in competitors if not get('competitor') or r['domain'] in get('competitor').split(',')]
                rows = []
                for d in domains:
                    cited = [c for c in captures if any(self.owned(r['href'], d) for r in c['refs'])]
                    rows.append(dict(domain=d, owned=d == p['domain'], numerator=len(cited), denominator=len(captures), query_count=len({c['query_id'] for c in captures}), cited_query_count=len({c['query_id'] for c in cited}), rate=round(len(cited)/len(captures)*100, 1) if captures else None, capture_ids=[c['id'] for c in cited], first=cited[0]['created'] if cited else '', last=cited[-1]['created'] if cited else ''))
            elif kind == 'publishers':
                domains = {}
                for c in captures:
                    for d in {r['domain'] for r in c['refs'] if not r.get('owned') and not any(self.owned(r['href'], x['domain']) for x in competitors)}:
                        domains.setdefault(d, []).append(c)
                rows = []
                for d, checks in domains.items():
                    if len(checks) < 2: continue
                    note = db.execute('SELECT * FROM publisher_notes WHERE project_id=? AND domain=?', (pid, d)).fetchone()
                    item=dict(domain=d, count=len(checks), query_count=len({c['query_id'] for c in checks}), capture_ids=[c['id'] for c in checks], shortlisted=0, classification='', notes='')
                    if note: item.update({k:note[k] for k in ('shortlisted','classification','notes')})
                    rows.append(item)
                rows.sort(key=lambda r: (-r['count'], r['domain']))
            elif kind == 'sources':
                if get('id'):
                    row = db.execute('SELECT * FROM cited_pages WHERE project_id=? AND id=?', (pid, int(get('id')))).fetchone()
                    if not row:
                        raise ValueError('Cited page not found in this project.')
                    row = dict(row)
                    row['result'] = json.loads(row['result'])
                    return row
                clauses, args = ['project_id=?'], [pid]
                if get('query_id'):
                    qid = int(get('query_id')); self.query(db, pid, qid)
                    clauses.append('id IN (SELECT e.page_id FROM cited_evidence e JOIN captures c ON c.id=e.capture_id WHERE c.query_id=?)')
                    args.append(qid)
                if get('search'): clauses.append('(instr(lower(url),?)>0 OR instr(lower(result),?)>0)'); args += [get('search').lower()]*2
                if get('status'): clauses.append('status=?'); args.append(get('status'))
                where = ' AND '.join(clauses)
                total = db.execute('SELECT count(*) FROM cited_pages WHERE '+where, args).fetchone()[0]
                rows = [dict(r) for r in db.execute('SELECT * FROM cited_pages WHERE '+where+' ORDER BY id DESC LIMIT 100 OFFSET ?', (*args, (page-1)*100))]
                for r in rows:
                    result = json.loads(r.pop('result'))
                    r['characteristics'] = {k:result.get(k) for k in ('title','h1','word_count','author','date','structured_data','findings')}
                    r['capture_ids'] = [x[0] for x in db.execute('SELECT capture_id FROM cited_evidence WHERE page_id=?', (r['id'],))]
                job = db.execute('SELECT * FROM source_jobs WHERE project_id=?', (pid,)).fetchone()
                base.update(job=dict(job) if job else None, counts=dict(db.execute('SELECT status,count(*) FROM cited_pages WHERE project_id=? GROUP BY status', (pid,)).fetchall()))
                return dict(base, rows=rows, total=total)
            elif kind == 'related':
                rows = self.related(db, pid, int(get('query_id', '0')))
            elif kind == 'opportunities':
                rows = self.opportunity_rows(db, pid, captures, get('competitor'))
                if get('importance'): rows = [r for r in rows if r['importance'] == get('importance')]
                if get('group'): rows = [r for r in rows if any(str(g['id']) == get('group') for g in r['groups'])]
                if get('target'): rows = [r for r in rows if r['target'] == get('target')]
                if get('gap') == 'recurring': rows = [r for r in rows if r['recurring']]
                if get('gap') == 'any': rows = [r for r in rows if r['gap_checks']]
                if get('saved') == 'yes': rows = [r for r in rows if r['saved']]
                rows.sort(key=(lambda r: r['text'].casefold()) if get('sort') == 'query' else (lambda r: (IMPORTANCE.index(r['importance']), -(r['uncited_checks']/r['checks'] if r['checks'] else -1), -r['checks'], r['text'].casefold())))
            else:
                raise ValueError('Unknown strategy list.')
            if get('search'):
                rows = [r for r in rows if get('search').casefold() in str(r.get('text', r.get('domain',''))).casefold()]
            return dict(base, rows=rows[(page-1)*100:page*100], total=len(rows))

    def suggestions(self, pid, qid):
        with self.app.connect() as db:
            q = self.query(db, pid, qid)
            words = query_terms(q['text'])
            if not words: return []
            match = ' AND '.join('"'+word.replace('"','""')+'"' for word in words)
            return [dict(r) for r in db.execute("SELECT p.id,p.url,p.title,p.status,snippet(page_search,2,'[',']',' … ',24) AS heading_evidence,snippet(page_search,3,'[',']',' … ',24) AS content_evidence FROM page_search JOIN crawl_pages p ON p.id=page_search.rowid WHERE page_search MATCH ? AND p.project_id=? AND p.kind='page' ORDER BY bm25(page_search,2,8,6,1) LIMIT 5", (match,pid))]

    def related(self, db, pid, qid):
        if qid: self.query(db, pid, qid)
        rows, grouped = [], {}
        for c in db.execute("SELECT c.id,c.created,c.raw,q.id AS query_id,q.text FROM captures c JOIN queries q ON q.id=c.query_id WHERE q.project_id=? AND (?=0 OR q.id=?) AND c.status IN ('found','absent') ORDER BY c.id DESC", (pid,qid,qid)):
            raw = json.loads(c['raw'] or '{}')
            for field, label, key in (('people_also_ask','Question','question'),('related','Related search','text')):
                for item in raw.get(field,[]) if isinstance(raw.get(field),list) else []:
                    value = item.get(key) if isinstance(item,dict) else None
                    if not isinstance(value,str) or not value.strip() or len(value)>500: continue
                    marker = (query_key(value), label)
                    observation = dict(query_id=c['query_id'], source_query=c['text'], capture_id=c['id'], created=c['created'])
                    if marker not in grouped:
                        grouped[marker] = dict(text=value.strip(), type=label, **observation, observations=[])
                        rows.append(grouped[marker])
                    if observation not in grouped[marker]['observations']:
                        grouped[marker]['observations'].append(observation)
        return rows

    def mutate(self, body):
        pid, action = int(body.get('project_id',0)), body.get('action')
        with self.app.lock, self.app.connect() as db:
            project = self.project(db,pid)
            if action == 'competitor':
                d = self.domain(str(body.get('domain','')))
                if self.owned('https://'+d,project['domain']) or self.owned('https://'+project['domain'],d):
                    raise ValueError('Your own domain cannot be a competitor.')
                db.execute('INSERT OR IGNORE INTO competitors(project_id,domain) VALUES (?,?)',(pid,d))
            elif action == 'remove-competitor':
                db.execute('DELETE FROM competitors WHERE project_id=? AND domain=?',(pid,str(body.get('domain',''))))
            elif action == 'plan':
                qid = int(body.get('query_id',0)); self.query(db,pid,qid)
                importance, target = body.get('importance','Unspecified'),body.get('target','unreviewed')
                if importance not in IMPORTANCE or target not in ('unreviewed','existing','planned','missing'): raise ValueError('Choose a valid importance and target.')
                page_id = int(body.get('page_id') or 0) or None
                title,url,notes = (str(body.get(k,'' )).strip() for k in ('planned_title','planned_url','notes'))
                if len(title)>2000 or len(url)>2048 or len(notes)>10000: raise ValueError('Planning fields are too long.')
                if target == 'existing' and not db.execute("SELECT 1 FROM crawl_pages WHERE id=? AND project_id=? AND kind='page'",(page_id,pid)).fetchone(): raise ValueError('Select an owned page in this project.')
                if target == 'planned' and not title: raise ValueError('Give the planned page a title.')
                if url and (not normalize(url,scope=project['domain'])): raise ValueError('Planned URLs must belong to the tracked domain.')
                db.execute('INSERT INTO query_plans VALUES (?,?,?,?,?,?,?) ON CONFLICT(query_id) DO UPDATE SET importance=excluded.importance,notes=excluded.notes,target=excluded.target,page_id=excluded.page_id,planned_title=excluded.planned_title,planned_url=excluded.planned_url',(qid,importance,notes,target,page_id if target=='existing' else None,title if target=='planned' else '',url if target=='planned' else ''))
                groups = body.get('groups',[])
                if not isinstance(groups,list) or len(groups)>100 or any(not isinstance(g,str) or not g.strip() or len(g)>100 for g in groups): raise ValueError('Use up to 100 group names, each up to 100 characters.')
                db.execute('DELETE FROM query_groups WHERE query_id=?',(qid,))
                for name in dict.fromkeys(g.strip() for g in groups):
                    db.execute('INSERT OR IGNORE INTO topic_groups(project_id,name) VALUES (?,?)',(pid,name))
                    group = db.execute('SELECT id FROM topic_groups WHERE project_id=? AND name=?',(pid,name)).fetchone()[0]
                    db.execute('INSERT INTO query_groups VALUES (?,?)',(qid,group))
            elif action == 'opportunity':
                qid = int(body.get('query_id',0)); self.query(db,pid,qid)
                notes, effort = str(body.get('notes','')).strip(),body.get('effort','')
                if len(notes)>10000 or effort not in ('','Small','Medium','Large'): raise ValueError('Invalid opportunity notes or effort.')
                _,start,end = self.window({k:[str(body[k])] for k in ('start','end') if body.get(k)})
                _, captures = self.evidence(db,pid,start,end)
                row = next(r for r in self.opportunity_rows(db,pid,captures,str(body.get('competitor',''))) if r['id']==qid)
                # Preserve the original supporting captures when notes or effort are edited.
                db.execute('INSERT INTO opportunities VALUES (?,?,?,?,?) ON CONFLICT(query_id) DO UPDATE SET notes=excluded.notes,effort=excluded.effort',(qid,notes,effort,json.dumps(row['capture_ids'] or [c['id'] for c in captures if c['query_id']==qid]),utc()))
            elif action == 'view':
                name, filters = str(body.get('name','')).strip(),body.get('filters',{})
                allowed={'start','end','importance','group','target','gap','saved','sort','search','competitor'}
                if not name or len(name)>100 or not isinstance(filters,dict) or set(filters)-allowed or any(not isinstance(v,str) or len(v)>500 for v in filters.values()): raise ValueError('Invalid saved view.')
                self.window({k:[v] for k,v in filters.items() if k in ('start','end') and v})
                db.execute('INSERT INTO saved_views(project_id,name,filters) VALUES (?,?,?) ON CONFLICT(project_id,name) DO UPDATE SET filters=excluded.filters',(pid,name,json.dumps(filters)))
            elif action == 'publisher':
                d=self.domain(str(body.get('domain',''))); notes=str(body.get('notes','')); category=str(body.get('classification',''))
                if len(notes)>10000 or len(category)>100: raise ValueError('Publisher notes are too long.')
                db.execute('INSERT INTO publisher_notes VALUES (?,?,?,?,?) ON CONFLICT(project_id,domain) DO UPDATE SET shortlisted=excluded.shortlisted,classification=excluded.classification,notes=excluded.notes',(pid,d,int(bool(body.get('shortlisted'))),category,notes))
            elif action == 'classify':
                value=body.get('type','')
                if value not in TYPES: raise ValueError('Choose a page type.')
                if not db.execute('UPDATE cited_pages SET confirmed=? WHERE project_id=? AND id=?',(value,pid,int(body.get('id',0)))).rowcount: raise ValueError('Cited page not found.')
            elif action in ('pause-sources','resume-sources'):
                db.execute('INSERT INTO source_jobs(project_id,paused) VALUES (?,?) ON CONFLICT(project_id) DO UPDATE SET paused=excluded.paused,message=?',(pid,int(action=='pause-sources'),''))
            elif action == 'refresh-source':
                if not db.execute("UPDATE cited_pages SET status='queued',reason='' WHERE id=? AND project_id=? AND status!='running'",(int(body.get('id',0)),pid)).rowcount: raise ValueError('Page not found or already being retrieved.')
                db.execute('INSERT INTO source_jobs(project_id) VALUES (?) ON CONFLICT(project_id) DO UPDATE SET paused=0,message=\'\'',(pid,))
            elif action not in ('related-add','analyse-query'):
                raise ValueError('Unknown action.')
        if action == 'related-add':
            qid=int(body.get('query_id',0))
            with self.app.connect() as db:
                candidates=self.related(db,pid,qid)
                if not any(r['text']==body.get('text') and r['capture_id']==int(body.get('capture_id',0)) for r in candidates): raise ValueError('Choose a related question from saved evidence.')
                groups=self.groups(db,pid,qid)
            result=self.app.add_queries({'project_id':pid,'text':body['text']})
            if result['added']:
                with self.app.connect() as db:
                    new=db.execute('SELECT id FROM queries WHERE project_id=? AND text=?',(pid,' '.join(body['text'].split()))).fetchone()[0]
                    db.executemany('INSERT OR IGNORE INTO query_groups VALUES (?,?)',[(new,g['id']) for g in groups])
            return result
        if action == 'analyse-query':
            qid=int(body.get('query_id',0))
            with self.app.connect() as db:
                self.query(db,pid,qid)
                ids=[r[0] for r in db.execute("SELECT id FROM captures WHERE query_id=? AND status='found'",(qid,))]
            for cid in ids: self.enqueue_capture(cid, retrieve=True)
            return {'queued_from_captures':len(ids)}
        self.app.last_saved=utc()
        return {'ok':True}

    def enqueue_capture(self, cid, retrieve=False):
        with self.app.lock, self.app.connect() as db:
            c=db.execute("SELECT c.refs,q.project_id,p.domain FROM captures c JOIN queries q ON q.id=c.query_id JOIN projects p ON p.id=q.project_id WHERE c.id=? AND c.status='found'",(cid,)).fetchone()
            if not c: return
            pid=c['project_id']
            for ref in json.loads(c['refs']):
                if self.owned(ref['href'],c['domain']): continue
                url=normalize(ref['href'])
                if not url: continue
                db.execute('INSERT OR IGNORE INTO cited_pages(project_id,url,status) VALUES (?,?,?)',(pid,url,'queued' if retrieve else 'available'))
                page_id=db.execute('SELECT id FROM cited_pages WHERE project_id=? AND url=?',(pid,url)).fetchone()[0]
                db.execute('INSERT OR IGNORE INTO cited_evidence VALUES (?,?)',(page_id,cid))
                if retrieve:
                    db.execute("UPDATE cited_pages SET status='queued' WHERE id=? AND status='available'", (page_id,))
            if retrieve:
                db.execute('INSERT INTO source_jobs(project_id,paused) VALUES (?,0) ON CONFLICT(project_id) DO UPDATE SET paused=0,message=\'\'',(pid,))

    @staticmethod
    def classify(url,result):
        if urlsplit(url).path in ('','/'): return 'Unknown'
        text=' '.join([result.get('title',''),url,*result.get('h1',[])]).lower()
        for pattern, label in ((r'case[ -]stud','Case Study'),(r'\b(vs|versus|comparison|compare)\b','Comparison'),(r'\b(research|survey|study|report)\b','Research'),(r'\b(forum|discussion|thread)\b','Discussion'),(r'\b(guide|how to|tutorial)\b','Guide'),(r'\b(product|service|pricing)\b','Product/Service')):
            if re.search(pattern,text): return label
        return 'Other' if result.get('content') else 'Unknown'

    def retrieve(self, row):
        url=row['url']; scope=urlsplit(url).hostname
        status, final, code, reason, result='error',url,None,'',{}
        try:
            origin=f'{urlsplit(url).scheme}://{urlsplit(url).netloc}'
            key=(row['project_id'],origin)
            # No discovery: robots sitemaps and page links never enter either inventory here.
            policy=self.robots.get(key)
            if policy is None:
                _,rc,_,rb=self.app.crawler.fetch(origin+'/robots.txt',scope)
                if rc in (404,410): policy=Robots(); policy.parse([])
                elif rc==200: policy=Robots(); policy.parse(rb.decode('utf-8','replace').splitlines())
                else: raise ValueError(f'Robots policy unavailable (HTTP {rc}); retrieval not attempted.')
                self.robots[key]=policy
            def allowed(target, _scope=None):
                if (urlsplit(target).scheme,urlsplit(target).netloc) != (urlsplit(url).scheme,urlsplit(url).netloc):
                    raise ValueError('Cross-origin redirect requires a separate robots policy; not retrieved.')
                if not policy.can_fetch(AGENT,target): raise ValueError('Blocked by robots.txt.')
                if policy.crawl_delay(AGENT) or policy.request_rate(AGENT):
                    raise ValueError('Robots policy requires special crawl pacing; retrieval not attempted.')
                return True
            allowed(url)
            final,code,headers,body=self.app.crawler.fetch(url,scope,allowed)
            if code in (401,403,429): status='blocked'; reason=f'Access blocked (HTTP {code}).'
            elif code!=200: reason=f'Page unavailable (HTTP {code}).'
            elif 'html' not in headers.get('content-type','').lower(): status='unsupported'; reason='Not an HTML page.'
            else:
                parser=PageParser(final,scope); parser.feed(decode(body,headers))
                result=parser.result(headers.get('x-robots-tag','')); status='done'
        except Exception as error:
            reason=str(error)[:1000]
            if 'robots' in reason.lower(): status='blocked'
        with self.app.connect() as db:
            db.execute('UPDATE cited_pages SET status=?,final_url=?,http_status=?,retrieved=?,reason=?,result=?,suggested=? WHERE id=?',(status,final,code,utc(),reason,json.dumps(result),self.classify(final,result),row['id']))

    def batch(self):
        with self.app.lock,self.app.connect() as db:
            rows=db.execute("SELECT p.* FROM cited_pages p JOIN source_jobs j ON j.project_id=p.project_id WHERE j.paused=0 AND p.status='queued' ORDER BY p.id LIMIT 5").fetchall()
            if not rows: return False
            db.executemany("UPDATE cited_pages SET status='running' WHERE id=?",[(r['id'],) for r in rows])
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(self.retrieve,[dict(r) for r in rows]))
        return True

    def export(self,params):
        kind=params.get('kind',['opportunities'])[0]
        fields=('domain','numerator','denominator','query_count','cited_query_count','rate','first','last','capture_ids') if kind=='competitors' else ('text','importance','groups','target','page','owned_status','checks','gap_checks','recurring','competitors','latest_evidence','capture_ids','saved')
        fields=(*fields,'window_start','window_end')
        stream=io.StringIO(newline=''); writer=csv.writer(stream); writer.writerow(fields)
        page=1
        while True:
            data=self.listing(dict(params,page=[str(page)]))
            for row in data['rows']:
                row.update(window_start=data['start'],window_end=data['end'])
                values=[]
                for key in fields:
                    value=row.get(key,'')
                    value=json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else str(value if value is not None else '')
                    values.append("'"+value if value.lstrip().startswith(('=','+','-','@','\t','\r')) else value)
                writer.writerow(values)
            if page*100>=data['total']: break
            page+=1
        return ('\ufeff'+stream.getvalue()).encode('utf-8'),'text/csv; charset=utf-8',kind+'.csv'
