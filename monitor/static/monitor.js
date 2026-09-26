'use strict';
const el = id => document.getElementById(id);
const text = v => v === null || v === undefined ? 'N/A' : String(v);
const number = v => v === null || v === undefined ? 'N/A' : v.toLocaleString();
const bytes = v => v == null ? 'N/A' : (v / 1073741824).toFixed(1) + ' GiB';
const duration = v => v == null ? 'N/A' : `${Math.floor(v/3600)}h ${Math.floor(v%3600/60)}m`;
const tone = v => ['running','active','complete','VERIFIED'].includes(v) ? 'good' : ['ERROR','unhealthy','failed'].includes(v) ? 'error' : ['REVIEW','GROUP_REVIEW','interrupted','incomplete','circuit-breaker','stopped','starting'].includes(v) ? 'warn' : '';
function node(tag, value, cls='') {const n=document.createElement(tag); n.textContent=text(value); n.className=cls; return n;}
function row(parent, label, value, cls='') {const r=node('div','','row');r.append(node('span',label),node('span',value,'value '+cls));parent.append(r);}
function render(data) {
  const d=data.database;
  for(const id of ['database','results','active','recent','server','services']) el(id).replaceChildren();
  for(const [label,value] of [['Total companies',number(d.total)],['Phase 1 complete',number(d.phase1_complete)],['Phase 2 processed',number(d.processed)],['Phase 2 remaining',number(d.remaining)],['Processed %',d.percent == null ? 'N/A' : d.percent+'%']]) row(el('database'),label,value);
  el('database').append(node('p',d.phase1_note,'muted'));
  if(d.note) el('database').append(node('p',d.note,'warn'));
  el('progress').value=d.percent || 0;
  for(const [status,count] of Object.entries(d.statuses)) row(el('results'),status,number(count),tone(status));
  if(d.other_statuses) row(el('results'),'Other / legacy status',number(d.other_statuses),'warn');
  row(el('results'),'Companies with usable emails',number(d.email_companies));row(el('results'),'Usable email addresses',number(d.emails));
  if(d.email_note) el('results').append(node('p',d.email_note,'warn'));
  if(!data.active.jobs.length) el('active').append(node('p',data.active.available ? 'No matching workers observed.' : data.active.note,'muted'));
  if(data.active.inaccessible_processes) el('active').append(node('p','Some processes could not be inspected; visibility is partial.','warn'));
  for(const j of data.active.jobs) {const box=node('div','','job');row(box,`PID ${j.pid}`,j.module,'good');row(box,'Report',j.report);row(box,'Limit / selected',text(j.limit)+' / '+text(j.selected));row(box,'Checkpointed / selected',text(j.processed)+' / '+text(j.selected));row(box,'Runtime',duration(j.runtime_seconds));row(box,'Current company',j.current_company ? `${j.current_company.id} ${j.current_company.name}` : null);row(box,'Search circuit breaker',j.circuit_breaker == null ? 'N/A' : j.circuit_breaker ? 'Triggered' : 'Not observed',j.circuit_breaker ? 'warn' : '');el('active').append(box);}
  const table=document.createElement('table'),head=document.createElement('tr');for(const h of ['REPORT','STATE','DONE / SELECTED','TIMESTAMP']) head.append(node('th',h));table.append(head);
  for(const j of data.recent.jobs) {const tr=document.createElement('tr');tr.append(node('td',j.name),node('td',j.status,tone(j.status)),node('td',`${j.processed} / ${j.selected}`),node('td',j.timestamp));table.append(tr);}el('recent').append(table);
  if(!data.recent.jobs.length) el('recent').append(node('p',data.recent.note || 'No runner reports found.','muted'));
  if(data.recent.skipped) el('recent').append(node('p',`${data.recent.skipped} unreadable or malformed reports skipped.`,'warn'));
  const s=data.server;for(const [label,value] of [['CPU',s.cpu_percent == null ? 'N/A' : s.cpu_percent+'%'],['RAM used / total',bytes(s.ram_used)+' / '+bytes(s.ram_total)],['Root disk used / total',bytes(s.disk_used)+' / '+bytes(s.disk_total)],['Uptime',duration(s.uptime_seconds)],['Load 1 / 5 / 15m',s.load ? s.load.map(v=>v.toFixed(2)).join(' / ') : 'N/A'],['CPU temperature',s.temperature_c == null ? 'N/A' : s.temperature_c.toFixed(1)+' °C']]) row(el('server'),label,value);
  for(const s of data.services) {row(el('services'),s.name,s.status,tone(s.status));el('services').append(node('p',s.source,'muted'));}
}
async function refresh(){try{const response=await fetch('/api/status',{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!response.ok)throw Error('status');const data=await response.json();render(data);el('connection').textContent='● LIVE · snapshot '+new Date(data.timestamp).toLocaleString();el('connection').className='good';}catch(_){el('connection').textContent='⚠ Refresh failed — displayed data may be stale. Retrying in 10s.';el('connection').className='warn';}finally{setTimeout(refresh,10000);}}
refresh();
