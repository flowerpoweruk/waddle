/* Planning uses saved observations only. Adding a query never runs a search. */
const strategyViews = ['competitors','opportunities','publishers','sources','related'];
let strategyData = null,strategyLoadedKey='', strategyKey = '', strategyRequest = 0,strategyError='';
let strategyFilters = {}, strategyPage = 1, planningQuery = 0, planningRows = [], planningSuggestions = [], pageChoice = null, pageChoicePage = 1;
let targetSearchTimer, pageSearchRequest = 0;
const types = ['Guide','Comparison','Product/Service','Case Study','Research','Discussion','Other','Unknown'];
const targetNames = {unreviewed:'Unreviewed — coverage unknown',existing:'Existing page',planned:'Planned page',missing:'No suitable page — confirmed'};
const sButton = (action, label, attrs='') => `<button data-strategy="${action}" ${attrs}>${escape(label)}</button>`;
const options = (values, selected) => values.map(v=>`<option value="${escape(v)}" ${v===selected?'selected':''}>${escape(v)}</option>`).join('');
const captureLinks = ids => ids?.length ? ids.map(id=>`<button class="text-button" data-evidence="${Number(id)}">Capture #${Number(id)}</button>`).join(' ') : '<span class="muted">No supporting captures</span>';
const queryKey=text=>text.trim().replace(/\s+/g,' ').toLowerCase();
const evidenceAge = date => Math.max(0,Math.floor((Date.now()-new Date(date).getTime())/86400000))+' days old';
function ownedStatusLabel(row){
  const domain=project()?.domain||'Your domain';
  return {'No owned URL cited':`${domain} not cited`,'Mapped page not cited':`${domain} not cited`,'Mapped URL cited':`${domain} cited — your target page cited`,'Different owned URL cited':`${domain} cited — your target page not cited`,'Owned URL cited; mapping unreviewed':`${domain} cited — target not reviewed`,'Owned URL cited; no existing target selected':`${domain} cited — no existing target selected`}[row.owned_status]||row.owned_status;
}

