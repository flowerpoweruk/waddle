/* Review generation is explicit; opening evidence never runs a model or a search. */
let improvementData=null,improvementLoadedKey='', improvementKey='', improvementSerial=0, improvementPage=1, improvementSearch='', workKind='reviews',improvementError='',reviewAnchor='';
const workMemory=new Map();
let activeReview=null, reviewProject=0, ollamaConfig=null, ollamaLoading=false, reviewPreparation=null;
let reviewPickerPage=1, reviewPickerSearch='', reviewPickerSerial=0, improvementTimer;
let reviewSources=new Set(),showHistoricalAssignments=false;
const comparisonAvailability=new Map();
function reviewSelectionStatus(){const usable=[...reviewSources].filter(key=>comparisonAvailability.get(key)).length;return `${reviewSources.size} selected · ${usable} with saved editorial content${usable<reviewSources.size?' · unavailable selections need review':''}`;}
const improvementButton=(action,label,extra='')=>{
  if(action==='evidence')label=label==='owned'?'Saved owned page':label.replace(/^(query|capture|link):/i,(_,kind)=>kind[0].toUpperCase()+kind.slice(1)+' #');
  return `<button data-improvement="${action}" ${extra}>${escape(label)}</button>`;
};
const improvementParams=()=>new URLSearchParams({project_id:projectId,kind:workKind,page:improvementPage,search:improvementSearch});

function taskOutcome(outcome){
  if(!outcome)return '<p class="small muted">After deployment, record its date to compare saved citation observations. Marking a task Done does not mean it was deployed.</p>';
  const sample=s=>`${s.cited} cited / ${s.usable} usable checks (${s.usable?Math.round(s.cited/s.usable*100)+'%':'no evidence'}); ${s.overviews} with an Overview`;
  return `<section><h3>Citation follow-up · ${outcome.days} complete UTC days per window</h3><p class="small muted">${escape(outcome.limitation)}</p>${outcome.days?outcome.queries.map(q=>`<h4>${escape(q.query)} · ${escape(q.target)}</h4><p>Before (${escape(q.before.start)}–${escape(q.before.end)}): ${sample(q.before)}</p><p>After (${escape(q.after.start)}–${escape(q.after.end)}): ${sample(q.after)}</p>`).join(''):'<p>Awaiting a complete day after deployment. Run comparable query checks manually; this screen starts no searches.</p>'}</section>`;
}

