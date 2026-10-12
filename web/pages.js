/* Search and pagination run on the server, never against a full browser-side inventory. */
let pageData=null,pageProject=-1,pageRequest=0,pageLoading=false,pageSearchTimer;
let retryMonitorProject=0,pageError='';
let pageFilters={page:1,search:'',path:'',status:'',http:'',title:'',start:'',end:'',canonical_diff:'',restricted:''};
const crawlNames={queued:'Waiting',running:'Crawling',done:'Crawled',blocked:'Blocked',error:'Unavailable',unsupported:'Not HTML'};
async function loadPages(){
  const request=++pageRequest,pid=projectId;pageLoading=true;pageError='';
  try{
    const data=await api('/api/pages?'+new URLSearchParams({...pageFilters,project_id:pid}));
    if(request!==pageRequest||pid!==projectId)return;
    pageData=data;pageProject=pid;pageFilters.page=data.page;
    if(data.retry?.status==='running')retryMonitorProject=pid;
    if(view==='pages'&&!document.querySelector('dialog[open]')){
      const active=document.activeElement,key=active?.dataset.pageFilter,pos=active?.selectionStart;
      render();if(key){const next=document.querySelector(`[data-page-filter="${key}"]`);next?.focus();if(next?.type==='search'&&pos!==null)next.setSelectionRange(pos,pos);}
    }
    if(data.retry?.status==='complete'&&(view==='pages'||retryMonitorProject===pid)&&!document.querySelector('dialog[open]')&&localStorage.getItem(`crawl-retry-dismissed-${pid}`)!==String(data.retry.id))showRetry();
  }catch(error){if(request===pageRequest&&pid===projectId){pageError=error.message;if(view==='pages')render();}}finally{if(request===pageRequest)pageLoading=false;}
}
function pagesView(){
  if(!projectId)return header('Website Pages','Choose a project to crawl its tracked domain.',newProjectButton());
  if(pageError&&(pageProject!==projectId||!pageData))return header('Website pages','Page inventory unavailable.')+loadFailure(pageError,'data-page-reload');
  if(pageProject!==projectId||!pageData){if(!pageLoading)queueMicrotask(loadPages);return header('Website Pages',`Pages for ${project()?.domain||''}`)+'<p role="status">Loading page inventory…</p>';}
  const d=pageData,job=d.job,running=job?.status==='running',flight=d.counts.running||0,total=Object.values(d.counts).reduce((a,b)=>a+b,0),inc=d.inclusion;
  const input=(key,label,type='search',placeholder='')=>`<label>${label}<input type="${type}" data-page-filter="${key}" value="${escape(pageFilters[key])}" placeholder="${escape(placeholder)}"></label>`;
  const select=(key,label,options)=>`<label>${label}<select aria-label="${label}" data-page-filter="${key}"><option value="">All</option>${Object.entries(options).map(([v,t])=>`<option value="${v}" ${pageFilters[key]===v?'selected':''}>${escape(t)}</option>`).join('')}</select></label>`;
  return header('Website pages',`${project().domain} · Find the pages you want to map and review.`)+(pageError?loadFailure(pageError+' Previous inventory shown; changed filters are not applied.','data-page-reload'):'')+
    `<div class="toolbar crawl-toolbar"><button class="primary" data-page-action="${job?'resume':'start'}" ${running||flight||job?.status==='complete'?'disabled':''}>${running?'Crawl running':job?.status==='complete'?'Crawl complete':job?'Resume crawl':'Start crawl'}</button><button data-page-action="pause" ${running?'':'disabled'}>Pause crawl</button>${disclosure('crawl-actions','Maintenance & exports',`<div class="toolbar"><button data-page-action="recrawl" ${!job||running||flight||d.retry?.status==='running'?'disabled':''}>Recrawl pages</button><button data-page-action="retry_failed" ${running||flight||d.retry?.status==='running'||!((d.counts.blocked||0)+(d.counts.error||0))?'disabled':''}>Retry blocked / unavailable pages</button>${d.retry?.status==='complete'?'<button data-retry-results>View last retry results</button>':''}<button data-action="export" data-kind="pages">Export website pages</button></div>`)}</div>`+
    `<p role="status">${job?escape(job.status):'Not started'} · ${total.toLocaleString()} / 10,000 URLs in inventory (including the seeded homepage) · ${d.counts.done||0} successfully retrieved · ${d.counts.queued||0} waiting · ${flight} in flight · ${(d.counts.error||0)+(d.counts.blocked||0)+(d.counts.unsupported||0)} blocked, unavailable or non-HTML</p>`+
    (job?.message?disclosure('last-crawl-message',job.status==='failed'?'Crawl failed — details':'Last crawl / retry result',`<p class="${job.status==='failed'?'notice error':'small'}">${escape(job.message)}</p>`,job.status==='failed'):'')+(total>=10000?'<div class="notice">Inventory limit reached. Further URLs are not added.</div>':'')+
    (d.discovery_warnings.length?`<details ${job?.status==='failed'?'open':''}><summary>Sitemap discovery notices</summary>${d.discovery_warnings.map(w=>`<p class="small">${escape(w.url)} — ${escape(w.reason)}</p>`).join('')}</details>`:'')+
    `<div class="filters page-filters">${input('search','Search URL or meta title','search','Find a page or set of pages')}${select('status','Crawl status',crawlNames)}<button data-page-clear>Clear filters</button></div>`+
    disclosure('page-filters','More filters'+(Object.entries(pageFilters).some(([k,v])=>!['page','search','status'].includes(k)&&v)?' · active':''),`<div class="filters page-filters">${input('path','URL contains','search','/products/')}${input('http','HTTP status','number','200')}${select('title','Meta title',{missing:'Missing',present:'Present'})}${input('start','Crawled from (UTC)','date')}${input('end','Crawled to (UTC)','date')}${select('canonical_diff','Canonical',{1:'Different URL'})}${select('restricted','Indexing / snippets',{1:'Restriction detected'})}</div>`)+
    disclosure('inclusion-check','Search Console inclusion check · '+(inc?.status==='included'?'user confirmed':'verification record'),inclusionView(inc))+
    `<div class="section-heading"><h2>${d.total.toLocaleString()} matching URLs</h2><span class="small muted">100 maximum per page</span></div><div class="table-wrap"><table class="pages-table"><thead><tr><th>URL / meta title</th><th>Crawl result</th><th>HTTP</th><th>Last attempted</th><th>Findings / action</th></tr></thead><tbody>${d.rows.map(r=>`<tr><td><button class="query-link" data-page-id="${r.id}">${escape(r.title||(r.successful?'Missing meta title':'Title not collected'))}</button><small>${escape(r.url)}</small></td><td>${escape(crawlNames[r.status]||r.status)}${r.reason?disclosure('page-reason-'+r.id,'Retrieval details',`<p class="small">${escape(r.reason)}</p>`):''}${r.successful&&r.status!=='done'?`<small>Title retained from ${stamp(r.successful)}</small>`:''}</td><td>${r.http_status||'—'}</td><td>${stamp(r.crawled)}</td><td>${r.restricted?'Restriction detected':r.canonical_diff?'Canonical differs':r.status==='done'?'Inspect page':'Not verified'}<button class="text-button" data-review-page="${r.id}">Review this page →</button></td></tr>`).join('')||'<tr><td colspan="5">No matching pages. Start a crawl or adjust filters.</td></tr>'}</tbody></table></div>`+
    `<div class="section-heading"><button data-page-turn="-1" ${d.page<=1?'disabled':''}>Previous</button><span>Page ${d.page} of ${d.pages} · ${d.total?((d.page-1)*100+1):0}–${Math.min(d.page*100,d.total)} of ${d.total.toLocaleString()}</span><button data-page-turn="1" ${d.page>=d.pages?'disabled':''}>Next</button></div>`+
    disclosure('crawl-method','What the crawl can tell you','<p class="small muted">10,000 distinct URLs maximum; five pages per automatic batch. Public HTML only. Crawl restrictions are respected. JavaScript is not executed. Downloads do not use Bright Data SERP requests. Crawl observations do not prove Google indexing or guarantee citations.</p>');
}
function showRetry(){
  const r=pageData?.retry;if(!r||r.status!=='complete')return;
  const dialog=$('#retry-dialog');dialog.dataset.projectId=String(pageProject);dialog.dataset.runId=String(r.id);
  $('#retry-information').innerHTML=`<p>Completed ${stamp(r.finished)}. Only the ${r.total} pages marked Blocked or Unavailable when this retry started are counted.</p><dl><dt>Successfully crawled</dt><dd>${r.successful}</dd><dt>Still unsuccessful</dt><dd>${r.unsuccessful}</dd></dl><p>Successful means readable HTML was retrieved and analysed. Unsuccessful includes blocked, unavailable and non-HTML results.</p><button data-action="export" data-kind="crawl-retry" data-run-id="${r.id}" data-project-id="${pageProject}" ${r.unsuccessful?'':'disabled'}>Export unsuccessful pages CSV</button><p class="small muted">The CSV contains URL, meta title if previously collected, status, HTTP status, final URL, attempt time and failure details from this retry.</p>`;
  dialog.showModal();retryMonitorProject=0;
}
document.addEventListener('DOMContentLoaded',()=>{
  $('#retry-dialog').addEventListener('close',event=>{const d=event.target;localStorage.setItem(`crawl-retry-dismissed-${d.dataset.projectId}`,d.dataset.runId);});
});
function inclusionView(inc){
  const choices={unknown:'Not verified',included:'Included — user confirmed',excluded:'Excluded — user confirmed',inherited:'Inherited — check the parent setting'};
  return `<section class="inclusion"><h2>Search generative AI inclusion check</h2><p>In your verified Search Console property, review Settings and the Search generative AI inclusion setting, including inherited settings. Record what you verified. The crawler cannot read this account setting or confirm Google indexing.</p><form id="inclusion-form"><label for="inclusion-status">Your confirmed status</label><select id="inclusion-status" name="status">${Object.entries(choices).map(([v,t])=>`<option value="${v}" ${(inc?.status||'unknown')===v?'selected':''}>${t}</option>`).join('')}</select><label for="inclusion-note">Verification note</label><textarea id="inclusion-note" name="note" maxlength="2000" rows="2" placeholder="Property checked and relevant evidence">${escape(inc?.note||'')}</textarea><button type="submit">Save inclusion check</button>${inc?`<p class="small muted">User-recorded ${stamp(inc.checked)}; not crawler-verified.</p>`:''}</form></section>`;
}
async function showPage(id){
  const r=await api(`/api/page?project_id=${projectId}&id=${id}`),p=r.result;
  const item=(label,value)=>`<dt>${label}</dt><dd>${escape(value??'Not available')}</dd>`;
  $('#page-information').innerHTML=`<h3>${escape(p.title||r.title||'Page')}</h3><p>${escape(crawlNames[r.status]||r.status)} · Last successful retrieval ${stamp(r.successful)}</p><p><a href="${escape(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">${escape(r.url)}</a></p>${r.status!=='done'?`<div class="notice">${escape(r.reason||'Not yet crawled')}. ${r.successful?'Details retained from last successful crawl.':'Complete page evidence unavailable.'}</div>`:''}<h3>Observed findings</h3><ul>${(p.findings||[]).map(f=>`<li>${escape(f)}</li>`).join('')||'<li>No listed findings. This does not confirm indexing or guarantee citations.</li>'}</ul>${disclosure('page-metadata','Page metadata and retrieval details',`<dl>${item('Meta title',p.title)}${item('Meta description',p.description)}${item('Final URL',r.final_url)}${item('HTTP status',r.http_status)}${item('Last attempted',stamp(r.crawled))}${item('Last successful',stamp(r.successful))}${item('Canonical',p.canonical)}${item('Robots / snippets',p.robots||'None detected in retrieved HTML and headers')}${item('Word count',p.word_count)}${item('Observable author',p.author)}${item('Observable date',p.date)}</dl>`)}<h3>Headings</h3><ul>${(p.headings||[]).map(h=>`<li>${escape(h.level.toUpperCase()+': '+h.text)}</li>`).join('')||'<li>None available</li>'}</ul><h3>Main readable content${p.content_truncated?' (truncated)':''}</h3><pre class="page-content">${escape(p.content||'Not available. JavaScript is not executed.')}</pre><details><summary>Internal links (${(p.links||[]).length}, maximum 500)</summary><ul>${(p.links||[]).map(l=>`<li>${escape(l.text||'(No anchor text)')} — ${escape(l.url)}</li>`).join('')}</ul></details><details><summary>Observable structured data</summary><pre class="page-content">${escape(JSON.stringify(p.structured_data||[],null,2))}</pre></details>`;
  $('#page-dialog').showModal();
}
document.addEventListener('click',async event=>{
  const b=event.target.closest('button');if(!b)return;
  try{
    if(b.hasAttribute('data-page-reload')){pageError='';await loadPages();}
    else if(b.dataset.close==='retry-dialog'){const d=$('#retry-dialog');localStorage.setItem(`crawl-retry-dismissed-${d.dataset.projectId}`,d.dataset.runId);}
    else if(b.dataset.pageAction){b.disabled=true;await api('/api/crawl',{project_id:projectId,action:b.dataset.pageAction});await loadPages();}
    else if(b.hasAttribute('data-page-clear')){for(const k of Object.keys(pageFilters))pageFilters[k]=k==='page'?1:'';await loadPages();}
    else if(b.dataset.pageTurn){pageFilters.page+=Number(b.dataset.pageTurn);await loadPages();$('#main').scrollIntoView();}
    else if(b.dataset.pageId)await showPage(Number(b.dataset.pageId));
    else if(b.hasAttribute('data-retry-results'))showRetry();
  }catch(error){toast(error.message);}finally{if(b.isConnected&&b.dataset.pageAction)b.disabled=false;}
});
document.addEventListener('change',event=>{if(event.target.dataset.pageFilter){pageFilters[event.target.dataset.pageFilter]=event.target.value;pageFilters.page=1;loadPages();}});
document.addEventListener('input',event=>{if(event.target.dataset.pageFilter&&event.target.type==='search'){pageFilters[event.target.dataset.pageFilter]=event.target.value;pageFilters.page=1;clearTimeout(pageSearchTimer);pageSearchTimer=setTimeout(loadPages,350);}});