function strategyParams(kind=view) {
  return new URLSearchParams({project_id:projectId,kind,page:strategyPage,...Object.fromEntries(Object.entries(strategyFilters).filter(([,v])=>v))});
}
async function loadStrategy(force=false) {
  if (!projectId || !strategyViews.includes(view)) return;
  const key = strategyParams().toString();
  if (!force && (key===strategyKey||strategyError)) return;strategyError='';
  strategyKey=key; const serial=++strategyRequest;
  try {
    const data=await api('/api/strategy?'+key);
    if(serial!==strategyRequest || key!==strategyParams().toString()) return;
    strategyData=data;strategyLoadedKey=key;strategyError='';render();
  } catch(error) {if(serial===strategyRequest){strategyKey='';strategyError=error.message;render();}}
}
function strategyFiltersHTML(data) {
  return `<div class="filters">
    <label class="search">Search<input data-strategy-filter="search" value="${escape(strategyFilters.search||'')}" placeholder="${view==='sources'?'URL or page information':'Query or domain'}"></label>
    ${view==='opportunities'?`<label>Importance<select data-strategy-filter="importance"><option value="">All</option>${options(['High','Medium','Low','Unspecified'],strategyFilters.importance)}</select></label>`:''}
    ${view==='related'?`<label>Source query<select data-strategy-filter="query_id"><option value="">All queries</option>${state.queries.map(q=>`<option value="${q.id}" ${String(q.id)===strategyFilters.query_id?'selected':''}>${escape(q.text)}</option>`).join('')}</select></label>`:''}
    ${view==='sources'?`<label>Source query<select data-strategy-filter="query_id"><option value="">All queries</option>${state.queries.map(q=>`<option value="${q.id}" ${String(q.id)===strategyFilters.query_id?'selected':''}>${escape(q.text)}</option>`).join('')}</select></label><label>Content availability<select data-strategy-filter="status"><option value="">All statuses</option>${Object.entries(sourceStates).map(([v,t])=>`<option value="${v}" ${strategyFilters.status===v?'selected':''}>${t}</option>`).join('')}</select></label>`:''}
    ${sButton('clear','Clear filters')}</div>`+
    (['sources','related'].includes(view)?'':disclosure('strategy-filters-'+view,'Evidence window & more filters'+(Object.entries(strategyFilters).some(([k,v])=>!['search','importance'].includes(k)&&v)?' · active':''),`<div class="filters"><label>From (UTC)<input type="date" data-strategy-filter="start" value="${escape(strategyFilters.start||data.start)}"></label><label>To (UTC)<input type="date" data-strategy-filter="end" value="${escape(strategyFilters.end||data.end)}"></label>
    ${view==='opportunities'?`
      <label>Group<select data-strategy-filter="group"><option value="">All groups</option>${data.groups.map(g=>`<option value="${g.id}" ${String(g.id)===strategyFilters.group?'selected':''}>${escape(g.name)}</option>`).join('')}</select></label>
      <label>Target<select data-strategy-filter="target"><option value="">All targets</option>${Object.entries(targetNames).map(([k,v])=>`<option value="${k}" ${strategyFilters.target===k?'selected':''}>${v}</option>`).join('')}</select></label>
      <label>Competitor gap<select data-strategy-filter="gap"><option value="">All</option><option value="any" ${strategyFilters.gap==='any'?'selected':''}>Any observed gap</option><option value="recurring" ${strategyFilters.gap==='recurring'?'selected':''}>Recurring only</option></select></label>
      <label>Saved<select data-strategy-filter="saved"><option value="">All</option><option value="yes" ${strategyFilters.saved==='yes'?'selected':''}>Saved only</option></select></label>
      <label>Sort<select data-strategy-filter="sort"><option value="">Importance and non-citation rate</option><option value="query" ${strategyFilters.sort==='query'?'selected':''}>Query name</option></select></label>`:''}
    ${['opportunities','competitors'].includes(view)?`<label>Compare competitor<select data-strategy-filter="competitor"><option value="">All selected competitors</option>${data.competitors.map(c=>`<option ${strategyFilters.competitor===c.domain?'selected':''}>${escape(c.domain)}</option>`).join('')}</select></label>`:''}
    </div>`))+(!['sources','related'].includes(view)?`<p class="date-note">Evidence window: ${escape(strategyFilters.start||data.start)} to ${escape(strategyFilters.end||data.end)} (UTC)</p>`:'');
}
const sourceStates={available:'Not retrieved',queued:'Waiting',running:'Retrieving',done:'Content saved',blocked:'Blocked',error:'Retrieval failed',unsupported:'Not HTML'};
function strategyView() {
  const names={competitors:'Competitors',opportunities:'Opportunities',publishers:'Cited sources',sources:'Cited sources',related:'Queries'};
  const subtitles={competitors:'Compare selected competitors using the same query set and search settings.',opportunities:'Prioritise work using business importance, confirmed pages and observed citation gaps.',publishers:'See which third-party domains repeatedly appear in captured AI Overviews.',sources:'Inspect the exact pages cited for your searches and choose useful comparisons.',related:'Discover tracking queries from saved Google questions and related searches.'};
  if(!projectId) return header(names[view],'Choose a project to begin.');
  if(!strategyError)queueMicrotask(()=>loadStrategy());
  const sectionTabs=view==='related'?queryTabs():['sources','publishers'].includes(view)?sourceTabs():'';
  if(strategyError&&!strategyData)return header(names[view],subtitles[view])+sectionTabs+loadFailure(strategyError,'data-strategy="reload"');
  if(!strategyData || (!strategyError&&strategyLoadedKey!==strategyParams().toString())) return header(names[view],subtitles[view])+sectionTabs+'<p role="status">Loading saved evidence…</p>';
  const data=strategyData;
  let body='';
  if(view==='competitors') {
    body=`<div class="table-wrap"><table><thead><tr><th>Domain</th><th>Citation rate</th><th>Queries</th><th>Evidence</th><th></th></tr></thead><tbody>${data.rows.map(r=>`<tr><td>${escape(r.domain)}${r.owned?' · Your domain':''}</td><td>${r.rate===null?'Not available':r.rate+'%'}<div class="small">${r.numerator} cited / ${r.denominator} usable Overview checks</div></td><td>${r.cited_query_count} cited / ${r.query_count} checked</td><td><details><summary>${r.numerator} supporting captures</summary>${captureLinks(r.capture_ids)}<p>${stamp(r.first)} – ${stamp(r.last)}</p></details></td><td>${r.owned?'':sButton('remove-competitor','Remove',`data-domain="${escape(r.domain)}"`)}</td></tr>`).join('')}</tbody></table></div>`+disclosure('add-competitor','Add competitor',`<form id="competitor-form" class="toolbar"><label>Competitor domain<input name="domain" required placeholder="competitor.com"></label><button class="primary">Add competitor</button></form>`)+disclosure('competitor-method','How citation rates are calculated','<p>Rates use usable AI Overview checks only; a domain counts once per capture. Percentages need not total 100%. Failed/incomplete checks do not count as absence.</p>');
  } else if(view==='opportunities') {
    body=disclosure('saved-opportunity-views','Saved views',`<div class="toolbar"><label>Saved views<select id="saved-strategy-view"><option value="">Choose a saved view</option>${data.views.map(v=>`<option value="${v.id}">${escape(v.name)}</option>`).join('')}</select></label>${sButton('save-view','Save this view')}</div>`)+`
      ${disclosure('opportunity-method','How priorities work','<p>Priority uses business importance and observed non-citation proportion. Repeated means at least two observations, not statistical confidence. Inspect sample sizes. An unreviewed mapping or incomplete crawl cannot prove a missing page.</p>')}
      <div class="table-wrap"><table class="opportunities-table"><thead><tr><th>Query / priority</th><th>Target page</th><th>Owned citation status</th><th>Citation gap evidence</th><th>Next action</th></tr></thead><tbody>${data.rows.map(r=>`<tr><td><strong>${escape(r.text)}</strong><p class="small"><span class="status ${r.importance==='High'?'priority-high':''}">${escape(r.importance)}</span> ${r.groups.map(g=>escape(g.name)).join(', ')}</p>${r.notes?disclosure('outcome-'+r.id,'Outcome note',`<p class="small">${escape(r.notes)}</p>`):''}</td>
        <td>${targetNames[r.target]}${r.page?`<p class="small">${escape(r.page.title||r.page.url)}</p><p class="small">${escape(r.page.url)} · ${r.page.status==='done'?'Saved content available':'Content unavailable'}</p>`:''}${r.target==='planned'?`<p>${escape(r.planned_title)}<br>${escape(r.planned_url)}</p>`:''}</td>
        <td>${escape(ownedStatusLabel(r))}${r.latest_evidence?`<p class="small">Evidence: ${stamp(r.latest_evidence.created)} · ${evidenceAge(r.latest_evidence.created)} ${captureLinks([r.latest_evidence.id])}</p>`:''}${r.older_evidence?`<p class="notice small">Outside this window, not counted: ${stamp(r.older_evidence.created)} · ${evidenceAge(r.older_evidence.created)} ${captureLinks([r.older_evidence.id])}</p>`:''}${r.latest_check&&!['found','absent'].includes(r.latest_check.status)?`<p class="status failed">Latest check: ${escape(r.latest_check.status)}</p>${disclosure('opportunity-failure-'+r.id,'Check details',`<p class="small">${escape(r.latest_check.reason)}. Not counted as citation loss.</p>`)}`:''}</td>
        <td>${r.gap_checks?`${r.recurring?'Repeated observation':'Single observation'}<p>${r.gap_checks} gap checks / ${r.checks} usable Overview checks</p><p class="small">${r.competitors.map(escape).join(', ')}</p><details><summary>Supporting evidence</summary>${captureLinks(r.capture_ids)}</details>`:(r.checks?`No selected-competitor gap. ${r.uncited_checks||0} of ${r.checks} Overview checks did not cite your domain. ${disclosure('uncited-'+r.id,'Supporting captures',captureLinks(r.uncited_capture_ids))}`:'Unknown — no usable Overview evidence in this window')}</td>
        <td><div class="strategy-actions">${r.page?`<button data-review-page="${r.page.id}" data-review-query="${r.id}" class="primary">Review this page</button>`:r.target==='planned'?`<button data-review-planned="${r.id}" class="primary">New-page brief</button>`:`<button data-plan="${r.id}">Choose target page</button>`}${disclosure('opportunity-actions-'+r.id,'More actions',`${r.page||r.target==='planned'?`<button data-plan="${r.id}">Edit query plan</button>`:''}${sButton('opportunity',r.saved?'Edit saved opportunity':'Save opportunity',`data-id="${r.id}"`)}${sButton('analyse-query','Analyse cited pages',`data-id="${r.id}"`)}${r.saved?`<details><summary>Original saved evidence</summary>${captureLinks(r.saved.capture_ids)}<p>${escape(r.saved.notes)} · ${escape(r.saved.effort||'Effort unspecified')}</p></details>`:''}`)}</div></td></tr>`).join('')}</tbody></table></div>`;
  } else if(view==='publishers') {
    body=`<p class="muted">Non-owned, non-competitor domains cited in at least two usable Overview captures in this window. Frequency is observed evidence, not authority, endorsement or a partnership signal.</p><div class="table-wrap"><table><thead><tr><th>Recurring domain</th><th>Evidence</th><th>Optional research notes</th></tr></thead><tbody>${data.rows.map(r=>`<tr><td><button class="query-link" data-source-domain="${escape(r.domain)}">${escape(r.domain)}</button></td><td>${r.count} captures · ${r.query_count} ${r.query_count===1?'query':'queries'}<details><summary>Inspect evidence</summary>${captureLinks(r.capture_ids)}</details></td><td>${disclosure('source-notes-'+r.domain,'Research notes',`<p>${r.shortlisted?'Saved to research shortlist':''} ${escape(r.classification)}</p><p>${escape(r.notes)}</p>${sButton('publisher','Edit research notes',`data-domain="${escape(r.domain)}"`)}`)}</td></tr>`).join('')}</tbody></table></div>`;
  } else if(view==='related') {
    body=`<p class="small muted">Adding a query does not run a search.</p>${disclosure('related-method','About these suggestions','<p>Saved Google questions and related searches, not observed AI Overview fan-out. New queries inherit the displayed source query’s groups and start with Unspecified importance.</p>')}<div class="table-wrap"><table><thead><tr><th>Suggestion</th><th>Observed source</th><th></th></tr></thead><tbody>${data.rows.map((r,i)=>`<tr><td>${escape(r.text)}<p class="small">${escape(r.type)}</p></td><td>${escape(r.source_query)}<p>${stamp(r.created)} ${captureLinks([r.capture_id])}</p>${r.observations?.length>1?disclosure('suggestion-'+i,r.observations.length+' source observations',r.observations.map(o=>`<p>${escape(o.source_query)} · ${stamp(o.created)} ${captureLinks([o.capture_id])}</p>`).join('')):''}</td><td>${state.queries.some(q=>queryKey(q.text)===queryKey(r.text))?'<span class="inline-badge">Already tracked</span>':sButton('related-add','Add to tracked queries',`data-index="${i}"`)}</td></tr>`).join('')}</tbody></table></div>`;
  } else {
    body=`<div class="toolbar"><span role="status">${data.job?.paused?'Retrieval paused':'Saved content'} · ${Object.entries(data.counts).map(([k,v])=>`${sourceStates[k]||k}: ${v}`).join(' · ')}</span>${disclosure('retrieval-controls','Retrieval controls',`${sButton('pause-sources','Pause retrieval')}${sButton('resume-sources','Resume retrieval')}${sButton('reload','Refresh status')}`)}</div>`+
      (data.job?.message?`<p class="notice">${escape(data.job.message)}</p>`:'')+
      disclosure('source-method','About source evidence','<p>Homepage-only, blocked and non-HTML sources cannot establish article characteristics. Page types describe saved HTML patterns; they do not explain why Google cited a page. Retrieval dates are separate from capture dates.</p>')+
      `<div class="table-wrap"><table><thead><tr><th>Cited page</th><th>Supporting evidence</th><th>Saved content</th><th>Action</th></tr></thead><tbody>${data.rows.map(r=>`<tr><td><a class="source-title" href="${escape(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">${escape(r.characteristics.title||new URL(r.url).hostname)}</a><p class="small">${escape(r.url)}</p>${new URL(r.url).pathname==='/'?'<span class="inline-badge">Homepage citation</span>':''}</td><td>${disclosure('source-captures-'+r.id,r.capture_ids.length+' supporting captures',captureLinks(r.capture_ids))}</td><td>${escape(sourceStates[r.status]||r.status)}<p class="small">${stamp(r.retrieved)}</p>${r.status==='blocked'||r.status==='error'?'<span class="small">Unavailable for comparison</span>':''}<p class="small">${escape(r.confirmed||r.suggested||'Unknown')} · ${r.confirmed?'confirmed':'suggested'}</p></td><td>${sButton('inspect-source',r.status==='done'?'Inspect saved content':'Inspect source details',`data-id="${r.id}"`)}${r.status==='available'?sButton('refresh-source','Retrieve page',`data-id="${r.id}"`):disclosure('source-actions-'+r.id,'More',sButton('refresh-source','Refresh / retry',`data-id="${r.id}" ${r.status==='running'?'disabled':''}`))}</td></tr>`).join('')}</tbody></table></div>`;
  }

  const exportControl=['opportunities','competitors'].includes(view)?sButton('export','Export CSV'):'';
  return header(names[view],subtitles[view])+sectionTabs+(strategyError?loadFailure(strategyError+' Previous results shown; changed filters are not applied.','data-strategy="reload"'):'')+strategyFiltersHTML(data)+body+
    (data.rows.length?'':`<p class="notice">${Object.values(strategyFilters).some(Boolean)?'No matching records. Clear filters to see saved evidence.':view==='related'?'No related questions in saved evidence.':view==='sources'?'No matching cited pages. New captures register citation URLs without fetching. Retrieve chosen pages or use Analyse cited pages for a query.':'No matching records in this window.'}</p>${sButton('clear','Clear filters')}`)+
    `<div class="toolbar strategy-pagination">${sButton('previous','Previous',strategyPage<=1?'disabled':'')}<span>${data.total?((strategyPage-1)*100+1):0}–${Math.min(strategyPage*100,data.total)} of ${data.total}</span>${sButton('next','Next',strategyPage*100>=data.total?'disabled':'')}${exportControl}</div>`;
}