function ollamaSettingsView(){
  return `<section class="inclusion" id="ollama-settings"><h2>Local AI — Ollama</h2><p>Review captured AI Overview evidence with your installed local model when you explicitly create a review. No cloud model, API key or model download is used.</p>
    <p>Saved model: <strong>${escape(ollamaConfig?.model||'None selected')}</strong>. Saved settings do not prove connectivity.</p><p class="small">Reported active context: ${ollamaConfig?.active_context?Number(ollamaConfig.active_context).toLocaleString('en-GB')+' tokens':'Unknown — test the connection to inspect'}. Advertised maximum: ${ollamaConfig?.model_context?Number(ollamaConfig.model_context).toLocaleString('en-GB')+' tokens':'Unknown'}.</p><details><summary>Configure and test local AI</summary><form id="ollama-form"><label>Ollama connection address<input name="address" id="ollama-address" value="${escape(ollamaConfig?.address||'http://127.0.0.1:11434')}" required></label>
    <div class="toolbar">${improvementButton('test-ollama','Test connection / Refresh models')}<span id="ollama-message" role="status">${escape(ollamaConfig?.message||'Test the connection to list your installed local text models.')}</span></div>
    <p class="small muted">Active context: ${ollamaConfig?.active_context?Number(ollamaConfig.active_context).toLocaleString('en-GB')+' tokens':'not reported — load/test the selected model to inspect'}. Advertised maximum: ${ollamaConfig?.model_context?Number(ollamaConfig.model_context).toLocaleString('en-GB')+' tokens':'not reported'}. Page reviews request up to 8,192 tokens per call and split larger inputs. This does not change Ollama’s global settings.</p><label>Installed local model<select name="model" id="ollama-model"><option value="">Choose a local model</option>${[...new Set([...(ollamaConfig?.models||[]),ollamaConfig?.model].filter(Boolean))].map(m=>`<option ${m===ollamaConfig?.model?'selected':''}>${escape(m)}</option>`).join('')}</select></label><button type="submit" class="primary">Save Ollama settings</button></form></details></section>`;
}
async function loadOllamaSettings(){
  if(ollamaConfig||ollamaLoading)return;ollamaLoading=true;
  try{ollamaConfig=await api('/api/ollama');if(view==='settings'&&!document.activeElement?.closest('#ollama-form'))$('#ollama-settings').outerHTML=ollamaSettingsView();}
  catch(e){toast(e.message);}finally{ollamaLoading=false;}
}
async function loadImprovements(force=false){
  if(!projectId||!['improvements','work'].includes(view)||activeReview&&reviewProject===projectId)return;
  const key=improvementParams().toString();if(!force&&(key===improvementKey||improvementError))return;improvementError='';
  improvementKey=key;const serial=++improvementSerial;
  try{const result=await api('/api/improvements?'+key);if(serial!==improvementSerial||key!==improvementParams().toString())return;improvementData=result;improvementLoadedKey=key;improvementError='';render();}
  catch(e){if(serial===improvementSerial){improvementKey='';improvementError=e.message;render();}}
}
function workTabs(){return tabs(['reviews','briefs','tasks'].map(kind=>[kind[0].toUpperCase()+kind.slice(1),`data-improvement="${kind}"`,workKind===kind]));}
function taskDetails(t){
  const values={owner:t.owner,status:t.status,due:t.due,deployed:t.deployed||'',deployment_note:t.deployment_note||''};
  return `<div class="task-details"><pre class="page-content">${escape(t.details)}</pre><form data-task-form="${t.id}" data-original="${escape(JSON.stringify(values))}" class="filters"><label>Owner<input name="owner" maxlength="150" value="${escape(t.owner)}"></label><label>Status<select name="status">${['To do','In progress','Blocked','Done'].map(s=>`<option ${s===t.status?'selected':''}>${s}</option>`).join('')}</select></label><label>Due date<input name="due" type="date" value="${escape(t.due)}"></label><label>Deployed on (UTC)<input name="deployed" type="date" value="${escape(t.deployed||'')}"></label><label>What actually changed<input name="deployment_note" maxlength="4000" value="${escape(t.deployment_note||'')}"></label><button>Save task</button></form>${disclosure('task-followup-'+t.id,'Citation follow-up',taskOutcome(t.outcome))}${improvementButton('open','Open supporting review',`data-id="${t.review_id}"`)}</div>`;
}
function improvementsView(){
  const name='Page improvements';
  if(!projectId)return header(name,'Choose a project to begin.',newProjectButton());
  if(activeReview&&reviewProject===projectId)return reviewView(activeReview);
  if(reviewProject!==projectId)activeReview=null;
  if(!improvementError)queueMicrotask(()=>loadImprovements());
  const top=header(name,'Review evidence, decide what to change and organise accepted work.',improvementButton('choose-page','Review your page','class="primary"'))+workTabs();
  const d=improvementData;
  if(improvementError&&!d)return top+loadFailure(improvementError,'data-improvement="reload"');
  if(!d||(!improvementError&&improvementLoadedKey!==improvementParams().toString()))return top+'<p role="status">Loading saved work…</p>';
  const taskView=workKind==='tasks',briefView=workKind==='briefs';
  const search=`<label>Search ${workKind}<input type="search" id="improvement-search" value="${escape(improvementSearch)}" placeholder="Search title"></label>`;
  const empty=improvementSearch?`<div class="empty"><h2>No matching ${workKind}</h2>${improvementButton('clear-search','Clear search')}</div>`:`<div class="empty"><h2>No ${workKind} yet</h2><p>${taskView?'Open a review and create a task from an accepted recommendation.':briefView?'Open a review, accept supported changes and create a brief.':'Choose a page and its search evidence to begin.'}</p>${improvementButton(taskView||briefView?'reviews':'choose-page',taskView||briefView?'Open reviews':'Review your page','class="primary"')}</div>`;
  const body=taskView?d.rows.map(t=>disclosure('task-'+t.id,`<span class="task-summary"><strong>${escape(t.title)}</strong><span>${escape(t.status)}</span><span class="small">Due ${escape(t.due||'not set')} · ${t.deployed?'Deployed '+escape(t.deployed):'Not deployed'}</span></span>`,taskDetails(t),false,'review-card')).join(''):
    `<div class="table-wrap"><table><thead><tr><th>${briefView?'Saved brief':'Page review'}</th><th>${briefView?'Saved versions':'Review / decisions'}</th><th>${briefView?'Updated':'Outputs'}</th><th>Action</th></tr></thead><tbody>${d.rows.map(r=>`<tr><td><strong>${escape(r.title)}</strong><small class="review-url">${stamp(r.created)}</small></td><td>${briefView?`${r.brief_versions} saved versions`:`${escape(r.status)} · ${r.assignments?`${r.decided} of ${r.assignments} saved assignments reviewed`:'No assignments'}`}${r.error?disclosure('review-error-'+r.id,'Failure details',`<p class="notice error">${escape(r.error)}</p>`):''}</td><td>${briefView?stamp(r.brief_updated):`${r.brief_versions?'Brief saved':'No brief'} · ${r.task_count} tasks`}</td><td>${improvementButton('open',briefView?'Open brief':'Open review',`data-id="${r.id}" ${briefView?'data-brief="true"':''}`)}</td></tr>`).join('')}</tbody></table></div>`;
  return top+(improvementError?loadFailure(improvementError+' Previous results shown; changed filters are not applied.','data-improvement="reload"'):'')+search+(d.rows.length?body:empty)+`<div class="section-heading">${improvementButton('previous','Previous',improvementPage<=1?'disabled':'')}<span>${d.total} ${d.total===1?workKind.slice(0,-1):workKind} · Page ${improvementPage}</span>${improvementButton('next','Next',improvementPage*100>=d.total?'disabled':'')}</div>`;
}

