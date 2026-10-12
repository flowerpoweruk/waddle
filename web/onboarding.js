/* Drafts contain project choices only; credentials stay in protected server storage. */
const setupStorageKey='waddle-project-draft-v1';
const setupCountries={gb:'United Kingdom',us:'United States',ca:'Canada',au:'Australia',ie:'Ireland',de:'Germany',fr:'France',es:'Spain',it:'Italy',nl:'Netherlands',in:'India',nz:'New Zealand'};
const setupLanguages={en:'English',de:'German',fr:'French',es:'Spanish',it:'Italian',nl:'Dutch'};
let setupDraft=null,setupBusy=false,setupError='',setupStorageError='';
try{
  const saved=JSON.parse(localStorage.getItem(setupStorageKey)||'null');
  if(saved){
    if(!/^[a-zA-Z0-9-]{16,64}$/.test(saved.setup_id)||!['domain','name','country','language','device','text','first','scope'].every(k=>typeof saved[k]==='string'))throw Error('Invalid draft');
    setupDraft=Object.fromEntries(['setup_id','step','domain','name','country','language','device','text','first','scope','savedId','creating','attempted'].filter(k=>k in saved).map(k=>[k,saved[k]]));
    setupDraft.step=Math.min(3,Math.max(1,Number(saved.step)||1));
  }
}catch{setupStorageError='The saved draft could not be read. Start a new draft to continue.';}
function saveSetupDraft(){
  try{if(setupDraft)localStorage.setItem(setupStorageKey,JSON.stringify(setupDraft));else localStorage.removeItem(setupStorageKey);setupStorageError='';}
  catch{setupStorageError='This browser could not save your draft. Keep this tab open until the project is created.';}
}
function setupImage(name,className=''){return `<img class="setup-mascot ${className}" src="/assets/onboarding/${name}.png" width="1280" height="1280" alt="">`;}
function setupHost(value){
  const u=new URL(value.includes('://')?value:'https://'+value);
  const host=u.hostname.replace(/\.$/,'').replace(/^www\./,'').toLowerCase();
  if(!['http:','https:'].includes(u.protocol)||u.username||u.password||! /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(host))throw Error('Enter a public website domain, such as example.com.');
  return host;
}
function setupQueries(text){
  const lines=text.split(/\r?\n/).map(x=>x.trim()).filter(Boolean),seen=new Set(),unique=[];
  for(const line of lines){const key=queryKey(line);if(!seen.has(key)){seen.add(key);unique.push(line.replace(/\s+/g,' '));}}
  return {lines,unique,duplicates:lines.length-unique.length};
}
function setupOptions(values,selected){return Object.entries(values).map(([value,label])=>`<option value="${value}" ${value===selected?'selected':''}>${label}</option>`).join('');}
function beginSetup(){
  if(!setupDraft)setupDraft={setup_id:crypto.randomUUID(),step:1,domain:'',name:'',country:'gb',language:'en',device:'desktop',text:'',first:'',scope:'one'};
  setupError='';saveSetupDraft();destination('onboarding');
}
function setupFocus(){window.scrollTo(0,0);$('#main').focus({preventScroll:true});}
function setupScope(){
  try{
    const host=setupHost(setupDraft.domain),matches=state.projects.filter(p=>p.domain===host);
    return `<p>Tracking <strong>${escape(host)}</strong>, including its subdomains. A page address tracks the domain, not just that page.</p>`+(matches.length?`<div class="setup-existing"><strong>This domain already has a project</strong>${matches.map(p=>`<p>${escape(p.name)} · ${escape(setupCountries[p.country]||p.country)} · ${escape(setupLanguages[p.language]||p.language)} · ${escape(p.device)} <button type="button" class="text-button" data-setup="open-existing" data-id="${p.id}">Open project</button></p>`).join('')}<p class="small">You can still create a separate project, for example for another market.</p></div>`:'');
  }catch{return '<p>Enter your website to see which domain Waddle will track.</p>';}
}
function setupView(){
  if(!setupDraft)return header('New project','Your saved workspace is unchanged.')+`<button class="primary" data-action="new-project">Start project setup</button>`;
  const d=setupDraft,step=Math.min(3,Math.max(1,Number(d.step)||1)),queries=setupQueries(d.text||''),first=queries.unique.includes(d.first)?d.first:queries.unique[0]||'';
  const recovery=d.creating||d.savedId;
  const intro=header('New project','A website, the searches that matter, and your first saved check.',`<button data-setup="leave">Save draft & leave</button>`);
  if(recovery)return intro+`<section class="setup-recovery">${setupImage('08-needs-attention')}<div><h2>${d.savedId?'Your project has been created':'Confirm your saved project'}</h2><p>${d.attempted?'A first-check request was attempted. It will not be sent again automatically. Inspect the saved checks before deciding whether to run another.':d.savedId?'Your project is saved. You can continue from its Dashboard.':'Saving was interrupted. Recover using the same draft so another project is not created.'}</p>${setupError?`<p class="setup-error" role="alert">${escape(setupError)}</p>`:''}<div class="toolbar">${d.savedId?'<button class="primary" data-setup="open-created">Open project & inspect checks</button>':'<button class="primary" data-setup="recover">Recover saved project</button>'}<button data-setup="discard">Discard draft</button></div><p class="small muted">Discarding this draft does not delete any project or saved evidence.</p></div></section>`;
  let body='';
  if(step===1)body=`<h2>Which website would you like to track?</h2><p>Waddle saves checks of Google Search AI Overviews so you can see whether they cite your website and how the results change.</p>
    <label for="setup-domain">Website address<input id="setup-domain" name="domain" value="${escape(d.domain)}" required maxlength="2048" placeholder="example.com or a website URL" autocomplete="url" aria-describedby="setup-scope"></label>
    <div id="setup-scope" class="setup-scope" aria-live="polite">${setupScope()}</div>
    <label for="setup-name">Project name<input id="setup-name" name="name" value="${escape(d.name)}" required maxlength="100" placeholder="A name you’ll recognise"></label>
    <h3>Whose search results should we check?</h3><p class="small muted">These settings apply to every search in this project. Use separate projects for different markets.</p>
    <div class="form-row"><label>Search country<select name="country">${setupOptions(setupCountries,d.country)}</select><span class="small muted">The country of the people you want to reach.</span></label><label>Search language<select name="language">${setupOptions(setupLanguages,d.language)}</select></label></div>
    <label>Search device<select name="device">${setupOptions({desktop:'Desktop',mobile:'Mobile'},d.device)}</select><span class="small muted">Controls the results requested, regardless of the device you’re using.</span></label>`;
  if(step===2)body=`<h2>What would your customers search for?</h2><p>Add the phrases you want to check in Google. Waddle calls these queries.</p><label for="setup-text">Search phrases — one per line<textarea id="setup-text" name="text" rows="7" maxlength="100000" placeholder="Type or paste your searches here" aria-describedby="setup-query-count">${escape(d.text)}</textarea></label><p id="setup-query-count" class="small" aria-live="polite">${queries.unique.length} searches · ${queries.duplicates} duplicates removed</p><p class="small muted">Start with 3–5 searches if you’re unsure. One is enough to begin; you can add more later. Up to 500 per batch, each up to 500 characters.</p>
    <details class="disclosure"><summary>Help me choose searches</summary><ul><li>What product or service would someone look for?</li><li>What question would they ask before choosing?</li><li>What alternatives might they compare?</li></ul><p class="small">Example only: “how to choose running shoes”. Choose searches relevant to your business; examples are never added for you.</p></details><p class="setup-reassurance">Adding searches saves them to your project. It doesn’t run a check.</p>`;
  if(step===3)body=`<h2>Ready for your first check?</h2><p>Review your choices, then start with one search to see how the evidence works.</p>
    <dl class="setup-summary"><dt>Website</dt><dd>${escape(setupHost(d.domain))}<small>Subdomains included</small></dd><dt>Project</dt><dd>${escape(d.name)}</dd><dt>Search audience</dt><dd>${escape(setupCountries[d.country])} · ${escape(setupLanguages[d.language])} · ${escape(d.device)}</dd><dt>Searches to save</dt><dd>${queries.unique.length}<details><summary>View search list</summary><ul>${queries.unique.map(q=>`<li>${escape(q)}</li>`).join('')}</ul></details></dd></dl><button type="button" class="text-button" data-setup="edit-site">Edit website or audience</button><button type="button" class="text-button" data-setup="edit-searches">Edit searches</button>
    ${queries.unique.length?`<fieldset class="setup-check"><legend>What should the first check include?</legend><label class="setup-radio"><input type="radio" name="scope" value="one" ${d.scope!=='all'?'checked':''}> One search first — recommended</label><label>First search<select name="first">${queries.unique.map(q=>`<option value="${escape(q)}" ${q===first?'selected':''}>${escape(q)}</option>`).join('')}</select></label><label class="setup-radio"><input type="radio" name="scope" value="all" ${d.scope==='all'?'checked':''}> All ${queries.unique.length} ${queries.unique.length===1?'search':'searches'}</label></fieldset>`:'<p>No searches added yet. You can save your project and add them later.</p>'}
    <section class="setup-connection"><h3>Search collection — Bright Data</h3><p id="setup-connection-status">${setupConnectionStatus()}</p><p class="small">Waddle sends your search phrases and selected search settings to Bright Data to collect Google results. Saved evidence stays on this computer. This connection is shared across all projects.</p>
    <button type="button" data-setup="test">Test connection</button><details class="disclosure"><summary>${state.key_saved?'Replace shared API key':'Add a Bright Data API key'}</summary><label>API key<input id="setup-api-key" type="password" autocomplete="new-password" maxlength="512"></label><p class="small">Saved using Windows protected storage. Replacing this key affects every project.</p><button type="button" data-setup="save-key">Save API key</button><p><a href="https://docs.brightdata.com/api-reference/authentication" target="_blank" rel="noopener noreferrer">Where do I get an API key?</a></p></details><p class="small muted">Ollama is optional for later page reviews. You don’t need it to collect search results.</p></section>
    <div class="setup-usage"><strong id="setup-request-count">${setupRequestCopy()}</strong><p>Summary and screenshot are separate requests. Retries can use more. Bright Data charges depend on your account.</p><p>${state.usage} requests this month · Waddle’s 500-request guide is advisory, not a billing allowance or hard limit.</p>${state.warning?'<p class="setup-warning">You have reached the 400-request warning. Review your usage before starting.</p>':''}<p>Checks are started manually. Creating a project does not set up a monitoring schedule.</p></div>`;
  const image=['02-your-website','03-your-searches','04-ready-to-check'][step-1];
  return intro+`<div class="setup-layout"><aside class="setup-rail" aria-label="Project setup progress"><ol>${['Website','Searches','First check'].map((name,i)=>`<li ${i+1===step?'aria-current="step"':''}><span>${i+1}</span>${name}${i+1<step?'<small>Completed</small>':''}</li>`).join('')}</ol>${setupImage(image)}<p>${setupStorageError?'Draft could not be saved.':'Draft saved on this browser.'}<br>Your existing projects stay available.</p><button class="text-button" data-setup="discard">Discard draft</button></aside>
    <form id="setup-form" class="setup-panel"><div class="setup-fields">${body}<p id="setup-error" class="setup-error" role="alert" ${setupError?'':'hidden'}>${escape(setupError)}</p>${setupStorageError?`<p class="setup-error" role="alert">${escape(setupStorageError)}</p>`:''}</div><div class="setup-footer"><div>${step>1?'<button type="button" data-setup="back">Back</button>':''}<span class="small muted">Step ${step} of 3</span></div><div class="setup-actions">${step===1?'<button type="button" class="text-button" data-setup="quick">Create project without guided setup</button>':step===2?'<button type="button" class="text-button" data-setup="skip-searches">Add searches later</button>':'<button type="button" data-setup="save-only">Create project without checking</button>'}<button type="submit" class="primary" ${step===3&&(!queries.unique.length||!state.key_saved||!state.zone_ready)?'disabled':''}>${step===3?(d.scope==='all'?'Create project & check all searches':'Create project & check this search'):'Continue'}</button></div></div></form></div>`;
}
function setupConnectionStatus(){return `${state.key_saved?'API key saved.':'No API key saved.'} ${escape(state.connection||'Connection not verified.')} ${state.zone_ready?'Previous service discovery succeeded; this is not a live connectivity guarantee.':'Connection not verified for checking.'}`;}
function setupRequestCopy(){const n=setupDraft?.scope==='all'?setupQueries(setupDraft.text||'').unique.length:1;return `${n} ${n===1?'check normally uses 2 requests':'checks normally use '+n*2+' requests'}.`;}
function updateSetupDraft(form){
  if(!setupDraft||setupDraft.creating||setupDraft.savedId)return;
  for(const [name,value] of new FormData(form))if(['domain','name','country','language','device','text','first','scope'].includes(name))setupDraft[name]=value;
  saveSetupDraft();
}
function validateSetupSite(){
  const d=setupDraft;setupHost(d.domain);
  if(!d.name.trim()||d.name.trim().length>100)throw Error('Enter a project name of 1 to 100 characters.');
}
function validateSetupQueries(){const q=setupQueries(setupDraft.text||'');if((setupDraft.text||'').length>100000||q.lines.length>500||q.lines.some(x=>x.length>500))throw Error('Add up to 500 searches, each no longer than 500 characters.');}
function showSetupError(error){setupError=error.message;if(setupDraft?.creating||setupDraft?.savedId){render();return;}const box=$('#setup-error');if(box){box.textContent=setupError;box.hidden=false;box.scrollIntoView({block:'nearest'});}else render();}
async function finishSetup(check=false){
  validateSetupSite();validateSetupQueries();
  const d=setupDraft,q=setupQueries(d.text||'');
  d.creating=true;saveSetupDraft();
  const result=await api('/api/project-setup',{...d,domain:setupHost(d.domain),text:q.unique.join('\n')});
  d.savedId=result.id;d.creating=false;saveSetupDraft();projectId=result.id;await refresh(false);
  if(check){
    if(!state.key_saved||!state.zone_ready)throw Error('Project saved. Test the search connection before starting a check.');
    const chosen=state.queries.find(x=>queryKey(x.text)===queryKey(d.first||q.unique[0]||''));
    const ids=d.scope==='all'?result.query_ids:[chosen?.id].filter(Boolean);
    if(!ids.length)throw Error('Project saved. Add a search before checking.');
    d.attempted=true;saveSetupDraft();
    await api('/api/run',{project_id:result.id,query_ids:ids,request_id:d.setup_id});
  }
  setupDraft=null;saveSetupDraft();setupError='';destination('dashboard');await refresh();
  toast(check?(d.scope==='all'&&q.unique.length>1?'Your project is ready. The selected searches are queued.':'Your project is ready. The selected check is queued.'):'Your project is ready. No checks were started.');
}
async function guidancePreference(value){await api('/api/onboarding-guidance',{project_id:projectId,...value});await refresh();}
function onboardingResult(capture){
  if(capture.status==='found')return capture.refs?.some(r=>r.owned)?'This AI Overview cited your website.':'This AI Overview didn’t cite your website.';
  if(capture.status==='absent')return 'No AI Overview was shown in this check.';
  if(['queued','running'].includes(capture.status))return 'Your check is in progress. You can leave this screen; saved results will remain available.';
  return 'We couldn’t establish a usable result. A failed or incomplete check does not show that your website was absent.';
}
function firstResultGuide(capture){
  if(!state.onboarding||state.onboarding.dismissed||state.onboarding.result_seen)return '';
  const waiting=['queued','running'].includes(capture.status),failed=['failed','incomplete','cancelled'].includes(capture.status);
  return `<section class="onboarding-note">${setupImage(waiting?'05-check-in-progress':failed?'08-needs-attention':'06-first-result')}<div><h2>${waiting?'Your check is in progress':'Understanding this result'}</h2><p>${onboardingResult(capture)}</p><p class="small">This is a saved observation for this search, country, language and device. Results can change between checks. The response and its cited URLs appear below.</p>${!waiting&&!failed?`<p class="small">${capture.has_screenshot?'The screenshot is collected separately and may differ from the response.':'An unavailable screenshot does not discard a usable response.'}</p>`:''}${!waiting?'<button data-setup="result-seen">Got it</button>':''}</div></section>`;
}
function onboardingPanel(){
  const g=state.onboarding;
  if(!projectId)return setupDraft?`<section class="onboarding-note">${setupImage('01-welcome')}<div><h2>Continue your project setup</h2><p>Your draft is saved in this browser.</p><button class="primary" data-action="new-project">Resume setup</button></div></section>`:'';
  if(!g||g.dismissed)return `<p><button class="text-button" data-setup="reopen">Show project guidance</button>${setupDraft?' · <button class="text-button" data-action="new-project">Resume new-project draft</button>':''}</p>`;
  const queries=state.queries,usable=queries.filter(q=>q.last_usable),checked=queries.find(q=>q.latest&&!q.pending),pending=queries.some(q=>q.pending);
  let title='Your project is ready',copy='',image='07-project-ready',action='';
  if(!queries.length){copy='Add the searches that matter to your business. Adding them won’t run a check.';action='<button class="primary" data-action="add-queries">Add your first searches</button>';}
  else if(!usable.length){
    if(pending){title='Your first check is in progress';image='05-check-in-progress';copy='You can keep using Waddle while the selected searches are collected.';action='<button data-view="queries">View check progress</button>';}
    else if(checked){title='Your check needs attention';image='08-needs-attention';copy='The check failed or was incomplete. Inspect the reason before deciding whether to run another.';action=`<button class="primary" data-action="detail" data-id="${checked.id}">Inspect check</button>`;}
    else if(!state.key_saved||!state.zone_ready){title='Connect search collection';copy='Save and test a Bright Data key in Settings. The connection is shared across projects. Ollama is optional for later reviews.';action='<button class="primary" data-view="settings">Set up search connection</button>';}
    else{copy='Start with one search. A check normally uses two requests; provider charges depend on your account. Checks are manual.';action=`<button class="primary" data-setup="first-check" data-id="${queries.find(q=>!q.paused)?.id||0}">Check your first search</button>`;}
  }else if(!g.result_seen){title='Your first result is ready';image='06-first-result';copy='Inspect what Google showed, whether your domain was cited, and the sources in the response.';action=`<button class="primary" data-action="detail" data-id="${usable[0].id}" data-capture="${usable[0].last_usable.id}">View your first result</button>`;}
  else if(!g.pages){title='Explore your website pages';image='09-explore-your-website';copy=g.crawl_status?`Website discovery status: ${g.crawl_status}. Inspect its progress or access issues. Search monitoring can continue independently.`:'Discover website pages when you’re ready to connect searches to content. Starting a crawl is optional and does not use SERP requests.';action='<button class="primary" data-view="pages">Explore website pages</button>';}
  else if(!queries.some(q=>['existing','planned','missing'].includes(q.plan?.target))){title='Choose a page for a search';image='10-improve-a-page';copy='Confirm which page should answer a search, record a planned page, or confirm that no suitable page exists. Suggestions are not confirmed mappings.';action=`<button class="primary" data-plan="${usable[0].id}">Choose target page</button>`;}
  else if(!g.review){title='Review a page when you’re ready';image='10-improve-a-page';copy='Set up and test an installed Ollama model in Settings for local page reviews. Review the selected evidence before explicitly starting generation.';action='<button class="primary" data-view="improvements">Explore page improvements</button>';}
  else if(g.review.status!=='completed'){title='Inspect your page review';image=g.review.status==='failed'?'08-needs-attention':'10-improve-a-page';copy='A queued, running or failed review is not a completed recommendation. Inspect its status; retries remain your choice.';action=`<button class="primary" data-improvement="open" data-id="${g.review.id}">Open review</button>`;}
  else if(!g.briefs&&!g.tasks){title='Decide what is worth changing';image='10-improve-a-page';copy='Review the evidence and accept, reject or correct recommendations. Create a brief or task only from supported work; a review may recommend no change.';action=`<button class="primary" data-improvement="open" data-id="${g.review.id}">Review recommendations</button>`;}
  else{title='Your next steps are yours';copy='Your evidence and page work are saved. Keep checking manually and review changes over time. Briefs and tasks do not publish website changes.';action='<button class="primary" data-setup="dismiss">Finish guided setup</button>';}
  const remaining=queries.filter(q=>!q.latest&&!q.paused&&!q.pending);
  return `<section class="onboarding-note" aria-label="Next steps for this project">${setupImage(image)}<div><div class="section-heading"><h2>${title}</h2><button class="text-button" data-setup="dismiss" aria-label="Dismiss project guidance">Dismiss</button></div><p>${escape(copy)}</p><div class="toolbar">${action}${usable.length&&remaining.length?`<button data-setup="remaining">Check ${remaining.length} remaining ${remaining.length===1?'search':'searches'}</button>`:''}</div>${usable.length?'<details><summary>Other ways to investigate</summary><p class="small">Queries → Related questions offers ideas observed in saved results. Cited sources contains exact cited URLs and recurring domains; retrieve comparison content only when you choose. Add competitors manually or from relevant citations. One citation does not establish a competitor.</p><div class="toolbar"><button data-view="related">Related questions</button><button data-view="sources">Cited sources</button><button data-view="competitors">Competitors</button><button data-view="settings">Local AI settings</button></div></details>':''}</div></section>`;
}
document.addEventListener('input',event=>{
  const form=event.target.closest('#setup-form');if(!form)return;
  if(event.target.id==='setup-api-key')return;
  const oldDomain=setupDraft.domain,oldName=setupDraft.name;updateSetupDraft(form);
  if(event.target.name==='domain'){
    try{const host=setupHost(setupDraft.domain);let previous='';try{previous=setupHost(oldDomain);}catch{}if(!oldName||oldName===previous){setupDraft.name=host;$('#setup-name').value=host;saveSetupDraft();}}catch{}
    $('#setup-scope').innerHTML=setupScope();
  }
  if(event.target.name==='text'){const q=setupQueries(setupDraft.text);$('#setup-query-count').textContent=`${q.unique.length} searches · ${q.duplicates} duplicates removed`;}
  if(event.target.name==='scope'){$('#setup-request-count').textContent=setupRequestCopy();$('#setup-form button[type=submit]').textContent=setupDraft.scope==='all'?'Create project & check all searches':'Create project & check this search';}
});
document.addEventListener('change',event=>{const form=event.target.closest('#setup-form');if(form&&event.target.id!=='setup-api-key')updateSetupDraft(form);});
document.addEventListener('submit',async event=>{
  if(event.target.id!=='setup-form')return;event.preventDefault();if(setupBusy)return;setupBusy=true;
  const submit=event.target.querySelector('button[type=submit]');submit.disabled=true;updateSetupDraft(event.target);setupError='';
  try{if(setupDraft.step===1){validateSetupSite();setupDraft.step=2;}else if(setupDraft.step===2){validateSetupQueries();setupDraft.step=3;}else{await finishSetup(true);return;}saveSetupDraft();render();setupFocus();}
  catch(e){showSetupError(e);}finally{setupBusy=false;if(submit.isConnected)submit.disabled=false;}
});
document.addEventListener('click',async event=>{
  const b=event.target.closest('[data-setup]');if(!b||setupBusy)return;
  const action=b.dataset.setup;setupBusy=true;b.disabled=true;setupError='';
  try{
    if(action==='leave'){saveSetupDraft();destination('dashboard');}
    else if(action==='discard'){if(confirm('Discard this setup draft? Any project already created and its saved evidence will be kept.')){setupDraft=null;saveSetupDraft();destination('dashboard');}}
    else if(action==='back'||action==='edit-site'||action==='edit-searches'||action==='skip-searches'){if($('#setup-form'))updateSetupDraft($('#setup-form'));setupDraft.step=action==='edit-site'?1:action==='edit-searches'?2:action==='skip-searches'?3:Math.max(1,setupDraft.step-1);if(action==='skip-searches')setupDraft.text='';saveSetupDraft();render();setupFocus();}
    else if(['quick','save-only','recover'].includes(action)){if($('#setup-form'))updateSetupDraft($('#setup-form'));await finishSetup(false);}
    else if(action==='open-existing'||action==='open-created'){projectId=Number(b.dataset.id||setupDraft.savedId);if(action==='open-created'){setupDraft=null;saveSetupDraft();}await refresh(false);destination('dashboard');}
    else if(action==='save-key'){const input=$('#setup-api-key');await api('/api/key',{key:input.value});input.value='';await refresh(false);render();toast('Shared API key saved. Test the connection before checking.');}
    else if(action==='test'){const result=await api('/api/test-connection',{});await refresh(false);render();toast(result.message);}
    else if(action==='dismiss'||action==='reopen'||action==='result-seen')await guidancePreference(action==='result-seen'?{result_seen:true}:{dismissed:action==='dismiss'});
    else if(action==='first-check'){
      const id=Number(b.dataset.id);if(!id)throw Error('Resume a query before checking.');
      await api('/api/run',{project_id:projectId,query_ids:[id],request_id:state.onboarding?.first_request||'onboarding-first-'+projectId});await refresh();
    }else if(action==='remaining'){
      const ids=state.queries.filter(q=>!q.latest&&!q.paused&&!q.pending).map(q=>q.id);
      if(ids.length&&confirm(`Check ${ids.length} remaining ${ids.length===1?'search':'searches'}? Normally ${ids.length*2} requests; provider charges apply and retries can use more.`))await run(ids);
    }
  }catch(e){
    if(action==='test'){try{await refresh(false);render();}catch{}}
    if(view==='onboarding')showSetupError(e);else toast(e.message+' Inspect saved checks before retrying; nothing is retried automatically.');
  }
  finally{setupBusy=false;if(b.isConnected)b.disabled=false;}
});