function strategyDialog(title, content) {
  let el=$('#strategy-dialog'); if(!el){el=document.createElement('dialog');el.id='strategy-dialog';el.className='strategy-dialog';document.body.append(el);}
  el.setAttribute('aria-labelledby','strategy-heading');el.innerHTML=`<div class="dialog-heading"><h2 id="strategy-heading">${escape(title)}</h2><button type="button" data-close="strategy-dialog">Close</button></div>${content}`;
  el.classList.toggle('review-preparation',Boolean(el.querySelector('#review-create-form')));
  if(!el.open) el.showModal(); return el;
}
async function openPlan(qid) {
  clearTimeout(targetSearchTimer);pageSearchRequest++;
  const data=await api('/api/strategy?'+new URLSearchParams({project_id:projectId,kind:'opportunities',page:1,search:state.queries.find(q=>q.id===qid)?.text||''}));
  const row=data.rows.find(r=>r.id===qid); if(!row)throw Error('Query not found.');
  planningQuery=qid; pageChoice=row.page; pageChoicePage=1;
  const suggestions=await api(`/api/page-suggestions?project_id=${projectId}&query_id=${qid}`);
  planningSuggestions=suggestions;
  strategyDialog('Plan: '+row.text,`<form id="plan-form"><label>Confirmed primary target<select name="target">${Object.entries(targetNames).map(([k,v])=>`<option value="${k}" ${row.target===k?'selected':''}>${v}</option>`).join('')}</select></label>
    <div id="target-existing"><p class="notice">Suggestions are not confirmed until you choose a page and save. Blocked pages remain selectable, but their content is unavailable.</p>
    <h3>Suggested pages</h3><div>${suggestions.length?suggestions.map((r,i)=>`<div class="source">${sButton('choose-suggestion',r.title||r.url,`type="button" data-index="${i}"`)}<p>${escape(r.url)} · ${escape(r.status)}</p><p class="small">Heading match: ${escape(r.heading_evidence||'None')}<br>Content match: ${escape(r.content_evidence||'None')}</p></div>`).join(''):'No matching indexed content. Search the inventory below.'}</div>
    <label>Search owned pages by URL or meta title<input id="target-page-search" placeholder="Start typing a page URL or title" aria-controls="target-page-results"></label><p class="small muted">Searches as you type. You can also press Enter or click Search pages.</p>${sButton('search-pages','Search pages','type="button"')}<div id="target-page-results" aria-live="polite"></div>
    <div id="chosen-target" class="page-choice-status" role="status" aria-live="polite" tabindex="-1"></div></div>
    <div id="target-planned"><label>Planned title<input name="planned_title" maxlength="2000" value="${escape(row.planned_title)}"></label><label>Intended owned URL (optional)<input name="planned_url" maxlength="2048" value="${escape(row.planned_url)}"></label></div>
    <p id="target-missing" class="notice">Saving this choice explicitly confirms that no suitable page exists. An incomplete crawl alone is not proof.</p><label>Business importance<select name="importance">${options(['High','Medium','Low','Unspecified'],row.importance)}</select></label>
    <label>Business notes (optional)<textarea name="notes" maxlength="10000">${escape(row.notes)}</textarea></label>
    <label>Topic / product groups (optional; one per line)<textarea name="groups" rows="3">${escape(row.groups.map(g=>g.name).join('\n'))}</textarea></label>
    <button class="primary">Save query plan</button></form>`);
  updateTargetFields();
  showPageChoice();
}
function showPageChoice() {
  const status=$('#chosen-target');
  status.innerHTML=pageChoice?`<strong>Selected page</strong><div>${escape(pageChoice.title||pageChoice.url)}</div><div class="small">${escape(pageChoice.url)}</div><p>Click <strong>Save query plan</strong> below to apply your choice.</p>`:'No page selected. Choose a suggested page or search your owned pages.';
  document.querySelectorAll('#plan-form [data-strategy="choose-page"],#plan-form [data-strategy="choose-suggestion"]').forEach(button=>{
    const row=(button.dataset.strategy==='choose-suggestion'?planningSuggestions:planningRows)[Number(button.dataset.index)];
    const selected=!!pageChoice&&row?.id===pageChoice.id;
    button.setAttribute('aria-pressed',String(selected));
    button.querySelector('.page-choice-check')?.remove();
    if(selected)button.insertAdjacentHTML('beforeend','<span class="page-choice-check"> ✓ Selected</span>');
  });
}
function updateTargetFields() {
  const value=$('#plan-form [name=target]')?.value;
  for(const kind of ['existing','planned','missing']){const el=$('#target-'+kind);if(el)el.hidden=value!==kind;}
}
async function choosePages() {
  clearTimeout(targetSearchTimer);
  const box=$('#target-page-results'),input=$('#target-page-search');if(!box||!input)return;
  const serial=++pageSearchRequest,pid=projectId,qid=planningQuery,search=input.value;
  const current=()=>serial===pageSearchRequest&&pid===projectId&&qid===planningQuery&&box.isConnected&&$('#strategy-dialog')?.open&&input.value===search;
  box.setAttribute('aria-busy','true');box.innerHTML='<p role="status">Searching owned pages…</p>';
  try {
    const data=await api('/api/pages?'+new URLSearchParams({project_id:pid,search,page:pageChoicePage}));
    if(!current())return;
    planningRows=data.rows;pageChoicePage=data.page;
    box.innerHTML=`<p role="status">${data.total} matching owned page${data.total===1?'':'s'}</p>`+
      (data.rows.length?data.rows.map((r,i)=>`<div class="source">${sButton('choose-page',r.title||r.url,`type="button" data-index="${i}"`)}<p class="small">${escape(r.url)} · ${escape(r.status)}</p></div>`).join(''):'<p class="notice">No owned pages match this URL or meta-title search. Try fewer words or part of the URL.</p>')+
      `<div class="toolbar">${sButton('page-previous','Previous','type="button" '+(pageChoicePage<=1?'disabled':''))}<span>Page ${pageChoicePage} · ${data.total} URLs</span>${sButton('page-next','Next','type="button" '+(pageChoicePage>=data.pages?'disabled':''))}</div>`;
    showPageChoice();
  }catch(error){if(current())box.innerHTML=`<p class="notice error" role="alert">${escape(error.message)}</p>`;}
  finally{if(serial===pageSearchRequest&&box.isConnected)box.removeAttribute('aria-busy');}
}
async function strategySave(body){return api('/api/strategy',{project_id:projectId,...body});}