async function pollImprovements(){
  if(document.querySelector('dialog[open]')||document.activeElement?.matches('input,textarea,select'))return;
  if(activeReview&&reviewProject===projectId){
    if(!['queued','running','refreshing'].includes(activeReview.status))return;
    const id=activeReview.id,pid=projectId;
    const latest=await api(`/api/improvements?project_id=${pid}&id=${id}`);
    if(pid!==projectId||activeReview?.id!==id)return;
    if(latest.status!==activeReview.status||JSON.stringify(latest.execution)!==JSON.stringify(activeReview.execution)){activeReview=latest;render();if(latest.status==='failed')toast('AI review failed: '+latest.error);}
  }else if(improvementData?.rows.some(r=>['queued','running','refreshing'].includes(r.status))&&!document.activeElement?.matches('input,select,textarea'))await loadImprovements(true);
}
async function openReview(id,brief=false){
  showHistoricalAssignments=false;
  const serial=++routeSerial,pid=projectId,result=await api(`/api/improvements?project_id=${pid}&id=${id}`);if(pid!==projectId||serial!==routeSerial)return;
  const same=activeReview?.id===id;if(!same)routeReturn=routeHash();
  activeReview=result;reviewProject=pid;view='improvements';reviewAnchor=brief?'brief':'';
  if(!same)commitRoute(true);render();if(!same){window.scrollTo(0,0);$('#main').focus({preventScroll:true});}
  if(brief)$('#content-brief')?.scrollIntoView({block:'start'});
}

