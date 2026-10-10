'use strict';

const upload = document.querySelector('[data-upload-form]');
if (upload) {
  const target = upload.querySelector('[data-drop-area]');
  const input = upload.querySelector('[data-file-input]');
  const empty = upload.querySelector('[data-upload-empty]');
  const selected = upload.querySelector('[data-upload-selected]');
  const filename = upload.querySelector('[data-file-name]');
  const continueButton = upload.querySelector('[data-upload-continue]');
  const showSelection = () => {
    const file = input?.files?.[0];
    if (!file) return;
    if (filename) filename.textContent = file.name;
    if (empty) empty.hidden = true;
    if (selected) selected.hidden = false;
    if (continueButton) {
      continueButton.disabled = false;
      continueButton.hidden = false;
    }
  };
  for (const name of ['dragenter', 'dragover']) {
    target.addEventListener(name, event => {
      event.preventDefault();
      target.classList.add('dragging');
    });
  }
  for (const name of ['dragleave', 'drop']) {
    target.addEventListener(name, event => {
      event.preventDefault();
      target.classList.remove('dragging');
    });
  }
  target.addEventListener('drop', event => {
    if (input && event.dataTransfer.files.length) {
      input.files = event.dataTransfer.files;
      showSelection();
    }
  });
  input?.addEventListener('change', showSelection);
}

const progressRoot = document.querySelector('[data-job-progress]');
function setText(selector, value) {
  const node = progressRoot?.querySelector(selector);
  if (node) node.textContent = value;
}
function renderJob(job) {
  if (!progressRoot || !job) return;
  progressRoot.dataset.state = job.state;
  progressRoot.dataset.activity = job.activity;
  setText('[data-stage]', job.stage);
  setText('[data-count]', `${job.completed_units ?? 0} / ${job.total_units ?? 0}`);
  setText('[data-matched]', job.matched_rows);
  setText('[data-review]', job.review_rows);
  setText('[data-incomplete]', job.incomplete_rows ?? 0);
  const bar = progressRoot.querySelector('[data-progress]');
  const count = progressRoot.querySelector('[data-count]');
  if (bar) {
    bar.hidden = !job.progress_determinate;
    bar.value = job.percent ?? 0;
  }
  if (count) count.hidden = !job.progress_determinate;
  const complete = progressRoot.querySelector('[data-complete]');
  const failed = progressRoot.querySelector('[data-failed]');
  const working = progressRoot.querySelector('[data-working-status]');
  const completeness = progressRoot.querySelector('[data-completeness]');
  const activityMessage = progressRoot.querySelector('[data-activity-message]');
  if (complete) complete.hidden = !['done', 'partial'].includes(job.state);
  if (failed) failed.hidden = job.state !== 'failed';
  if (working) working.hidden = job.state !== 'working';
  if (completeness) completeness.hidden = !job.incomplete_rows;
  if (activityMessage) activityMessage.textContent = {
    queued: 'Waiting to start…',
    delayed: 'Processing is temporarily delayed…',
    active: 'Working on it…',
  }[job.activity] || 'Working on it…';
}
async function poll() {
  if (!progressRoot) return;
  try {
    const response = await fetch(progressRoot.dataset.progressUrl, {
      cache: 'no-store', credentials: 'same-origin', signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) {
      window.setTimeout(poll, 3000);
      return;
    }
    const payload = await response.json();
    renderJob(payload.job);
    if (payload.job.state === 'working') window.setTimeout(poll, 1500);
  } catch (_error) {
    window.setTimeout(poll, 3000);
  }
}
if (progressRoot) poll();