document.addEventListener('input',event=>{
  if(event.target.id!=='target-page-search'||event.isComposing)return;
  clearTimeout(targetSearchTimer);pageSearchRequest++;pageChoicePage=1;
  $('#target-page-results').innerHTML='<p role="status">Searching owned pages…</p>';
  const input=event.target;
  targetSearchTimer=setTimeout(()=>{if(input.isConnected&&$('#strategy-dialog')?.open)choosePages();},250);
});
document.addEventListener('keydown',event=>{
  if(event.target.id==='target-page-search'&&event.key==='Enter'&&!event.isComposing){event.preventDefault();pageChoicePage=1;choosePages();}
});

document.addEventListener('change',async event=>{
  try {
    if(event.target.id==='project-select'){strategyFilters={};strategyPage=1;strategyData=null;strategyKey='';strategyRequest++;}
    if(event.target.matches('[data-strategy-filter]')){strategyError='';strategyFilters[event.target.dataset.strategyFilter]=event.target.value;strategyPage=1;await loadStrategy(true);}
    if(event.target.id==='saved-strategy-view'&&event.target.value){const saved=strategyData.views.find(v=>v.id===Number(event.target.value));strategyFilters=JSON.parse(saved.filters);strategyPage=1;await loadStrategy(true);}
    if(event.target.matches('#plan-form [name=target]'))updateTargetFields();
    if(event.target.dataset.classify){await strategySave({action:'classify',id:Number(event.target.dataset.classify),type:event.target.value});toast('Page type confirmed.');await loadStrategy(true);}
  }catch(error){toast(error.message);}
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  try {
    if(button.dataset.sourceDomain){destination('sources');strategyFilters={search:button.dataset.sourceDomain};strategyPage=1;strategyKey='';strategyData=null;render();return;}
    if(button.dataset.view)return;
    if(button.dataset.plan){await openPlan(Number(button.dataset.plan));return;}
    if(button.dataset.evidence){const c=await api('/api/capture?id='+Number(button.dataset.evidence));strategyDialog('Capture #'+c.id,`<p>${stamp(c.created)} · ${escape(c.status)}</p><p>${escape(JSON.stringify(c.context))}</p><div class="summary-content">${content(c.overview?.texts,c.refs||[])}</div><h3>Original citation URLs</h3>${(c.refs||[]).map(r=>`<p><a href="${escape(safeURL(r.href))}" target="_blank" rel="noopener noreferrer">${escape(r.href)}</a>${!r.owned?`<button data-competitor="${escape(r.domain)}">Add competitor</button>`:''}</p>`).join('')}`);return;}
    if(button.dataset.competitor){await strategySave({action:'competitor',domain:button.dataset.competitor});toast('Competitor added.');if(strategyViews.includes(view))await loadStrategy(true);return;}
    const action=button.dataset.strategy;if(!action)return;
    if(action==='clear'){strategyFilters={};strategyPage=1;await loadStrategy(true);return;}
    if(action==='previous'||action==='next'){strategyPage+=action==='next'?1:-1;await loadStrategy(true);return;}
    if(action==='reload'){strategyError='';await loadStrategy(true);return;}
    if(action==='inspect-source'){
      const r=await api('/api/strategy?'+new URLSearchParams({project_id:projectId,kind:'sources',id:button.dataset.id}));
      strategyDialog('Saved cited page',snapshotPageHTML({...r,successful:r.retrieved})+`<p>Suggested type: ${escape(r.suggested||'Unknown')}</p><label>Confirm or correct page type<select data-classify="${r.id}"><option value="">Choose type</option>${options(types,r.confirmed)}</select></label>`);return;
    }
    if(action==='search-pages'){pageChoicePage=1;await choosePages();return;}
    if(action==='page-previous'||action==='page-next'){pageChoicePage+=action==='page-next'?1:-1;await choosePages();return;}
    if(action==='choose-page'||action==='choose-suggestion'){
      pageChoice=(action==='choose-suggestion'?planningSuggestions:planningRows)[Number(button.dataset.index)];
      showPageChoice();$('#chosen-target').focus({preventScroll:true});$('#chosen-target').scrollIntoView({block:'center'});return;
    }
    if(action==='export'){
      const response=await fetch('/api/export?'+strategyParams());if(!response.ok)throw Error('Export failed.');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=view+'.csv';a.click();URL.revokeObjectURL(url);return;
    }
    if(action==='save-view') {strategyDialog('Save live view',`<form id="view-form"><label>View name<input name="name" required maxlength="100"></label><p>The filters and sorting are saved; results remain live.</p><button class="primary">Save view</button></form>`);return;}
    if(action==='opportunity'){
      const row=strategyData.rows.find(r=>r.id===Number(button.dataset.id));planningQuery=row.id;
      strategyDialog('Save opportunity: '+row.text,`<form id="opportunity-form"><label>Notes<textarea name="notes" maxlength="10000">${escape(row.saved?.notes||'')}</textarea></label><label>Effort<select name="effort">${options(['','Small','Medium','Large'],row.saved?.effort||'')}</select></label><p>Original capture references are preserved when you edit this record.</p><button class="primary">Save opportunity</button></form>`);return;
    }
    if(action==='publisher'){
      const row=strategyData.rows.find(r=>r.domain===button.dataset.domain);
      strategyDialog('Cited source: '+row.domain,`<form id="publisher-form" data-domain="${escape(row.domain)}"><label><input type="checkbox" name="shortlisted" ${row.shortlisted?'checked':''}> Save to research shortlist</label><label>Your classification<input name="classification" maxlength="100" value="${escape(row.classification)}" placeholder="Trade publication, association, community…"></label><label>Notes<textarea name="notes" maxlength="10000">${escape(row.notes)}</textarea></label><button class="primary">Save research notes</button></form>`);return;
    }
    const body={action};
    if(button.dataset.id)body[action==='analyse-query'?'query_id':'id']=Number(button.dataset.id);
    if(button.dataset.domain)body.domain=button.dataset.domain;
    if(action==='related-add')Object.assign(body,strategyData.rows[Number(button.dataset.index)]);
    const result=await strategySave(body);toast(action==='related-add'?`${result.added} query added; ${result.duplicates} duplicate skipped.`:'Saved.');await refresh(false);await loadStrategy(true);
  } catch(error){toast(error.message);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!['competitor-form','plan-form','opportunity-form','view-form','publisher-form'].includes(form.id))return;
  event.preventDefault();
  const data=Object.fromEntries(new FormData(form));
  try{
    if(form.id==='competitor-form')await strategySave({action:'competitor',domain:data.domain});
    if(form.id==='plan-form')await strategySave({action:'plan',query_id:planningQuery,...data,page_id:pageChoice?.id||null,groups:data.groups.split('\n').map(x=>x.trim()).filter(Boolean)});
    if(form.id==='opportunity-form')await strategySave({action:'opportunity',query_id:planningQuery,...data,start:strategyData.start,end:strategyData.end,competitor:strategyFilters.competitor||''});
    if(form.id==='view-form')await strategySave({action:'view',name:data.name,filters:{...strategyFilters}});
    if(form.id==='publisher-form')await strategySave({action:'publisher',domain:form.dataset.domain,...data,shortlisted:form.elements.shortlisted.checked});
    if($('#strategy-dialog')?.open)$('#strategy-dialog').close();toast('Saved.');await refresh(false);if(strategyViews.includes(view))await loadStrategy(true);
  }catch(error){toast(error.message);}
});
setInterval(()=>{if(view==='sources'&&!document.querySelector('dialog[open]'))loadStrategy(true);},4000);