function reviewView(r){
  const recommendations=r.recommendations.filter(rec=>showHistoricalAssignments||!(rec.origin==='invalidated'||['Internal links','Conversion'].includes(rec.category)||(rec.origin==='guided'&&/^No (meta title|meta description|H1 heading) observed$/.test(rec.title))));
  const historical=r.recommendations.length-recommendations.length;
  const s=r.snapshot,p=s.page,decided=recommendations.filter(x=>["Accepted","Rejected","Corrected"].includes(x.decision)).length;
  return header(r.title,p?p.url:'New-page brief — no existing owned page was reviewed',improvementButton('back','Back to '+(routeReturn?'previous view':'reviews')))+
    `<div class="review-status ${r.status==='failed'?'notice error':r.status==='completed'?'notice success':['queued','running','refreshing'].includes(r.status)?'notice info':'notice'}" role="${r.status==='failed'?'alert':'status'}"><strong>${r.status==='completed'?'Generation completed':'AI review: '+escape(r.status)}</strong><p>${escape(r.error||(['queued','running','refreshing'].includes(r.status)?'Work is queued or running locally. Evidence refresh finishes before generation; only one generation runs at a time.':'AI editorial suggestions require your review.'))}</p><span class="small">${r.finished?'Finished '+stamp(r.finished):'Created '+stamp(r.created)}</span></div>`+
    `<div class="toolbar">${['queued','running','refreshing'].includes(r.status)?improvementButton('cancel','Cancel review',`data-id="${r.id}"`):''}${disclosure('new-review-draft','Create a new review draft',improvementButton('regenerate','Prepare a new review',`data-id="${r.id}"`))}${disclosure('review-provenance-'+r.id,'Model & snapshot',`<p class="small">Model: ${escape(r.model)}</p>${improvementButton('snapshot','Inspect saved snapshot',`data-id="${r.id}"`)}`)}</div>`+
    `
    ${evidenceTrail([['Tracked queries',`<strong>${s.queries.length} selected</strong>`],['Saved evidence',`<strong>${s.captures.length} usable Overview captures</strong><small>${escape(s.start)} to ${escape(s.end)}</small>`],['Target page',p?improvementButton('evidence','Inspect owned page','data-key="owned"'):'<strong>Planned page</strong>'],['Decisions',`<strong>${recommendations.length?`${decided} of ${recommendations.length} reviewed`:'No current assignments'}</strong>`]])}
    ${!['full_saved_content','adaptive_saved_content'].includes(s.input_policy)||s.review_policy!=='overview_grounded'?`<div class="notice"><strong>Legacy evidence: ${!['full_saved_content','adaptive_saved_content'].includes(s.input_policy)?'partial excerpts; ':''}supporting quotations require review.</strong>${disclosure('legacy-'+r.id,'Historical input details','<p>Earlier review: partial excerpts or older grounding rules may apply. Original records are preserved. Prepare a new draft for current evidence rules.</p>')}</div>`:''}
    ${r.execution?.stage?`<section class="review-card" role="status"><strong>${escape(r.execution.stage)}</strong><p>${r.execution.completed||0} of ${r.execution.total||0} scheduled stages complete · ${escape(r.execution.mode)} · ${r.execution.context.toLocaleString('en-GB')} context tokens</p><p class="small">Estimated input: ${r.execution.estimated_input||0} · ${escape(r.execution.estimation||'')} · ${r.execution.coverage?.length||0} sections read. ${r.status==='completed'?'All scheduled stages finished.':'More stages are scheduled as evidence is read.'}</p></section>`:''}
    ${r.readiness?.errors?.length?`<p class="notice error">${r.readiness.errors.map(escape).join('<br>')}</p>`:''}
    ${r.readiness?.unavailable_sources?.map(x=>`<p class="notice">Unavailable comparison: ${escape(x.url)} · ${escape(x.status)} · ${escape(x.reason)}</p>`).join('')||''}
    ${['failed','cancelled'].includes(r.status)?`<div class="toolbar">${improvementButton('refresh-review','Refresh evidence and create replacement review',`data-id="${r.id}"`)}${r.readiness?.ready?improvementButton('resume','Resume saved evidence',`data-id="${r.id}"`):''}</div>`:''}
    ${r.predecessor?`<p>Replacement for ${improvementButton('open','review #'+r.predecessor,`data-id="${r.predecessor}"`)}</p>`:''}
    ${(r.replacements||[]).map(id=>`<p>${improvementButton('open','Open replacement review #'+id,`data-id="${id}"`)}</p>`).join('')}
    ${r.assessment?`<section class="review-card"><h2>Evidence assessment</h2><p>${escape(r.assessment)}</p></section>`:''}${historical?`<p class="small muted">${historical} historical assignments preserved. ${improvementButton('historical-recommendations','Inspect historical assignments')}</p>`:''}<div class="brief-shortcut toolbar"><strong>${decided} of ${recommendations.length} reviewed · ${recommendations.filter(x=>['Accepted','Corrected'].includes(x.decision)).length} accepted</strong><button data-improvement="brief-anchor">${r.brief?'Open brief':'Create brief'}</button><button data-improvement="tasks">Open tasks</button></div><div class="section-heading"><h2>Recommendations</h2><span class="small muted">Review each suggestion before creating work</span></div>${r.status!=='completed'?'<p>Guided observations may be available below. These do not mean the AI review completed successfully.</p>':''}
    ${recommendations.map(rec=>`<section class="review-card"><p class="small">${escape(rec.category)} · ${escape(rec.label)} · ${escape(rec.origin)} · ${escape(rec.decision)}</p><h3>${escape(rec.title)}</h3><p><strong>Proposed change:</strong> ${escape(rec.change)}</p><p>${escape(rec.rationale)}</p>${Object.entries(rec.grounding||{}).map(([id,quote])=>`<blockquote><p>${escape(quote)}</p><small>${escape(id)} · quotation verified against saved input; relevance requires your review</small></blockquote>`).join('')}<div class="toolbar">${rec.evidence_ids.map(key=>improvementButton('evidence',key,`data-key="${escape(key)}"`)).join('')}</div>
    <form data-recommendation-form="${rec.id}"><div class="toolbar"><button name="decision" value="Accepted" class="${rec.decision==='Accepted'?'selected-decision':''}" aria-pressed="${rec.decision==='Accepted'}">${rec.decision==='Accepted'?'Accepted':'Accept'}</button><button name="decision" value="Rejected" aria-pressed="${rec.decision==='Rejected'}">${rec.decision==='Rejected'?'Rejected':'Reject'}</button></div>${disclosure('correction-'+r.id+'-'+rec.id,rec.correction?'Edit corrected assignment':'Correct assignment',`<label>Your corrected assignment<textarea name="correction" rows="3">${escape(rec.correction)}</textarea></label><button name="decision" value="Corrected">Save correction</button>`,rec.decision==='Corrected')}<div class="toolbar">${['Accepted','Corrected'].includes(rec.decision)?improvementButton('task-create','Create '+(rec.category==='Technical'&&rec.origin==='guided'?'technical':'writing')+' task',`data-rec="${rec.id}"`):''}</div></form></section>`).join('')||(r.status==='completed'?'<p>No supported editorial change was recommended. Read the evidence assessment; this does not guarantee citations.</p>':'<p>No recommendations available yet.</p>')}
    ${disclosure("review-evidence-"+r.id,"Supporting evidence · "+s.captures.length+" usable Overview captures",`<section class="review-card"><h2>Supporting evidence</h2><p>Snapshot saved ${stamp(s.saved)} · Window ${escape(s.start)} to ${escape(s.end)} · ${s.captures.length} usable Overview captures.</p>${p?`<p>Owned content last retrieved: ${stamp(p.successful)} · Latest crawl status: ${escape(p.status)}${p.reason?' — '+escape(p.reason):''}</p>`:''}
    <p class="notice">${s.evidence_policy==='editorial_content_v1'?'Saved editorial sections are processed in one request or bounded independent passes. Waddle requests 8,192 context tokens (subject to model capacity), reserves response space and preserves verified stage results. Missing or blocked content cannot prove a gap.':s.input_policy==='full_saved_content'?'All available saved page content is supplied to Ollama together, without excerpts or splitting. Ollama uses its configured model settings; if the context is insufficient, the review fails instead of silently shortening the input.':'This earlier review used partial excerpts. Create a new review draft to analyse all available saved content; the original review and evidence remain unchanged.'} HTML only; no JavaScript. Missing, blocked or crawler-truncated content cannot prove a gap.</p>
    <p class="small muted">${escape(s.limitations)}</p>${s.queries.map(q=>{const cs=s.captures.filter(c=>c.query_id===q.id),last=cs.at(-1);const own=last?.refs.filter(x=>x.owned)||[];const equivalents=p?[p.url,p.final_url,p.result.canonical].filter(Boolean):[];const cited=own.some(x=>equivalents.includes(x.href));return `<div><strong>${escape(q.text)}</strong><p>${!last?'No usable Overview evidence in this window':!own.length?escape(s.project.domain)+' not cited':escape(s.project.domain)+(p?(cited?' cited — your target page cited':' cited — your target page not cited'):' cited — no existing target page selected')}</p>${q.latest_check&&q.latest_check.status!=='found'?`<p class="notice">Latest check: ${escape(q.latest_check.status)} · ${escape(q.latest_check.reason)}. Failed/incomplete checks are not citation loss.</p>`:''}</div>`;}).join('')}
    <div class="toolbar">${s.captures.map(c=>improvementButton('evidence','Capture #'+c.id,`data-key="capture:${c.id}"`)).join('')}${s.sources.map(x=>improvementButton('evidence',x.result.title||x.url,`data-key="source:${escape(x.key)}"`)).join('')}</div></section>`)}
    <section id="content-brief" class="review-card"><h2>Content brief</h2><p>Create a draft from accepted assignments. Each save adds a version; previous edits are preserved.</p>${improvementButton('draft',r.brief?'Create another brief version from accepted assignments':'Create brief from accepted assignments',`data-id="${r.id}" ${r.status==='completed'?'':'disabled'}`)}
    ${r.brief?`<form id="brief-form"><label>Editable brief<textarea name="content" rows="22">${escape(r.brief)}</textarea></label><div class="toolbar"><button class="primary">Save brief version</button>${improvementButton('copy-brief','Copy current text')}<a href="/api/brief-export?project_id=${projectId}&id=${r.id}">Download saved Markdown</a></div></form><label>Saved versions<select id="brief-version"><option value="">Select a version to inspect</option>${r.versions.map(v=>`<option value="${v.id}">#${v.id} · ${stamp(v.created)}</option>`).join('')}</select></label>`:''}</section>`;
}
async function showReviewPicker(){
  strategyDialog('Select your page',`<label>Search your URL or meta title<input id="review-page-search" type="search" value="${escape(reviewPickerSearch)}"></label><div id="review-picker-results" role="status">Loading…</div>`);
  await loadReviewPicker();
}
async function loadReviewPicker(){
  const serial=++reviewPickerSerial,pid=projectId;
  const data=await api('/api/pages?'+new URLSearchParams({project_id:pid,search:reviewPickerSearch,page:reviewPickerPage}));
  if(serial!==reviewPickerSerial||pid!==projectId||!$('#review-picker-results'))return;
  $('#review-picker-results').innerHTML=`<p>${data.total} matching URLs · page ${data.page} of ${data.pages}</p>${data.rows.map(p=>`<p><button data-review-page="${p.id}">${escape(p.title||p.url)}</button><small class="review-url">${escape(p.url)} · ${escape(p.status)}</small></p>`).join('')||'<p>No matching pages.</p>'}<div class="toolbar">${improvementButton('picker-previous','Previous',reviewPickerPage<=1?'disabled':'')}${improvementButton('picker-next','Next',reviewPickerPage>=data.pages?'disabled':'')}</div>`;
}
async function prepareReview(pageId,queryIds){
  const pid=projectId;
  const params=new URLSearchParams({project_id:pid,kind:'prepare',page_id:pageId});if(queryIds)params.set('query_ids',queryIds.join(','));
  const data=await api('/api/improvements?'+params);if(pid!==projectId)return;
  reviewPreparation={...data,project_id:pid,page_id:pageId};
  strategyDialog(pageId?'Review your page':'New-page brief',`<form id="review-create-form"><p>${escape(data.page?.url||'Planned page — no existing content is reviewed.')}</p><div class="review-columns"><div class="review-options"><fieldset id="review-queries"><legend>Tracked queries <span id="review-query-count">${data.queries.length} selected</span></legend><label class="small">Find a query<input type="search" id="review-query-search" placeholder="Filter the query list"></label><div class="query-choices">${state.queries.filter(q=>pageId||data.queries.some(x=>x.id===q.id)).map(q=>`<label><input type="checkbox" value="${q.id}" ${data.queries.some(x=>x.id===q.id)?'checked':''}> <span>${escape(q.text)}</span></label>`).join('')}</div></fieldset><p class="small muted">Confirmed queries for this page are selected. Choose the searches this review should use.</p><div class="form-row"><label>Evidence from (UTC)<input id="review-start" type="date" value="${data.start}"></label><label>Evidence to (UTC)<input id="review-end" type="date" value="${data.end}"></label></div>${improvementButton('refresh-evidence','Update comparison choices')}</div><div id="review-comparisons"></div></div>${disclosure('review-method','What this review uses','<p>All selected saved editorial sections are read. Small reviews use one request; larger reviews use bounded independent passes with verified quotations. Owned editorial content and a usable capture for each query are required. Retrieval uses the existing five-fetch queue. Google checks do not run.</p>')}<div class="dialog-actions"><span class="small">Runs saved model: ${escape(data.model||ollamaConfig?.model||'see Settings')}</span><div class="toolbar">${improvementButton('retrieve','Retrieve selected pages')}<button type="submit" class="primary">Create review — run local AI</button></div></div></form>`);
  renderReviewComparisons(data);
}
function renderReviewComparisons(data,reset=true){
  if(reset){reviewSources=new Set(data.sources.map(s=>s.key));comparisonAvailability.clear();}for(const s of [...data.sources,...data.candidates])comparisonAvailability.set(s.key,s.status==='done'&&Boolean(s.result.review_content?.content));
  $('#review-comparisons').innerHTML=`${data.readiness?.errors?.length?`<p class="notice error">${data.readiness.errors.map(escape).join('<br>')}</p>`:''}<h3>Choose up to three cited pages</h3><p>${data.captures.length} usable Overview captures. Choose no pages if comparison content is unavailable.</p><p id="review-source-count" role="status">${reviewSelectionStatus()}</p><p class="small">Unavailable selected pages are not usable comparison content. Inspect each selection before running.</p>${data.candidates.map(s=>`<div class="review-source"><label><input type="checkbox" name="source" aria-label="${escape(s.url)}" value="${escape(s.key)}" ${reviewSources.has(s.key)?'checked':''}> <span class="source-choice-title">${escape(s.result.title||s.url)}</span><small>${escape(s.url)} · ${escape(s.status)} · ${s.status==='done'&&s.result.review_content?.content?'Editorial content ready':'Editorial content unavailable — retrieve this page'} · ${stamp(s.retrieved)} · ${s.frequency} captures${s.reason?' · '+escape(s.reason):''}</small></label><details><summary>Inspect comparison</summary><p>${escape(s.result.title||s.url)}</p><p>${escape(s.url)} · ${escape(s.status)}</p><pre class="page-content">${escape(s.result.review_content?.content||s.result.content||'Content unavailable. No gap inferred.')}</pre></details></div>`).join('')||'<p>No third-party citation URLs in the selected evidence.</p>'}<div class="toolbar">${improvementButton('candidate-previous','Previous cited pages',data.candidate_page<=1?'disabled':'')}<span>${data.candidate_total} cited pages · page ${data.candidate_page} · 100 per page</span>${improvementButton('candidate-next','Next cited pages',data.candidate_page*100>=data.candidate_total?'disabled':'')}</div>`;
}
function reviewFormBody(){
  document.querySelectorAll('#review-comparisons input').forEach(i=>{if(i.checked)reviewSources.add(i.value);else reviewSources.delete(i.value);});
  return {project_id:reviewPreparation.project_id,page_id:reviewPreparation.page_id,
    query_ids:[...document.querySelectorAll('#review-queries input[type=checkbox]:checked')].map(o=>Number(o.value)),start:$('#review-start').value,end:$('#review-end').value,
    source_ids:[...reviewSources]};
}
async function refreshReviewEvidence(candidatePage=1,preserve=false){
  const b=reviewFormBody(),pid=projectId;
  const d=await api('/api/improvements?'+new URLSearchParams({project_id:b.project_id,kind:'prepare',page_id:b.page_id,query_ids:b.query_ids.join(','),start:b.start,end:b.end,candidate_page:candidatePage}));
  if(pid!==projectId||!$('#review-create-form'))return;reviewPreparation={...d,project_id:pid,page_id:b.page_id};renderReviewComparisons(d,!preserve);
}
function inspectReviewEvidence(key){
  const s=activeReview.snapshot;
  let value;
  if(key==='owned')value=s.page;
  else if(key.startsWith('source:'))value=s.sources.find(x=>'source:'+x.key===key);
  else if(key.startsWith('capture:'))value=s.captures.find(x=>'capture:'+x.id===key);
  else if(key.startsWith('query:'))value=s.queries.find(x=>'query:'+x.id===key);
  else if(key.startsWith('link:'))value=s.internal_links.find(x=>'link:'+x.id===key);
  let body;
  if(key==='owned'||key.startsWith('source:'))body=snapshotPageHTML(value);
  else if(key.startsWith('capture:')&&value){const overview=typeof value.overview==='string'?JSON.parse(value.overview):value.overview;body=`<p>${stamp(value.created)} · ${escape(value.status)}</p><div class="summary-content">${content(overview?.texts,value.refs)}</div><h3>Original citation URLs</h3>${value.refs.map(r=>`<p>${escape(r.href)}</p>`).join('')}`;}
  else body=`<pre class="page-content">${escape(JSON.stringify(value||'Evidence unavailable',null,2))}</pre>`;
  strategyDialog('Saved evidence: '+key,`<p>This is the immutable evidence used by this review, not a fresh fetch.</p>${body}`);
}
function snapshotPageHTML(page){
  if(!page)return '<p>No existing page evidence was available.</p>';
  const r=page.result||{};
  return `<h3>${escape(r.title||page.title||page.url)}</h3><p>${escape(page.url)}</p><p>${escape(page.status)} · Content retrieved ${stamp(page.successful||page.retrieved)} · HTTP ${escape(page.http_status||'unknown')}</p>${page.reason?`<p class="notice">${escape(page.reason)}</p>`:''}<dl><dt>Meta description</dt><dd>${escape(r.description||'Not observed')}</dd><dt>Canonical</dt><dd>${escape(r.canonical||'Not observed')}</dd><dt>Robots / snippets</dt><dd>${escape(r.robots||'No directives observed')}</dd><dt>Word count</dt><dd>${escape(r.word_count??'Unknown')}</dd><dt>Observable author</dt><dd>${escape(r.author||'Not observed')}</dd><dt>Observable date</dt><dd>${escape(r.date||'Not observed')}</dd></dl>${disclosure('snapshot-findings','Observed findings and structured data',`<ul>${(r.findings||[]).map(f=>`<li>${escape(f)}</li>`).join('')}</ul><pre class="page-content">${escape(JSON.stringify(r.structured_data||[],null,2))}</pre>`)}<h3>Headings</h3><ul>${(r.headings||r.h1?.map(text=>({text}))||[]).map(h=>`<li>${escape(h.text)}</li>`).join('')}</ul><h3>Saved readable content${r.content_truncated?' (truncated)':''}</h3><pre class="page-content">${escape(r.content||'Unavailable — no content gap inferred.')}</pre>`;
}
function unsavedReviewBrief(exceptForm=null){
  if([...document.querySelectorAll('[data-task-form]')].some(f=>f!==exceptForm&&f.dataset.original&&JSON.stringify(Object.fromEntries(new FormData(f)))!==f.dataset.original))return true;
  if(!activeReview)return false;
  if(exceptForm?.id!=='brief-form'&&$('#brief-form textarea')&&$('#brief-form textarea').value!==activeReview.brief)return true;
  return [...document.querySelectorAll('[data-recommendation-form]')].some(f=>f!==exceptForm&&f.elements.correction.value!==(activeReview.recommendations.find(r=>r.id===Number(f.dataset.recommendationForm))?.correction||''));
}
window.addEventListener('beforeunload',event=>{if(unsavedReviewBrief()){event.preventDefault();event.returnValue='';}});
document.addEventListener('click',event=>{
  const b=event.target.closest('button');
  if(b&&unsavedReviewBrief(b.value==='Corrected'?b.closest('form'):null)&&(b.dataset.view||b.dataset.action==='new-project'||b.name==='decision'||['back','open','reviews','briefs','tasks','draft','retry','cancel','task-create','regenerate'].includes(b.dataset.improvement))&&!confirm('You have unsaved review edits. Leave them without saving?')){event.preventDefault();event.stopImmediatePropagation();}
},true);
document.addEventListener('change',event=>{
  if(event.target.id==='project-select'&&unsavedReviewBrief()&&!confirm('You have unsaved review edits. Change project without saving?')){
    event.target.value=String(projectId);event.stopImmediatePropagation();
  }
},true);
document.addEventListener('click',async event=>{
  const b=event.target.closest('button');if(!b)return;
  const action=b.dataset.improvement;
  if(!action&&!b.dataset.reviewPage&&!b.dataset.reviewPlanned)return;
  if(action&&b.closest('form')&&b.type!=='button'){event.preventDefault();}
  b.disabled=true;
  try{
    if(b.dataset.reviewPage){$('#page-dialog')?.close();await prepareReview(Number(b.dataset.reviewPage),b.dataset.reviewQuery?[Number(b.dataset.reviewQuery)]:undefined);}
    else if(b.dataset.reviewPlanned)await prepareReview(0,[Number(b.dataset.reviewPlanned)]);
    else if(action==='historical-recommendations'){showHistoricalAssignments=true;render();}
    else if(action==='test-ollama'){
      const address=$('#ollama-address').value,model=$('#ollama-model').value;
      await api('/api/ollama',{address,model});
      const result=await api('/api/ollama',{action:'test'});ollamaConfig=result;
      $('#ollama-settings').outerHTML=ollamaSettingsView();
      $('#ollama-message').textContent=`Ollama ${result.version}. ${result.message}`;
    }else if(action==='choose-page'){reviewPickerPage=1;reviewPickerSearch='';await showReviewPicker();}
    else if(action==='picker-previous'||action==='picker-next'){reviewPickerPage+=action==='picker-next'?1:-1;await loadReviewPicker();}
    else if(action==='open')await openReview(Number(b.dataset.id),Boolean(b.dataset.brief));
    else if(action==='brief-anchor')$('#content-brief')?.scrollIntoView({block:'start'});
    else if(action==='reload'){improvementError='';await loadImprovements(true);}
    else if(action==='clear-search'){improvementSearch='';improvementPage=1;await loadImprovements(true);}
    else if(action==='back'){activeReview=null;improvementKey='';await routeBack();}
    else if(['reviews','briefs','tasks'].includes(action)){workMemory.set(projectId+':'+workKind,{page:improvementPage,search:improvementSearch});const saved=workMemory.get(projectId+':'+action);activeReview=null;improvementData=null;workKind=action;improvementKey='';improvementError='';improvementPage=saved?.page||1;improvementSearch=saved?.search||'';routeReturn='';view='improvements';commitRoute(true);render();}
    else if(action==='previous'||action==='next'){improvementPage+=action==='next'?1:-1;improvementKey='';render();}
    else if(action==='refresh-evidence')await refreshReviewEvidence();
    else if(action==='candidate-previous'||action==='candidate-next')await refreshReviewEvidence(reviewPreparation.candidate_page+(action==='candidate-next'?1:-1),true);
    else if(action==='refresh-review'){const result=await api('/api/improvements',{action,project_id:projectId,id:activeReview.id});await openReview(result.id);}
    else if(action==='resume'){await api('/api/improvements',{action,project_id:projectId,id:activeReview.id});await openReview(activeReview.id);}
    else if(action==='retrieve'){await api('/api/improvements',{action:'retrieve',...reviewFormBody()});toast('Selected URLs queued. Check Cited-page analysis, then update comparison choices.');}
    else if(action==='regenerate'){await prepareReview(activeReview.snapshot.page?.id||0,activeReview.snapshot.queries.map(q=>q.id));}
    else if(action==='evidence')inspectReviewEvidence(b.dataset.key);
    else if(action==='snapshot')strategyDialog('Saved page snapshot',`<p>Snapshot ${stamp(activeReview.snapshot.saved)}. Original retrieval dates are preserved below.</p>${snapshotPageHTML(activeReview.snapshot.page)}<h3>Comparison snapshots</h3>${activeReview.snapshot.sources.map(snapshotPageHTML).join('')}<details><summary>Exact evidence sent to Ollama</summary><pre class="page-content">${escape(JSON.stringify(activeReview.snapshot.model_evidence,null,2))}</pre></details>`);
    else if(action==='copy-brief'){await navigator.clipboard.writeText($('#brief-form textarea').value);toast('Current brief text copied. Save separately to preserve edits.');}
    else if(['cancel','retry','draft','task-create'].includes(action)){
      if(action==='draft'&&activeReview.brief&&!confirm('Create another version from accepted recommendations? Your saved versions will remain available. Save any unsaved edits first.'))return;
      await api('/api/improvements',{project_id:projectId,id:activeReview.id,action,recommendation_id:Number(b.dataset.rec||0)});
      await openReview(activeReview.id);if(action==='task-create')toast('Task created. Open Page improvements → Tasks to assign it.');
    }
  }catch(e){if(action==='test-ollama'&&$('#ollama-message')){$('#ollama-message').textContent=e.message;$('#ollama-message').setAttribute('role','alert');}toast(e.message);}
  finally{if(b.isConnected)b.disabled=false;}
});
document.addEventListener('submit',async event=>{
  const f=event.target;
  if(!['ollama-form','review-create-form','brief-form'].includes(f.id)&&!f.dataset.recommendationForm&&!f.dataset.taskForm)return;
  event.preventDefault();
  if(f.id==='brief-form'&&unsavedReviewBrief(f)&&!confirm('Other recommendation edits are unsaved. Save this brief and discard those edits?'))return;
  const button=event.submitter;if(button)button.disabled=true;
  try{
    if(f.id==='ollama-form'){const body=Object.fromEntries(new FormData(f));await api('/api/ollama',body);ollamaConfig={...ollamaConfig,...body};toast('Ollama settings saved.');}
    else if(f.id==='review-create-form'){const result=await api('/api/improvements',{action:'create',...reviewFormBody()});$('#strategy-dialog').close();await openReview(result.id);}
    else if(f.id==='brief-form'){await api('/api/improvements',{action:'brief-save',project_id:projectId,id:activeReview.id,content:f.elements.content.value});await openReview(activeReview.id);toast('Brief version saved.');}
    else if(f.dataset.recommendationForm){await api('/api/improvements',{action:'decision',project_id:projectId,id:activeReview.id,recommendation_id:Number(f.dataset.recommendationForm),decision:button?.value,correction:f.elements.correction.value});await openReview(activeReview.id);}
    else if(f.dataset.taskForm){await api('/api/improvements',{action:'task-update',project_id:projectId,id:Number(f.dataset.taskForm),...Object.fromEntries(new FormData(f))});f.dataset.original=JSON.stringify(Object.fromEntries(new FormData(f)));toast('Task saved.');improvementKey='';await loadImprovements(true);}
  }catch(e){toast(e.message);}finally{if(button?.isConnected)button.disabled=false;}
});
document.addEventListener('input',event=>{
  if(event.target.id==='review-query-search'){
    const term=event.target.value.toLowerCase();document.querySelectorAll('#review-queries .query-choices label').forEach(l=>{l.hidden=!l.textContent.toLowerCase().includes(term);});
  }
  if(event.target.id==='review-page-search'){reviewPickerSearch=event.target.value;reviewPickerPage=1;clearTimeout(improvementTimer);improvementTimer=setTimeout(()=>loadReviewPicker().catch(e=>toast(e.message)),250);}
  if(event.target.id==='improvement-search'){improvementSearch=event.target.value;improvementPage=1;clearTimeout(improvementTimer);improvementTimer=setTimeout(()=>{improvementKey='';loadImprovements().then(()=>{const el=$('#improvement-search');el?.focus();});},250);}
});
document.addEventListener('change',async event=>{
  if(event.target.matches('#review-queries input[type=checkbox]'))$('#review-query-count').textContent=document.querySelectorAll('#review-queries input[type=checkbox]:checked').length+' selected';
  if(event.target.matches('#review-comparisons input')){reviewFormBody();$('#review-source-count').textContent=reviewSelectionStatus();}
  if(event.target.id==='brief-version'&&event.target.value){
    try{const r=await api(`/api/improvements?project_id=${projectId}&id=${activeReview.id}&version=${Number(event.target.value)}`);strategyDialog('Saved brief version',`<pre class="page-content">${escape(r.version_content)}</pre><p>Inspect only. Your current saved brief has not changed.</p>`);}catch(e){toast(e.message);}
  }
});
