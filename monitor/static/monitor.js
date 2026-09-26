'use strict';
const el = id => document.getElementById(id);
const text = v => v === null || v === undefined ? 'N/A' : String(v);
const number = v => v === null || v === undefined ? 'N/A' : v.toLocaleString();
const bytes = v => v == null ? 'N/A' : (v / 1073741824).toFixed(1) + ' GiB';
const duration = v => v == null ? 'N/A' : [Math.floor(v/3600),Math.floor(v%3600/60),Math.floor(v%60)].map(x=>String(x).padStart(2,'0')).join(':');
const percent = (used,total) => used == null || !total ? null : Math.min(100,Math.max(0,100*used/total));
const tone = v => ['running','active','complete','VERIFIED'].includes(v) ? 'good' : ['ERROR','unhealthy','failed'].includes(v) ? 'error' : ['REVIEW','GROUP_REVIEW','interrupted','incomplete','circuit-breaker','stopped','starting'].includes(v) ? 'warn' : '';
// Presentation only: API/report statuses and diagnostic text stay unchanged.
const resultLabel = status => ({ERROR:'UNRESOLVED',GROUP_REVIEW:'GROUP REVIEW',NOT_FOUND:'NOT FOUND'}[status] || status);
const companyLabel = company => company ? [company.id,company.name].filter(v=>v!=null && v!=='').join(' · ') || null : null;
const localTime = value => {if(!value)return 'N/A';const date=new Date(value);return Number.isNaN(date.valueOf()) ? 'N/A' : date.toLocaleString();};
function node(tag,value,cls=''){const n=document.createElement(tag);n.textContent=text(value);n.className=cls;return n;}
function row(parent,label,value,cls=''){const r=node('div','','row'),n=node('span',value,'value '+cls);r.append(node('span',label),n);parent.append(r);return n;}
function bar(parent,value,label){if(value==null)return;const n=document.createElement('progress');n.max=100;n.value=value;n.setAttribute('aria-label',label);parent.append(n);}
const previous = new Map();
function changed(id,value,render){const key=JSON.stringify(value);if(previous.get(id)===key)return;previous.set(id,key);const target=el(id),scroll=[target.scrollLeft,target.scrollTop];target.replaceChildren();render(target);target.scrollLeft=scroll[0];target.scrollTop=scroll[1];}
let runtimes = new Map();
const easterEggs = window.DastabaseEasterEggs?.create(el('easter-egg'));
function renderStatus(data){
  const d=data.database;
  changed('database',d,box=>{
    row(box,'Companies',number(d.total));row(box,'Processed',`${number(d.processed)} / ${number(d.total)}`);
    row(box,'Remaining',number(d.remaining));row(box,'Processed %',d.percent == null ? 'N/A' : d.percent+'%');
    if(d.note)box.append(node('p',d.note,'warn'));
  });
  el('progress').value=d.percent || 0;
  changed('results',[d.statuses,d.email_companies,d.emails,d.email_note,d.other_statuses],box=>{
    const labels={VERIFIED:'Verified websites',REVIEW:'Review',GROUP_REVIEW:'Group review',NOT_FOUND:'Not found',ERROR:'Unresolved'};
    for(const [status,count] of Object.entries(d.statuses))row(box,labels[status]||status,number(count),tone(status));
    if(d.other_statuses)row(box,'Other / legacy status',number(d.other_statuses),'warn');
    row(box,'Companies with usable emails',number(d.email_companies));row(box,'Usable email addresses',number(d.emails));
    if(d.email_note)box.append(node('p',d.email_note,'warn'));
  });
  changed('active',data.active,box=>{
    runtimes=new Map();
    if(!data.active.jobs.length)box.append(node('p',data.active.available ? 'No active worker observed.' : data.active.note,'muted'));
    if(data.active.inaccessible_processes)box.append(node('p','Process visibility is partial.','warn'));
    for(const j of data.active.jobs){
      const job=node('div','','job');job.append(node('div','● '+j.job_type,'good job-title'));
      row(job,'Batch',j.report);row(job,'Companies',`${number(j.processed)} / ${number(j.selected)}`);
      row(job,'Remaining',number(j.remaining));row(job,'Batch progress',j.percent == null ? 'N/A' : j.percent+'%');bar(job,j.percent,'Batch progress');
      row(job,'Current',companyLabel(j.current_company));
      const last=j.last_completed;row(job,'Last',last ? [last.company_id,last.company_name,resultLabel(last.status)].filter(v=>v!=null).join(' · ') : null,last ? tone(last.status) : '');
      row(job,'PID',j.pid);runtimes.set(j.identity,row(job,'Runtime',duration(j.runtime_seconds)));
      row(job,'Circuit breaker',j.circuit_breaker == null ? 'N/A' : j.circuit_breaker ? 'TRIGGERED' : 'Not triggered',j.circuit_breaker ? 'warn' : '');box.append(job);
    }
  });
  changed('workload',data.workload,box=>{
    const w=data.workload;
    if(w.state==='idle'){box.append(node('p','No active batch','muted'));row(box,'Unprocessed companies',number(w.database_remaining));}
    else{if(w.state==='unknown')box.append(node('p','Active batch visibility unavailable','warn'));
      row(box,w.active_batches>1 ? 'Active batches remaining' : 'Current batch remaining',number(w.batch_remaining));
      row(box,'Database remaining',number(w.database_remaining));row(box,'Remaining after batch (est.)',number(w.remaining_after_batch));
      if(w.active_batches>1)row(box,'Active batches',w.active_batches);
    }
  });
  changed('activity',data.activity.entries,box=>{
    if(!data.activity.entries.length)box.append(node('p','No completed companies observed yet.','muted'));
    for(const event of data.activity.entries){const line=node('div','','event');
      line.append(node('span',event.status==='VERIFIED' ? '✓' : event.status==='ERROR' ? '×' : '·',tone(event.status)),node('span',event.company_id,'event-id'),node('span',event.company_name || 'Name unavailable','event-name'));
      if(event.status)line.append(node('span',resultLabel(event.status),tone(event.status)));
      if(event.domain)line.append(node('span',event.domain,'muted'));
      if(event.usable_emails!=null)line.append(node('span',`${event.usable_emails} emails`,'muted'));
      line.title=event.report;box.append(line);
    }
  });
  el('activity-note').textContent=(data.activity.source==='active' ? 'Active runner · ' : 'Recent runner · ')+data.activity.note;
  changed('recent',data.recent,box=>{
    const table=document.createElement('table'),head=document.createElement('tr');for(const h of ['REPORT','STATE','DONE / SELECTED','TIMESTAMP'])head.append(node('th',h));table.append(head);
    for(const j of data.recent.jobs){const tr=document.createElement('tr');tr.append(node('td',j.name),node('td',resultLabel(j.status),tone(j.status)),node('td',`${j.processed} / ${j.selected}`),node('td',localTime(j.timestamp)));table.append(tr);}box.append(table);
    if(!data.recent.jobs.length)box.append(node('p',data.recent.note || 'No runner reports found.','muted'));
    if(data.recent.skipped)box.append(node('p',`${data.recent.skipped} unreadable or malformed reports skipped.`,'warn'));
  });
  changed('services',data.services,box=>{for(const s of data.services){row(box,s.name,s.status,tone(s.status));box.append(node('p',s.source,'muted'));}});
  // Optional personality must never turn a successful operations refresh into a failure.
  try { easterEggs?.observe(data); } catch (_) { /* Operational UI remains independent. */ }
}
function renderLive(data){
  const s=data.server;
  changed('server',s,box=>{
    row(box,'CPU',s.cpu_percent == null ? 'N/A' : s.cpu_percent+'%');bar(box,s.cpu_percent,'CPU utilization');
    const ram=percent(s.ram_used,s.ram_total),disk=percent(s.disk_used,s.disk_total);
    row(box,'RAM',`${ram==null ? 'N/A' : ram.toFixed(1)+'%'} · ${bytes(s.ram_used)} / ${bytes(s.ram_total)}`);bar(box,ram,'RAM usage');
    row(box,'Root disk',`${disk==null ? 'N/A' : disk.toFixed(1)+'%'} · ${bytes(s.disk_used)} / ${bytes(s.disk_total)}`);bar(box,disk,'Root disk usage');
    row(box,'Uptime',duration(s.uptime_seconds));row(box,'Load 1 / 5 / 15m',s.load ? s.load.map(v=>v.toFixed(2)).join(' / ') : 'N/A');
    row(box,'CPU temperature',s.temperature_c == null ? 'N/A' : s.temperature_c.toFixed(1)+' °C');
  });
  const jobs=new Map(data.processes.jobs.map(j=>[j.identity,j]));
  for(const [identity,n] of runtimes){const j=jobs.get(identity);n.textContent=j ? duration(j.runtime_seconds) : data.processes.available ? 'No longer observed' : 'N/A';}
}
async function poll(url,interval,render,id,label){
  const started=performance.now();
  try{const response=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!response.ok)throw Error('status');const data=await response.json();render(data);el(id).textContent=`● ${label} · ${new Date(data.timestamp).toLocaleTimeString()}`;el(id).className='good';}
  catch(_){el(id).textContent=`⚠ ${label} unavailable — displayed data may be stale`;el(id).className='warn';}
  // Separate sequential loops: no overlapping requests or catch-up bursts.
  setTimeout(()=>poll(url,interval,render,id,label),Math.max(100,interval-(performance.now()-started)));
}
poll('/api/status',2500,renderStatus,'connection','Operations');
poll('/api/live',1000,renderLive,'live-connection','System');
