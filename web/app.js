/* Browser interface; data and credentials stay in the local Python service. */
let state = {projects: [], queries: [], usage: 0}, projectId = 0, view = 'dashboard', activeQuery = 0;
let token = '', filters = {search: '', status: '', start: '', end: '', citation:'', attention:''}, history = [], selectedCapture = 0;
let toastTimer, polling = false, workspaceError='';
const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stamp = value => value ? new Date(value).toLocaleString('en-GB', {dateStyle:'medium',timeStyle:'short'}) : 'Not checked';
const statusNames = {found:'AI Overview found',absent:'No AI Overview shown',failed:'Collection failed',incomplete:'Result incomplete',queued:'Waiting to run',running:'Collecting',cancelled:'Cancelled'};
const badge = status => `<span class="status ${escape(status || '')}">${escape(statusNames[status] || 'Not checked')}</span>`;
const project = () => state.projects.find(p => p.id === projectId);
const safeURL = value => {try {const u = new URL(value); return ['https:','http:'].includes(u.protocol) && !u.username ? u.href : '';} catch {return '';}};

async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Workspace-Token':token},body:JSON.stringify(body)};
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'The local request could not be completed.');
  return value;
}
function toast(message) {
  const box = $('#toast'); box.textContent = message; box.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => {box.hidden = true;}, 7000);
}
async function refresh(redraw = true) {
  state = await api(`/api/state?project_id=${projectId}`); token = state.token;
  if (state.milestone !== 3) throw new Error('Please close and relaunch Waddle to finish its upgrade.');
  if (!state.projects.some(p => p.id === projectId) && state.projects.length) {
    projectId = state.projects[0].id; state = await api(`/api/state?project_id=${projectId}`);
  }
  $('#project-select').innerHTML = '<option value="0">Select a project</option>' + state.projects.map(p => `<option value="${p.id}">${escape(p.name)}</option>`).join('');
  $('#project-select').value = String(projectId);
  $('#usage').textContent = `${state.usage} / 500 requests this month`;
  $('#usage').classList.toggle('warning', state.warning);
  $('#saved').textContent = state.save_error || `Local data saved · ${stamp(state.saved)}`;
  $('#project-domain').textContent=project()?.domain||''; workspaceError='';
  if (redraw) render();
}
function header(title, subtitle, action = '') {
  return `<header><div><h1>${escape(title)}</h1><p>${escape(subtitle)}</p></div>${action}</header>`;
}
function tabs(items){return `<div class="view-tabs" aria-label="Sections">${items.map(([label,attrs,active])=>`<button ${attrs} aria-pressed="${active}">${label}</button>`).join('')}</div>`;}
function queryTabs(){return tabs([['Tracked queries','data-view="queries"',view==='queries'],['Related questions','data-view="related"',view==='related']]);}
function sourceTabs(){return tabs([['Cited pages','data-view="sources"',view==='sources'],['Recurring domains','data-view="publishers"',view==='publishers']]);}
function loadFailure(message,attrs){return `<div class="notice error" role="alert"><p>${escape(message)} Saved evidence is retained.</p><button ${attrs}>Retry loading</button></div>`;}
function attentionMatches(q,kind){
  const c=q.last_usable,changes=c?.changes;
  return kind==='loss'?changes?.owned_before===true&&changes?.owned_now===false:kind==='gain'?changes?.owned_before===false&&changes?.owned_now===true:kind==='failed'?['failed','incomplete'].includes(q.latest?.status):kind==='stale'?Boolean(c&&Date.now()-new Date(c.created).getTime()>=7*86400000):kind==='important'?q.plan?.importance==='High'&&c?.status==='found'&&!c.refs.some(r=>r.owned):true;
}
function queryNext(q){
  const p=q.plan;
  return p?.target==='existing'&&p.page_id?`<button class="primary" data-review-page="${p.page_id}" data-review-query="${q.id}">Review ${escape(p.page_title||'target page')}</button>`:p?.target==='planned'?`<button class="primary" data-review-planned="${q.id}">Create new-page brief</button>`:`<button data-plan="${q.id}">Choose target page</button>`;
}

