const $ = (selector) => document.querySelector(selector);
let state;
let toastTimer;
let formSettings = {};
let prefillRunning = false;
const names = {overview:'Overview',playground:'Playground',chats:'Connected chats',activity:'Activity',connection:'Connections',agent:'Agent settings'};
const escapeHTML = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

function toast(message, error=false) {
  clearTimeout(toastTimer);
  const el=$('#toast'); el.textContent=message; el.classList.toggle('error',error); el.hidden=false;
  toastTimer=setTimeout(()=>el.hidden=true,6000);
}
async function api(path, options={}) {
  const response=await fetch('/api'+path, {...options, headers:{'X-Moose-Request':'1',...(options.body instanceof FormData ? {} : {'Content-Type':'application/json'}),...options.headers}});
  let data; try { data=await response.json(); } catch { throw new Error('The server did not return a valid response.'); }
  if(!response.ok) throw new Error(typeof data.detail==='string' ? data.detail : 'Check the entered values and try again.');
  return data;
}
function route() {
  const page=Object.hasOwn(names,location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
  document.querySelectorAll('.page').forEach(el=>el.hidden=el.id!==page);
  document.querySelectorAll('[data-page]').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
  $('#breadcrumb').textContent=names[page];
}
function empty(title, description) {return `<div class="empty-state"><span>✧</span><h4>${escapeHTML(title)}</h4><p>${escapeHTML(description)}</p></div>`;}
function eventHTML(event) {
  const labels={success:'Replied',error:'Error',blocked:'Blocked',limited:'Limited'};
  const replies=event.reply_messages || [], tools=event.tools_used || [];
  const memory=event.memory_review ? `<p>Memory review: ${escapeHTML(({updated:'Updated',no_change:'No new memory needed',conflict:'Conflict — newer memory preserved',failed:'Failed — reply unaffected'})[event.memory_review] || event.memory_review)}</p>` : '';
  const details=replies.length || tools.length ? `<details class="event-details" data-event="${escapeHTML(event.id)}"><summary>View reply & tools</summary><div class="event-tools">Tools called: ${tools.length ? tools.map(escapeHTML).join(' → ') : 'None'}</div>${replies.map(text=>`<pre class="event-reply">${escapeHTML(text)}</pre>`).join('')}${!replies.length ? '<p>No text reply.</p>' : ''}</details>` : '';
  return `<div class="event"><span class="badge ${escapeHTML(event.status)}">${labels[event.status]||'Event'}</span><div class="event-main"><strong>${escapeHTML(event.chat)}</strong><p>${escapeHTML(event.detail)}</p>${memory}${details}</div><time title="${escapeHTML(event.time)}">${escapeHTML(new Date(event.time).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}))}</time></div>`;
}
function renderActivity() {
  const expanded=new Set([...document.querySelectorAll('.event-details[open]')].map(el=>el.dataset.event));
  const filter=$('#activity-filter').value;
  const events=state.activity.filter(e=>filter==='all'||e.status===filter);
  $('#activity-list').innerHTML=events.map(eventHTML).join('')||empty('A clean slate.','Mentions and optional participation appear here.');
  $('#recent-activity').innerHTML=state.activity.slice(0,3).map(eventHTML).join('')||empty('Quiet for now.','Your agent’s next conversation starts here.');
  document.querySelectorAll('.event-details').forEach(el=>{el.open=expanded.has(el.dataset.event);});
}
function imageModelHint() {
  const input=$('#agent-form [name="image_model"]');
  const model=input.value.trim().toLowerCase();
  const hint=input.closest('label').querySelector('small');
  if(model.startsWith('recraft/')&&model.includes('vector')) {
    hint.textContent='This model outputs SVG, which Moose does not support. Choose a raster image model; Recraft Styles also requires a style-reference image.';
  } else if(model.startsWith('recraft/')&&model.includes('styles')) {
    hint.textContent='Recraft Styles requires a style-reference image and creates new images in that style. Choose a general-purpose image model for edits such as background removal.';
  } else {
    hint.textContent='Must support PNG, JPEG, WebP, or GIF output; editing also requires image input.';
  }
}
function render(populate=false) {
  const s=state.settings;
  $('#debug-token-status').textContent=state.debug_token_configured?'Debug token active. Rotating it invalidates the previous token.':'No debug token configured.';
  $('#news-usage').textContent=`${state.news_usage?.requests || 0} of ${s.news_daily_limit} news requests used today (UTC).`;
  const memory=$('#agent-form [name="consciousness"]');
  if(!populate && memory.value===formSettings.consciousness) {
    memory.value=s.consciousness;
    formSettings.consciousness=s.consciousness;
  }
  const allowed=state.chats.filter(c=>c.allowed).length;
  const status=state.bot.status;
  const active=['running','connecting','reconnecting'].includes(status);
  document.body.classList.toggle('running',active);
  document.querySelectorAll('.bot-toggle').forEach(el=>{el.textContent=s.enabled?'Ⅱ Pause agent':'▶ Start agent';});
  $('#status-stat').textContent=({running:'Listening',connecting:'Connecting',reconnecting:'Reconnecting',error:'Needs attention',stopped:'Offline'})[status]||status;
  $('.bot-status').textContent=({running:'Listening to conversations',connecting:'Connecting',reconnecting:'Reconnecting',error:'Connection error',stopped:'Agent paused'})[status]||status;
  $('#username-stat').textContent=state.bot.username?'@'+state.bot.username:'Connect your Telegram bot to begin';
  $('#allowed-stat').textContent=allowed; $('#chat-count').textContent=allowed;
  $('#responses-stat').textContent=state.stats.responses;
  const steps=[s.has_bot_token,s.has_api_key&&s.model,allowed>0||s.allow_private];
  ['telegram','ai','chats'].forEach((name,i)=>{const el=$('#step-'+name);el.classList.toggle('done',!!steps[i]);el.textContent=steps[i]?'✓':i+1;});
  $('#setup-count').textContent=`${steps.filter(Boolean).length} of 3 complete`;
  $('#load-error').hidden=!state.bot.error; $('#load-error').textContent=state.bot.error;
  $('#chat-list').innerHTML=state.chats.map(chat=>{
    const permitted=chat.kind==='private'?s.allow_private:chat.allowed;
    return `<div class="chat-row"><span class="chat-icon">▤</span><div><strong>${escapeHTML(chat.title)}</strong><small>${chat.id} · ${escapeHTML(chat.kind)}</small></div><span class="badge ${permitted?'success':'blocked'}">${permitted?'Allowed':'Blocked'}</span>${chat.kind==='private'?'<span class="subtle">Use private chat settings</span>':`<button class="secondary chat-toggle" data-id="${chat.id}">${chat.allowed?'Revoke access':'Allow chat'}</button>`}</div>`;
  }).join('')||empty('Your agent is waiting for an invitation.','Allow a group or channel using its Telegram chat ID.');
  renderActivity();
  if(populate) {
    formSettings={...s};
    for(const form of [$('#connection-form'),$('#agent-form')]) for(const element of form.elements) {
      if(!element.name||!(element.name in s)) continue;
      if(element.type==='checkbox') element.checked=s[element.name]; else element.value=s[element.name];
    }
    $('#bot-token-hint').textContent=s.has_bot_token?'Token saved. Leave blank to keep it.':'No token saved yet.';
    $('#api-key-hint').textContent=s.has_api_key?'API key saved. Leave blank to keep it.':'No API key saved yet.';
    $('#news-api-key-hint').textContent=s.has_news_api_key?'News token saved. Leave blank to keep it.':'No news token saved yet.';
    imageModelHint();
  }
  const prefill=state.consciousness_prefill || {};
  $('#prefill-consciousness').disabled=prefillRunning || prefill.running;
  if(prefillRunning || prefill.running) $('#prefill-status').textContent='Analyzing conversation history… Large histories may take several minutes. The result will be saved automatically.';
  else $('#prefill-status').textContent='Refresh when needed: merge recent history into existing memory, or rebuild from the selected history window. Automatic memory review continues during normal interactions.';
}
async function refresh(populate=false) {state=await api('/state');render(populate);}
async function busy(button, fn) {
  const text=button.textContent; button.disabled=true;button.textContent='Working…';
  try {await fn();} catch(error) {toast(error.message,true);} finally {button.disabled=false;button.textContent=text;if(state)render();}
}
function values(form) {
  const result={};
  for(const element of form.elements) {
    if(!element.name) continue;
    if(['bot_token','api_key','news_api_key'].includes(element.name)&&!element.value.trim()) continue;
    result[element.name]=element.type==='checkbox'?element.checked:element.type==='number'?Number(element.value):element.value;
  }
  return result;
}
for(const id of ['connection-form','agent-form']) $('#'+id).addEventListener('submit',event=>{
  event.preventDefault();const form=event.currentTarget;
  busy(form.querySelector('[type=submit]'),async()=>{
    const changed=Object.fromEntries(Object.entries(values(form)).filter(([key,value])=>value!==formSettings[key]));
    if(Object.hasOwn(changed,'consciousness')) changed.consciousness_previous=formSettings.consciousness;
    await api('/settings',{method:'PATCH',body:JSON.stringify(changed)});
    if(id==='connection-form'){form.elements.bot_token.value='';form.elements.api_key.value='';form.elements.news_api_key.value='';}
    await refresh(true);toast('Settings saved.');
  });
});
$('#prefill-consciousness').addEventListener('click',event=>busy(event.currentTarget,async()=>{
  const form=$('#agent-form');
  if(['consciousness','system_prompt','consciousness_prompt','memory_review_max_tokens'].some(key=>form.elements[key].value!==String(formSettings[key]))) {
    throw new Error('Save your personality, consciousness, guidance, and consciousness token budget before refreshing.');
  }
  prefillRunning=true;render();
  try {
    const mode=$('#prefill-mode').value;
    if(mode==='rebuild' && state.settings.consciousness.trim() && !window.confirm('Rebuild consciousness from the selected history? Existing memory will be replaced only after successful analysis.')) return;
    const result=await api('/consciousness/prefill',{method:'POST',body:JSON.stringify({mode,days:Number($('#prefill-days').value)})});
    await refresh();
    toast(`Consciousness refreshed from ${result.batches} history batch(es).`);
  } finally {prefillRunning=false;}
}));
$('#generate-debug-token').addEventListener('click',event=>busy(event.currentTarget,async()=>{
  const result=await api('/debug-token',{method:'POST'});
  $('#debug-token').value=result.token;await refresh();toast('Debug token generated. Copy it now.');
}));
$('#revoke-debug-token').addEventListener('click',event=>busy(event.currentTarget,async()=>{
  await api('/debug-token',{method:'DELETE'});$('#debug-token').value='';await refresh();toast('Debug access revoked.');
}));
$('.bot-toggle').addEventListener('click',event=>busy(event.currentTarget,async()=>{
  await api('/settings',{method:'PATCH',body:JSON.stringify({enabled:!state.settings.enabled})});await refresh();
}));
document.querySelectorAll('.test-button').forEach(button=>button.addEventListener('click',()=>busy(button,async()=>{
  if(button.dataset.test==='news' && $('#connection-form [name="news_api_key"]').value.trim()) {
    throw new Error('Save connections first to test the new news token. This button tests the saved token.');
  }
  const result=await api('/test/'+button.dataset.test,{method:'POST'});toast(result.message);
})));
$('#chat-form').addEventListener('submit',event=>{
  event.preventDefault();const form=event.currentTarget;const data=values(form);data.id=Number(data.id);
  busy(form.querySelector('button'),async()=>{await api('/chats',{method:'PUT',body:JSON.stringify(data)});form.reset();await refresh();toast('Chat allowed. You can now invite your bot.');});
});
$('#chat-list').addEventListener('click',event=>{
  const button=event.target.closest('.chat-toggle');if(!button)return;
  const chat=state.chats.find(c=>String(c.id)===button.dataset.id);
  busy(button,async()=>{await api('/chats',{method:'PUT',body:JSON.stringify({id:chat.id,title:chat.title,allowed:!chat.allowed})});await refresh();toast('Chat access updated.');});
});
$('#activity-filter').addEventListener('change',renderActivity);
$('#agent-form [name="image_model"]').addEventListener('input',imageModelHint);
$('#playground-form').addEventListener('submit',event=>{
  event.preventDefault();const form=event.currentTarget;
  busy(form.querySelector('button'),async()=>{
    const file=form.elements.file.files[0];const text=form.elements.text.value;const context=form.elements.context.value;
    $('#output-status').textContent='Thinking…';$('#playground-output').textContent='Your agent is working on it…';
    try {
      let result;
      if(file){const body=new FormData();body.append('file',file);body.append('prompt',text);body.append('context',context);result=await api('/playground/media',{method:'POST',body});}
      else result=await api('/playground',{method:'POST',body:JSON.stringify({text,context})});
      const output=$('#playground-output');output.textContent='';
      for(const text of (result.messages || [result.text])) {
        const bubble=document.createElement('p');bubble.textContent=text;output.append(bubble);
      }
      for(const [index,url] of result.images.entries()) {
        const image=document.createElement('img');image.src=url;image.alt='Generated image '+(index+1);output.append(image);
        const link=document.createElement('a');link.href=url;link.download='moose-result-'+(index+1);link.textContent='Download original ↗';output.append(link);
      }
      $('#output-status').textContent=`Complete · ${result.tool_calls} tool calls${result.memory_review ? ' · Memory: '+result.memory_review : ''}`;
    }catch(error){$('#output-status').textContent='Unable to complete';$('#playground-output').textContent=error.message;throw error;}
  });
});
window.addEventListener('hashchange',route);route();
refresh(true).catch(error=>{$('#load-error').hidden=false;$('#load-error').textContent=error.message;});
setInterval(()=>{if(!document.hidden)refresh().catch(()=>{});},5000);
