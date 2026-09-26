/* Isolated, session-memory-only decoration. No I/O or recurring timers. */
(() => {
  'use strict';
  const messages = Object.freeze({
    space: '🚀 deep space enrichment in progress',
    cookie: '🍪 cookie break avoided. still working.',
    sail: '⛵ favorable winds detected',
    dove: '🕊',
    skis: '🎿 excellent conditions. continuing downhill.',
    dinosaur: '🦖 dinosaurs have entered the database'
  });
  function create(target, options = {}) {
    const now = options.now || (() => performance.now());
    const random = options.random || Math.random;
    const schedule = options.schedule || ((fn, ms) => window.setTimeout(fn, ms));
    const visible = options.visible || (() => !document.hidden);
    const batches = new Map();
    let lastShown = -Infinity;
    let lastRare = now();
    let lastCheck = now();
    function show(kind, time) {
      if (!target || !visible() || time-lastShown < 120000) return;
      lastShown = time;
      target.textContent = messages[kind];
      // Exactly one expiration timer per shown message; no animation or focus changes.
      schedule(() => { target.textContent = ''; }, 6000);
    }
    function observe(data) {
      const time = now();
      const jobs = data.active?.jobs || [];
      let milestone = null;
      let advancing = false;
      for (const job of jobs) {
        // Report path is the batch identity across PID changes/resumes. Without it,
        // suppress milestones rather than infer identity from a company ID or PID.
        if (typeof job.report !== 'string' || !job.report ||
            !Number.isInteger(job.processed) || !Number.isInteger(job.selected) ||
            job.processed < 0 || job.processed > job.selected) continue;
        let batch = batches.get(job.report);
        if (!batch) {
          batch = {processed: job.processed, space: false, cookie: false};
          batches.set(job.report, batch);
        }
        if (job.processed === 42 && batch.processed <= 42 && !batch.space) {
          batch.space = true;
          milestone ||= 'space';
        }
        if (batch.processed < 100 && job.processed >= 100 && !batch.cookie) {
          batch.cookie = true;
          milestone ||= 'cookie';
        }
        if (job.processed > batch.processed && job.circuit_breaker === false &&
            job.last_completed?.status !== 'ERROR') advancing = true;
        batch.processed = Math.max(batch.processed, job.processed);
      }
      // A worker can exit between polls. Only a previously observed active batch
      // is eligible; opening a page of historical completed reports never triggers.
      for (const report of data.recent?.jobs || []) {
        const batch = batches.get(report.name);
        if (batch && !batch.cookie && batch.processed < 100 && report.status === 'complete' &&
            report.selected === 100 && report.processed === 100) {
          batch.cookie = true;
          batch.processed = 100;
          milestone ||= 'cookie';
        }
      }
      // Milestones are consumed even if hidden or suppressed by cooldown; never queued.
      if (milestone) { show(milestone, time); return; }
      if (!advancing || data.active?.available !== true || data.active?.inaccessible_processes ||
          !visible() || time-lastCheck < 60000 || time-lastRare < 1200000 || time-lastShown < 120000) return;
      lastCheck = time;
      const draw = random();
      const kind = draw < 0.0005 ? 'dinosaur' : draw < 0.0025 ? 'skis' :
                   draw < 0.0125 ? 'dove' : draw < 0.0225 ? 'sail' : null;
      if (kind) { lastRare = time; show(kind, time); }
    }
    return Object.freeze({observe});
  }
  window.DastabaseEasterEggs = Object.freeze({create});
})();