function newProjectButton(){return '<button class="primary" data-action="new-project">New project</button>';}
// Keep disclosures open through polling and filter redraws without persisting new preferences.
const disclosures = new Set();
document.addEventListener('toggle', event => {
  const key=event.target.dataset?.disclosure;if(!key)return;
  if(event.target.open)disclosures.add(key);else disclosures.delete(key);
}, true);
function disclosure(key, title, body, open=false, className='') {
  return `<details class="disclosure ${className}" data-disclosure="${key}" ${open||disclosures.has(key)?'open':''}><summary>${title}</summary>${body}</details>`;
}
function evidenceTrail(items){return `<ol class="evidence-trail" aria-label="Evidence trail">${items.map(([label,body])=>`<li><span>${escape(label)}</span>${body}</li>`).join('')}</ol>`;}
let routing=false, navigationReady=false, routeReturn='', routeSerial=0;
const viewMemory=new Map();
function routeHash(){
  const p=new URLSearchParams({project:projectId,view});
  if(view==='improvements'){p.set('tab',workKind);if(activeReview)p.set('review',activeReview.id);}
  if(view==='detail'){p.set('query',activeQuery);p.set('capture',selectedCapture);}
  const saved={};
  for(const [name,values] of Object.entries({queries:filters,pages:pageFilters,strategy:strategyFilters})){
    const active=Object.fromEntries(Object.entries(values).filter(([k,v])=>v&&!(k==='page'&&v===1)));
    if(Object.keys(active).length)saved[name]=active;
  }
  if(strategyPage>1)saved.strategyPage=strategyPage;
  if(improvementPage>1)saved.workPage=improvementPage;
  if(improvementSearch)saved.workSearch=improvementSearch;
  if(Object.keys(saved).length)p.set('filters',JSON.stringify(saved));
  if(reviewAnchor&&activeReview)p.set('anchor',reviewAnchor);
  if(routeReturn&&['detail','improvements'].includes(view))p.set('from',routeReturn);
  return '#'+p;
}
function commitRoute(push=false){
  if(routing||!navigationReady)return;
  const hash=routeHash();if(window.location.hash===hash)return;
  window.history[push?'pushState':'replaceState'](null,'',hash);
}
function safeFilters(defaults,value){return Object.fromEntries(Object.entries(defaults).map(([k,v])=>[k,typeof v==='number'?(Number.isSafeInteger(Number(value?.[k]))&&Number(value[k])>0?Number(value[k]):v):String(value?.[k]??v).slice(0,1000)]));}
async function restoreRoute(hash){
  const serial=++routeSerial,p=new URLSearchParams(hash.replace(/^#/,''));routing=true;
  try{
    const wanted=Number(p.get('project'))||0;
    if(wanted!==projectId){projectId=wanted;await refresh(false);}
    if(serial!==routeSerial)return;
    let saved={};try{saved=JSON.parse(p.get('filters')||'{}')||{};}catch{}
    filters=safeFilters({search:'',status:'',start:'',end:'',citation:'',attention:''},saved.queries);
    pageFilters=safeFilters({page:1,search:'',path:'',status:'',http:'',title:'',start:'',end:'',canonical_diff:'',restricted:''},saved.pages);
    strategyFilters=safeFilters({search:'',start:'',end:'',importance:'',group:'',target:'',gap:'',saved:'',sort:'',competitor:'',status:'',query_id:''},saved.strategy);
    strategyPage=Math.max(1,Number(saved.strategyPage)||1);improvementPage=Math.max(1,Number(saved.workPage)||1);improvementSearch=String(saved.workSearch||'').slice(0,1000);
    const allowed=['dashboard','queries','detail','pages','opportunities','competitors','sources','publishers','related','improvements','work','settings','onboarding'];
    view=allowed.includes(p.get('view'))?p.get('view'):'dashboard';
    workKind=['reviews','briefs','tasks'].includes(p.get('tab'))?p.get('tab'):'reviews';if(view==='work')view='improvements';
    activeReview=null;reviewAnchor=p.get('anchor')==='brief'?'brief':'';routeReturn=p.get('from')?.startsWith('#')?p.get('from'):'';activeQuery=Number(p.get('query'))||0;selectedCapture=Number(p.get('capture'))||0;
    strategyData=null;strategyKey='';strategyError='';improvementData=null;improvementKey='';improvementError='';pageError='';pageProject=-1;
    if(view==='detail'){
      if(!state.queries.some(q=>q.id===activeQuery))throw Error('This query is not in the selected project.');
      const result=await api('/api/history?query_id='+activeQuery);if(serial!==routeSerial)return;history=result;
      selectedCapture=history.some(c=>c.id===selectedCapture)?selectedCapture:history[0]?.id||0;
    }
    if(view==='improvements'&&Number(p.get('review'))){
      const result=await api(`/api/improvements?project_id=${projectId}&id=${Number(p.get('review'))}`);if(serial!==routeSerial)return;
      activeReview=result;reviewProject=projectId;
    }
    render();window.scrollTo(0,0);$('#main').focus({preventScroll:true});
    if(p.get('anchor')==='brief')$('#content-brief')?.scrollIntoView({block:'start'});
  }catch(e){workspaceError=e.message;render();}
  finally{if(serial===routeSerial){routing=false;navigationReady=true;commitRoute();}}
}
async function routeBack(){
  const target=routeReturn||'#'+new URLSearchParams({project:projectId,view:view==='detail'?'queries':'improvements'});
  window.history.pushState(null,'',target);await restoreRoute(target);
}
function destination(next){
  routeSerial++;routing=false;workspaceError='';
  if(strategyViews.includes(view))viewMemory.set(projectId+':'+view,{filters:{...strategyFilters},page:strategyPage});
  if(strategyViews.includes(next)&&next!==view){const saved=viewMemory.get(projectId+':'+next);strategyFilters=saved?.filters||{};strategyPage=saved?.page||1;strategyData=null;strategyKey='';strategyError='';}
  view=next;routeReturn='';if(next!=='detail')activeQuery=0;
  commitRoute(true);render();window.scrollTo(0,0);$('#main').focus({preventScroll:true});
}
window.addEventListener('popstate',()=>{
  if(unsavedReviewBrief()&&!confirm('You have unsaved review edits. Leave them without saving?')){window.history.pushState(null,'',routeHash());return;}
  restoreRoute(window.location.hash);
});
window.addEventListener('hashchange',()=>{if(!routing&&window.location.hash!==routeHash())restoreRoute(window.location.hash);});

function selectedQueries() {
  return state.queries.filter(q => attentionMatches(q,filters.attention) && (!filters.citation || (q.last_usable?(q.last_usable.refs.some(r=>r.owned)?'cited':'uncited'):'unknown')===filters.citation) && (!filters.search || q.text.toLowerCase().includes(filters.search.toLowerCase())) &&
    (!filters.status || q.latest?.status === filters.status) &&
    (!filters.start || q.latest?.created?.slice(0,10) >= filters.start) &&
    (!filters.end || (q.latest?.created && q.latest.created.slice(0,10) <= filters.end)));
}
function queryTable(queries, actions = false) {
  const rows = queries.map(q => {
    const latest = q.latest, good = q.last_usable, refs = good?.refs || [];
    const citation = good ? (refs.some(r => r.owned) ? 'Cited' : 'Not cited') : 'Unknown';
    return `<tr><td><button class="query-link" data-action="detail" data-id="${q.id}">${escape(q.text)}</button>${q.paused ? '<div class="small muted">Paused</div>' : ''}</td>
      <td>${badge(latest?.status)}${latest?.reason ? disclosure('query-reason-'+q.id,latest.status==='failed'?'Why this check failed':latest.status==='incomplete'?'What is incomplete':'Check details',`<p class="small muted">${escape(latest.reason)}</p>`) : ''}</td>
      <td>${citation}${good && good.id !== latest?.id ? `<div class="small muted">From last usable check · ${stamp(good.created)} <button class="text-button" data-action="detail" data-id="${q.id}" data-capture="${good.id}">Inspect</button></div>` : ''}</td><td>${stamp(latest?.created)}</td>
      <td>${actions ? `<div class="row-actions"><button data-action="check" data-id="${q.id}" ${q.pending || q.paused ? 'disabled':''}>Check</button>${disclosure('query-actions-'+q.id,'More',`<div class="toolbar"><button data-plan="${q.id}">Edit query plan</button><button data-action="pause" data-id="${q.id}" ${q.pending?'disabled':''}>${q.paused?'Resume':'Pause'}</button><button class="text-button danger" data-action="delete-query" data-id="${q.id}" ${q.pending?'disabled':''}>Delete</button></div>`)}</div>` : `<button class="text-button" data-action="detail" data-id="${q.id}">Inspect evidence →</button>`}</td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table class="query-table"><thead><tr><th>Query</th><th>Latest check</th><th>Your domain</th><th>Last checked</th><th>${actions?'Actions':'Evidence'}</th></tr></thead><tbody>${rows}</tbody></table>${queries.length ? '' : (state.queries.length?'<div class="empty"><h2>No matching queries</h2><p>Try another phrase or clear your filters.</p><button data-action="clear-filters">Clear filters</button></div>':empty())}</div>`;
}
function empty() {
  return `<div class="empty"><img class="empty-penguin" src="/assets/waddle-penguin-icon.png" width="80" height="80" alt=""><h2>${projectId?'Your queries start here':'Your first project starts here'}</h2><p>${projectId?'Add queries, then run a check to capture Google’s AI Overview and its sources.':'Add a domain and the Google queries you want to track.'}</p><button class="primary" data-action="${projectId?'add-queries':'new-project'}">${projectId?'Add queries':'Create project'}</button></div>`;
}
function filterBar() {
  return `<div class="filters"><label class="search">Search queries<input id="search-filter" value="${escape(filters.search)}" placeholder="Find a query"></label><label>Result<select id="status-filter"><option value="">All results</option>${Object.entries(statusNames).map(([k,v])=>`<option value="${k}" ${filters.status===k?'selected':''}>${v}</option>`).join('')}</select></label><label>Your domain<select id="citation-filter"><option value="">All citation states</option>${[['cited','Cited'],['uncited','Not cited'],['unknown','Unknown']].map(([v,t])=>`<option value="${v}" ${filters.citation===v?'selected':''}>${t}</option>`).join('')}</select></label><button data-action="clear-filters">Clear filters</button></div>${filters.attention?`<p class="filter-summary">Attention filter: ${escape({loss:'Owned citation losses',gain:'Owned citation gains',important:'High-importance uncited queries',failed:'Failed or incomplete checks',stale:'Stale evidence'}[filters.attention])}</p>`:''}`+disclosure('query-dates','Date filters'+(filters.start||filters.end?' · active':''),`<div class="filters"><label>From (UTC)<input type="date" id="start-filter" value="${filters.start}"></label><label>To (UTC)<input type="date" id="end-filter" value="${filters.end}"></label></div>`,Boolean(filters.start||filters.end));
}
function exportBar() {
  return `<div class="toolbar"><span class="small muted">Export captures:</span><button data-action="export" data-kind="queries">Queries CSV</button><button data-action="export" data-kind="citations">Citations CSV</button><button data-action="export" data-kind="json">Full JSON</button></div>`;
}
function dashboard() {
  const usable = state.queries.map(q => q.last_usable).filter(Boolean), found = usable.filter(c => c.status === 'found');
  const cited = found.filter(c => c.refs.some(r => r.owned));
  const changes = usable.map(c => c.changes).filter(Boolean);
  const gained=changes.filter(c=>c.owned_before===false&&c.owned_now===true).length,lost=changes.filter(c=>c.owned_before===true&&c.owned_now===false).length;
  const unique = new Set(found.flatMap(c => c.refs.map(r => r.href))).size;
  const subtitle = project() ? `${project().domain} · ${project().country.toUpperCase()} · ${project().language.toUpperCase()} · ${project().device}` : 'Track your domain in Google Search AI Overviews.';
  const recent=[...state.queries].sort((a,b)=>(b.latest?.id||0)-(a.latest?.id||0)).slice(0,6);
  return header('Dashboard', subtitle, projectId?`<button class="primary" data-action="check-all" ${!state.queries.some(q=>!q.paused&&!q.pending)?'disabled':''}>Check all queries</button>`:newProjectButton()) +
    onboardingPanel()+
    (state.queries.length?`<section class="overview-panel" aria-label="Latest usable results"><div class="overview-lead"><h2>Your domain in AI Overviews</h2><div class="overview-number">${found.length?cited.length:'—'} <span>${found.length?'of '+found.length:'No usable Overview yet'}</span></div><p>Overview results cite your domain</p><p class="small muted">Latest usable result per query · ${usable.length} of ${state.queries.length} queries with usable evidence</p></div><dl class="overview-stats"><div><dt>Tracked queries</dt><dd>${state.queries.length}</dd></div><div><dt>With an AI Overview</dt><dd>${found.length}</dd></div><div><dt>Owned-domain movement</dt><dd><button class="text-button" data-attention="gain">${gained} gained</button> · <button class="text-button" data-attention="loss">${lost} lost</button></dd></div><p class="small muted">Compared with each query’s previous comparable check</p></dl></section>`:'')+
    (state.metrics?.stale_queries?`<p class="notice">${state.metrics.stale_queries} queries have usable evidence at least seven days old. Inspect their dates before making decisions.</p>`:'')+notice()+
    dashboardAttention()+
    (state.queries.length?`<div class="section-heading"><h2>Recent checks</h2><button class="text-button" data-view="queries">All ${state.queries.length} queries →</button></div>`+queryTable(recent):empty())+
    (usable.length?disclosure('dashboard-evidence','Evidence window and limitations',`<p class="small">Overview coverage ${Math.round(found.length/usable.length*100)}% · Domain citation rate ${found.length?Math.round(cited.length/found.length*100)+'%':'not available'} · ${unique} unique cited URLs in latest usable Overview results.</p>`+evidenceMetrics()):'')+
    (usable.length ? `<div class="section-heading">${exportBar()}</div>`:'');
}
function dashboardAttention(){
  if(!state.queries.length)return '';
  const groups=[['loss','Owned citation losses'],['important','Important uncited queries'],['failed','Failed or incomplete checks'],['stale','Evidence at least seven days old']].map(([kind,title])=>({kind,title,rows:state.queries.filter(q=>attentionMatches(q,kind))})).filter(g=>g.rows.length);
  const ready=state.queries.filter(q=>q.plan?.importance==='High'&&q.last_usable?.status==='found'&&!q.last_usable.refs.some(r=>r.owned)).slice(0,3);
  return (groups.length?`<section class="attention-list"><h2>Needs attention</h2>${groups.map(g=>`<div><button class="text-button" data-attention="${g.kind}">${g.title}</button><strong>${g.rows.length}</strong><span class="small muted">${escape(g.rows.slice(0,2).map(q=>q.text).join(' · '))}</span></div>`).join('')}</section>`:'<p class="small muted">No citation losses, high-importance gaps, failed checks or stale evidence in the current saved results.</p>')+
    (ready.length?`<section class="next-work"><div class="section-heading"><h2>Next page work</h2><button class="text-button" data-view="opportunities">All opportunities →</button></div>${ready.map(q=>`<div><strong>${escape(q.text)}</strong>${queryNext(q)}</div>`).join('')}</section>`:`<p><button class="text-button" data-view="opportunities">Choose your next page improvement →</button></p>`);
}
function evidenceMetrics(){
  const m=state.metrics;if(!m)return '';
  const rate=n=>n?`${Math.round(m.cited/n*100)}%`:'Not available';
  const missing=state.queries.filter(q=>!q.last_usable).length,older=state.queries.filter(q=>q.last_usable&&q.latest?.id!==q.last_usable.id).length;
  return `<section class="evidence-metrics"><h3>Citation consistency across saved history</h3><p>Across all saved usable checks${m.start?` (${stamp(m.start)} to ${stamp(m.end)})`:''}: <strong>${m.cited} cited / ${m.overviews} Overview checks (${rate(m.overviews)})</strong>; <strong>${m.cited} cited / ${m.usable} usable checks (${rate(m.usable)})</strong>.</p><p class="small muted">Failed and incomplete checks are excluded. These are sampled observations, not a universal Google ranking.</p><p>${m.failed} failed · ${m.incomplete} incomplete · ${missing} queries without usable evidence · ${older} showing an older usable result · ${m.stale_queries} last usable results at least seven days old</p></section>`;
}

function notice() {
  return (state.warning ? '<div class="notice">You have reached 400 requests this month. The 500-request guide is advisory; checks can continue.</div>' : '') +
    (state.pending_count ? `<div class="progress" role="status">${state.pending_count} query check${state.pending_count===1?'':'s'} running or waiting. Summary and screenshot are saved automatically.</div>` : '');
}
function queriesView() {
  return header('Queries', project()?`Tracked searches for ${project().domain}`:'Choose or create a project to track queries.', projectId?'<button class="primary" data-action="add-queries">Add queries</button>':newProjectButton()) + queryTabs() + notice() +
    (projectId ? filterBar() + `<div class="section-heading"><h2>${selectedQueries().length} ${selectedQueries().length===1?'query':'queries'}</h2><button data-action="check-all" ${!state.queries.some(q=>!q.paused&&!q.pending)?'disabled':''}>Check all queries</button></div>` + queryTable(selectedQueries(),true) + `<div class="section-heading">${exportBar()}</div>` : empty());
}
function settings(){
  const usage=`<p>${state.usage} requests in ${escape(state.month)} · advisory guide 500; warning at 400.</p><p class="small muted">Waddle's dispatched requests only, not account-wide billing. Each check normally uses a summary and a screenshot request. Unknown outcomes are never automatically retried.</p>`;
  return header('Settings','Saved configuration, connection tests and local workspace controls.')+`<div class="settings-form"><section><h2>Search collection</h2><p>Bright Data · ${state.key_saved?'API key saved':'No API key saved'}</p><p class="small">Last connection result: ${escape(state.connection)}</p><button data-action="test">Test connection</button>${disclosure('replace-key','Replace API key',`<form id="key-form"><label>New API key<input name="key" type="password" autocomplete="new-password" minlength="10" maxlength="512" required placeholder="Enter a new Bright Data API key"></label><button class="primary" type="submit">Save API key</button></form>`)}<h3>Monthly usage</h3>${usage}</section>
    ${ollamaSettingsView()}
    ${project()?`<section><h2>Current project</h2><p>${escape(project().name)} · ${escape(project().domain)}</p><p>${project().country.toUpperCase()} · ${project().language.toUpperCase()} · ${project().device}. Subdomains included.</p><button data-setup="reopen">Show project guidance</button></section>`:''}
    <section><h2>Storage and backups</h2><p><code>${escape(state.data_folder)}</code></p><p>Changes save immediately. A local backup is written every five minutes. Captures remain until deleted.</p><p>Last backup: ${stamp(state.backup)}</p><button data-action="save">Save now</button></section>
    <section><h2>Application controls</h2><p>Save and stop the local service. Pause crawls/retrieval and finish in-flight work first; cancel active reviews. Launch.cmd opens Waddle again.</p><button data-action="close">Save and close application</button></section>
    ${project()?`<section><h2>Delete project history</h2><p>Delete ${escape(project().name)} and its saved queries, captures and work.</p><button class="danger" data-action="delete-project">Delete project and its history</button></section>`:''}</div>`;
}

function refsFor(block, refs) {
  const indexes = Array.isArray(block.reference_indexes) ? block.reference_indexes : [];
  return refs.filter(r => indexes.includes(r.index));
}
function content(blocks, refs) {
  if (!Array.isArray(blocks)) return '';
  return blocks.filter(b=>b&&typeof b==='object').map(block => {
    let html = block.title ? `<h3>${escape(block.title)}</h3>` : '';
    const value = block.snippet || block.text || block.code || '';
    if (block.type === 'title') html += `<h3>${escape(value)}</h3>`;
    else if (block.type === 'code') html += `<pre>${escape(value)}</pre>`;
    else if (value) html += `<p>${escape(value)}</p>`;
    if (Array.isArray(block.list)) html += `<ul>${block.list.map(b=>`<li>${content([b],refs)}</li>`).join('')}</ul>`;
    const inline = refsFor(block, refs);
    if (inline.length) html += `<div class="inline-citations">${inline.map(r=>`<a href="${escape(safeURL(r.href))}" target="_blank" rel="noopener noreferrer">${escape(r.source)}${r.owned?' · Your domain':''}</a>`).join('')}</div>`;
    if (Array.isArray(block.links)) html += `<div class="inline-citations">${block.links.filter(l=>safeURL(l.link)).map(l=>`<a href="${escape(safeURL(l.link))}" target="_blank" rel="noopener noreferrer">${escape(l.text || 'Source link')}</a>`).join('')}</div>`;
    return html;
  }).join('');
}
function plain(blocks) {
  if (!Array.isArray(blocks)) return '';
  return blocks.filter(b=>b&&typeof b==='object').map(b => [b.title,b.snippet || b.text || b.code,plain(b.list)].filter(Boolean).join('\n')).join('\n\n');
}
function detail() {
  const q = state.queries.find(q=>q.id===activeQuery), capture = history.find(c=>c.id===selectedCapture) || history[0];
  if (!q) {view='queries';return queriesView();}
  if (!capture) return header(q.text,'No captures yet.', '<button data-action="back">Back to previous view</button>') + `<div class="empty"><p>Run this query to capture its AI Overview, sources and screenshot.</p><button class="primary" data-action="check" data-id="${q.id}" ${q.paused?'disabled':''}>Check now</button></div>`;
  const refs = capture.refs || [], context = capture.context, changes = capture.changes;
  return header(q.text, `${context.country.toUpperCase()} · ${context.language.toUpperCase()} · ${context.device} · ${stamp(capture.created)}`, '<button data-action="back">Back to previous view</button>') +
    firstResultGuide(capture)+
    `<div class="history-row"><div class="toolbar">${badge(capture.status)}<strong>${capture.status==='found'?(refs.some(r=>r.owned)?'Your domain is cited':'Your domain is not cited'):'Citation result unavailable'}</strong></div><label class="sr-only" for="history-select">Capture history</label><select id="history-select">${history.map(c=>`<option value="${c.id}" ${c.id===capture.id?'selected':''}>${stamp(c.created)} · ${statusNames[c.status]||c.status}</option>`).join('')}</select><button class="primary" data-action="check" data-id="${q.id}" ${q.pending||q.paused?'disabled':''}>Check now</button></div>` +
    evidenceTrail([['Query',`<strong>${escape(q.text)}</strong>`],['Capture',`<strong>#${capture.id} · ${escape(statusNames[capture.status])}</strong><small>${stamp(capture.created)}</small>`],['Target page',`<strong>${escape(q.plan?.page_title||q.plan?.planned_title||'Not selected')}</strong><button class="text-button" data-plan="${q.id}">Inspect page mapping →</button>`],['Next step',queryNext(q)]])+
    (capture.reason?`<div class="notice error">${escape(capture.reason)}</div>`:'') + (capture.warning?`<div class="notice">Bright Data: ${escape(capture.warning)}</div>`:'') +
    (Object.keys(capture.diagnostics||{}).length?`<details><summary>Safe collection diagnostics</summary><dl>${Object.entries(capture.diagnostics).map(([k,v])=>`<dt>${escape(k.replaceAll('_',' '))}</dt><dd>${escape(v)}</dd>`).join('')}</dl><p class="small muted">No raw error body or credentials retained. An unreadable response does not establish whether Google showed an Overview.</p></details>`:'') +
    (changes?`<p class="small"><strong>Since the previous comparable check:</strong> ${changes.owned_before!==changes.owned_now?(changes.owned_now?'Your domain gained a citation.':'Your domain lost its citation.'):'Owned-domain citation unchanged.'} ${changes.text_changed?'Overview text changed.':''} ${changes.gained_urls.length} source URLs gained · ${changes.lost_urls.length} lost.</p>`:'')+
    `<div class="detail-grid"><section class="summary"><h2>AI Overview</h2><div class="summary-content">${content(capture.overview?.texts,refs) || `<p class="muted">${capture.status==='absent'?'Google did not show an AI Overview in this check.':'No summary content was captured.'}</p>`}</div></section><section class="source-panel"><h2>${refs.length} cited sources</h2><button class="text-button" data-query-sources="${q.id}">Inspect cited pages →</button><button class="text-button" data-query-related="${q.id}">Related questions →</button>${refs.map(r=>`<div class="source ${r.owned?'owned':''}"><a class="source-title" href="${escape(safeURL(r.href))}" target="_blank" rel="noopener noreferrer">${escape(r.title)}</a>${(r.title||'').length>160?disclosure('capture-title-'+capture.id+'-'+refs.indexOf(r),'Full source title',`<p>${escape(r.title)}</p>`):''}<small>${escape(r.domain)}${r.owned?' · Your domain':''}</small><small>${escape(r.href)}</small></div>`).join('') || '<p class="muted">No source URLs in this capture.</p>'}</section></div>` +
    `<section class="screenshot"><div class="section-heading"><h2>Saved screenshot</h2>${capture.has_screenshot?`<a href="/api/screenshot?id=${capture.id}" target="_blank" rel="noopener">Open full image</a>`:''}</div>${capture.has_screenshot?`<p class="small muted">Screenshot collected ${stamp(capture.screenshot_time)} in a separate search request; it may differ from the summary above.</p><img src="/api/screenshot?id=${capture.id}" alt="Google Search screenshot for ${escape(q.text)}" loading="lazy">`:`<p class="muted">${escape(capture.screenshot_error || (q.pending?'Screenshot collection is in progress.':'No screenshot available for this capture.'))}</p>`}</section>` +
    (changes?disclosure('capture-changes','Detailed citation and text changes',`<section class="changes"><h3>Changes since the previous comparable check</h3><p>${changes.overview_changed?'AI Overview appeared or disappeared. ':''}${changes.owned_before!==changes.owned_now?(changes.owned_now?'Your domain gained a citation. ':'Your domain lost its citation. '):''}${changes.text_changed?'Summary text changed.':'Summary text unchanged.'}</p><div class="compare"><div><strong>Gained URLs (${changes.gained_urls.length})</strong><ul>${changes.gained_urls.map(x=>`<li>${escape(x)}</li>`).join('')}</ul><p class="small">New domains: ${escape(changes.gained_domains.join(', ') || 'None')}</p></div><div><strong>Lost URLs (${changes.lost_urls.length})</strong><ul>${changes.lost_urls.map(x=>`<li>${escape(x)}</li>`).join('')}</ul><p class="small">Removed domains: ${escape(changes.lost_domains.join(', ') || 'None')}</p></div></div><button data-action="compare" data-id="${capture.id}">Compare summaries</button><div id="comparison"></div></section>`):'') +disclosure('source-history','Source history · URLs and domains',sourceHistory());
}
function sourceHistory() {
  const refs=[...new Map(history.flatMap(c=>c.refs).map(r=>[r.href,r])).values()];
  if (!refs.length) return '<p class="muted">No sources to show.</p>';
  const table=(items,field,label)=>`<div class="table-wrap"><table><thead><tr><th>${label}</th><th>First seen</th><th>Last seen</th></tr></thead><tbody>${items.map(r=>{const seen=history.filter(c=>c.refs.some(x=>x[field]===r[field]));return `<tr><td>${escape(r[field])}</td><td>${stamp(seen.at(-1)?.created)}</td><td>${stamp(seen[0]?.created)}</td></tr>`;}).join('')}</tbody></table></div>`;
  return table(refs,'href','Source URL') + '<h3>Domains</h3>' + table([...new Map(refs.map(r=>[r.domain,r])).values()],'domain','Source domain');
}
function render() {
  const focused=document.activeElement;
  const focusSelector=focused?.matches('[data-strategy-filter]')?`[data-strategy-filter="${focused.dataset.strategyFilter}"]`:focused?.id==='history-select'?'#history-select':null;
  document.querySelectorAll('#main details[data-disclosure]').forEach(el=>{if(el.open)disclosures.add(el.dataset.disclosure);else disclosures.delete(el.dataset.disclosure);});
  document.querySelectorAll('.nav').forEach(b=>{const active=b.dataset.view===(['detail','related'].includes(view)?'queries':view==='publishers'?'sources':view==='work'?'improvements':view);b.classList.toggle('active',active);if(active)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
  $('#main').innerHTML = workspaceError?header('Workspace unavailable','Reconnect to the local service.')+loadFailure(workspaceError,'data-action="retry-workspace"'):view==='onboarding'?setupView():['improvements','work'].includes(view)?improvementsView():strategyViews.includes(view)?strategyView():view==='pages'?pagesView():view==='settings'?settings():view==='queries'?queriesView():view==='detail'?detail():dashboard();
  if(view==='settings')queueMicrotask(loadOllamaSettings);
  if (view==='detail') document.querySelectorAll('.source:not(.owned)').forEach(el=>{const a=el.querySelector('a');if(a)el.insertAdjacentHTML('beforeend',`<button class="text-button" data-competitor="${escape(new URL(a.href).hostname)}">Add competitor</button>`);});
  if(focusSelector)$(focusSelector)?.focus({preventScroll:true});
  commitRoute();
}
async function run(ids) {
  const result = await api('/api/run',{project_id:projectId,...(ids?{query_ids:ids}:{})});
  toast(result.queued?`${result.queued} query check${result.queued===1?'':'s'} queued.`:'No active queries to queue.');
  await refresh();
}
async function showDetail(id,captureId=0){
  const serial=++routeSerial,pid=projectId,result=await api(`/api/history?query_id=${id}`);if(pid!==projectId||serial!==routeSerial)return;
  routeReturn=routeHash();activeQuery=id;history=result;selectedCapture=history.some(c=>c.id===captureId)?captureId:history[0]?.id||0;
  view='detail';commitRoute(true);render();window.scrollTo(0,0);$('#main').focus({preventScroll:true});
}

document.addEventListener('click', async event => {
  const button=event.target.closest('button'); if(!button) return;
  if(button.dataset.close){document.getElementById(button.dataset.close).close();return;}
  if(button.dataset.view){activeReview=null;improvementKey='';strategyError='';strategyKey='';strategyData=null;destination(button.dataset.view==='work'?'improvements':button.dataset.view);return;}
  if(button.dataset.querySources||button.dataset.queryRelated){const next=button.dataset.querySources?'sources':'related';destination(next);strategyFilters={query_id:button.dataset.querySources||button.dataset.queryRelated};strategyKey='';strategyData=null;render();return;}
  if(button.dataset.attention){filters={search:'',status:'',start:'',end:'',citation:'',attention:button.dataset.attention};destination('queries');return;}
  const action=button.dataset.action; if(!action) return;
  const id=Number(button.dataset.id);button.disabled=true;
  try {
    if(action==='retry-workspace'){await refresh(false);await restoreRoute(window.location.hash);}
    else if(action==='new-project'){beginSetup();}
    else if(action==='add-queries'){$('#query-dialog').showModal();}
    else if(action==='detail')await showDetail(id,Number(button.dataset.capture)||0);
    else if(action==='back')await routeBack();
    else if(action==='check')await run([id]);
    else if(action==='check-all')await run();
    else if(action==='pause'){const q=state.queries.find(q=>q.id===id);await api('/api/pause',{id,paused:!q.paused});await refresh();}
    else if(action==='delete-query'){
      if(confirm('Delete this query and all its saved results and screenshots?')){await api('/api/delete-query',{id});await refresh();}
    } else if(action==='delete-project'){
      if(confirm('Delete this project, all its queries, results and screenshots?')){await api('/api/delete-project',{id:projectId});projectId=0;await refresh();}
    } else if(action==='test'){const result=await api('/api/test-connection',{});toast(result.message);await refresh();}
    else if(action==='save'){await api('/api/save',{});toast('Local workspace and backup saved.');await refresh(false);}
    else if(action==='close'){await api('/api/close',{});clearInterval(pollTimer);$('#main').innerHTML=header('Workspace saved','The application is closed. Use Launch.cmd to open it again.');}
    else if(action==='clear-filters'){filters={search:'',status:'',start:'',end:'',citation:'',attention:''};render();}
    else if(action==='export'){
      if(filters.start && filters.end && filters.start>filters.end)throw new Error('Choose a start date before the end date.');
      const params=new URLSearchParams({project_id:button.dataset.projectId||projectId,kind:button.dataset.kind,run_id:button.dataset.runId||'',start:filters.start,end:filters.end});
      const response=await fetch('/api/export?'+params);if(!response.ok)throw new Error((await response.json()).error);
      const blob=await response.blob(), url=URL.createObjectURL(blob), link=document.createElement('a');
      link.href=url;link.download=button.dataset.kind==='json'?'AI Overview captures.json':`AI Overview ${button.dataset.kind}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    } else if(action==='compare'){
      const current=history.find(c=>c.id===id), previous=history.find(c=>c.id===current.previous_id) || await api('/api/capture?id='+current.previous_id);
      const oldText=plain(previous.overview.texts), newText=plain(current.overview.texts);
      const highlight=(text,other)=>text.split('\n').map(line=>line && !other.split('\n').includes(line)?`<mark>${escape(line)}</mark>`:escape(line)).join('\n');
      $('#comparison').innerHTML=`<div class="compare"><div><h3>Previous · ${stamp(previous.created)}</h3><article>${highlight(oldText||'No AI Overview',newText)}</article></div><div><h3>Selected · ${stamp(current.created)}</h3><article>${highlight(newText||'No AI Overview',oldText)}</article></div></div>`;
    }
  } catch(error){toast(error.message);} finally{if(button.isConnected)button.disabled=false;}
});
document.addEventListener('submit',async event=>{
  if(!['query-form','key-form','inclusion-form'].includes(event.target.id))return;
  const form=event.target;event.preventDefault();const button=form.querySelector('button[type=submit]');button.disabled=true;
  try{
    const body=Object.fromEntries(new FormData(form));
    if(form.id==='query-form'){const result=await api('/api/queries',{...body,project_id:projectId});form.reset();$('#query-dialog').close();view='queries';toast(`${result.added} queries added${result.duplicates?`; ${result.duplicates} duplicates skipped`:''}.`);await refresh();}
    else if(form.id==='key-form'){await api('/api/key',body);form.reset();toast('API key saved using Windows protected storage.');await refresh();}
    else if(form.id==='inclusion-form'){await api('/api/inclusion',{...body,project_id:projectId});toast('User-confirmed inclusion check saved.');await loadPages();}
  }catch(error){toast(error.message);}finally{if(button.isConnected)button.disabled=false;}
});
$('#project-select').addEventListener('change',async event=>{projectId=Number(event.target.value);view='dashboard';activeReview=null;improvementData=null;improvementKey='';filters={search:'',status:'',start:'',end:'',citation:'',attention:''};strategyError='';pageError='';improvementError='';routeReturn='';try{await refresh(false);commitRoute(true);render();window.scrollTo(0,0);$('#main').focus({preventScroll:true});}catch(error){toast(error.message);}});

$('#save').addEventListener('click',async()=>{try{await api('/api/save',{});await refresh(false);toast('Local workspace and backup saved.');}catch(error){toast(error.message);}});
document.addEventListener('change',async event=>{
  const id=event.target.id;
  if(id==='history-select'){selectedCapture=Number(event.target.value);render();return;}
  const key={'citation-filter':'citation','status-filter':'status','start-filter':'start','end-filter':'end'}[id];if(key){filters[key]=event.target.value;render();}
});
document.addEventListener('input',event=>{
  if(event.target.id==='search-filter'){const input=event.target, pos=input.selectionStart;filters.search=input.value;render();const next=$('#search-filter');next.focus();next.setSelectionRange(pos,pos);}
});
async function poll(){
  if(polling || document.hidden)return;polling=true;
  try{
    const running=state.pending_count>0;await refresh(false);
    if((view==='pages'||retryMonitorProject===projectId)&&!pageLoading&&!document.activeElement?.matches('input,textarea,select')&&!document.querySelector('dialog[open]'))await loadPages();
    if(['improvements','work'].includes(view))await pollImprovements();
    if(running || state.pending_count){
      if(view==='detail'){history=await api(`/api/history?query_id=${activeQuery}`);if(!selectedCapture)selectedCapture=history[0]?.id||0;}
      if(!['settings','improvements','work','onboarding'].includes(view) && !document.querySelector('dialog[open]'))render();
    }
  }catch(error){toast(error.message);}finally{polling=false;}
}
const navPaths={dashboard:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',queries:'M21 21l-5-5 M10.5 18a7.5 7.5 0 1 0 0-15 7.5 7.5 0 0 0 0 15',pages:'M8 3h9l4 4v14H8z M3 7v14 M16 3v5h5',opportunities:'M9 18h6 M10 21h4 M8 14c-5-6 0-13 6-10s4 7 2 10l-1 2H9z',competitors:'M16 21v-3c0-4-12-4-12 0v3 M10 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M18 4a4 4 0 0 1 0 7 M21 21v-3c0-2-1-3-3-4',sources:'M9 15l6-6 M8 12H6a5 5 0 0 1 0-10h3 M16 12h2a5 5 0 0 1 0 10h-3',related:'M4 3h16v14H9l-5 4z M8 7h8 M8 11h5',publishers:'M4 21V3h16v18 M8 7h2 M14 7h2 M8 11h2 M14 11h2 M10 21v-6h4v6',improvements:'M4 3h10l4 4v5 M4 3v18h8 M14 3v5h4 M14 18l5-5 3 3-5 5h-3z',work:'M9 5h12 M9 12h12 M9 19h12 M3 5l1 1 2-3 M3 12l1 1 2-3 M3 19l1 1 2-3',settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z'};
document.querySelectorAll('.sidebar .nav[data-view]').forEach(b=>b.insertAdjacentHTML('afterbegin',`<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${navPaths[b.dataset.view]}"></path></svg>`));
const initialRoute=window.location.hash;refresh(false).then(()=>restoreRoute(initialRoute)).catch(error=>{workspaceError=error.message;render();});const pollTimer=setInterval(poll,2500);
