'use strict';

/* ---------------------------------------------------------------- utilities */

const $ = (sel) => document.querySelector(sel);
const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (ch) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

const humanSize = (bytes) => {
  if (!bytes && bytes !== 0) return '';
  let value = bytes;
  for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (value < 1024 || unit === 'TB') return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${unit}`;
    value /= 1024;
  }
  return '';
};

const clock = (seconds) => {
  if (!seconds || seconds < 0) return '';
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
};

/** Mirrors app/naming.py so the preview matches the file that gets written. */
const sanitize = (title) =>
  (title || '').replace(/[<>:"/\\|?*\x00-\x1f]/g, ' ').replace(/\s+/g, ' ').replace(/^[\s.]+|[\s.]+$/g, '')
    .slice(0, 150) || 'video';

let toastTimer;
function toast(message, bad = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = bad ? 'toast bad' : 'toast';
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3800);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* keep default */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

/* ------------------------------------------------------------ client state */

/** Per-URL UI state that must survive the 4x/second state broadcasts. */
const local = new Map();          // url -> { title, selected:Set<streamId>, touchedTitle:boolean }
const cardKeys = new Map();       // url -> signature of the last render
const jobNodes = new Map();       // jobId -> element
let serverSettings = {};

function localFor(url, detection) {
  let entry = local.get(url);
  if (!entry) {
    entry = { title: detection.title || '', selected: new Set(), touchedTitle: false };
    local.set(url, entry);
  }
  if (!entry.touchedTitle && detection.title && entry.title !== detection.title) {
    entry.title = detection.title;
  }
  return entry;
}

/* ------------------------------------------------------------- websocket */

let socket;
let reconnectDelay = 500;

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${scheme}://${location.host}/ws`);

  socket.onopen = () => {
    reconnectDelay = 500;
    setInterval(() => socket.readyState === WebSocket.OPEN && socket.send('ping'), 25000);
  };
  socket.onmessage = (event) => render(JSON.parse(event.data));
  socket.onclose = () => {
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 8000);
  };
}

/* --------------------------------------------------------------- rendering */

function render(state) {
  serverSettings = state.settings || {};
  renderCapabilities(serverSettings);
  renderDetections(state.detections || []);
  renderJobs(state.jobs || []);
}

function renderCapabilities(settings) {
  const pills = [
    ['Browser capture', settings.sniffer],
    ['ffmpeg', settings.ffmpeg],
  ].map(([label, on]) =>
    `<span class="pill ${on ? 'on' : 'off'}">${esc(label)}: ${on ? 'ready' : 'missing'}</span>`);
  pills.push(`<span class="pill" title="Where finished files are written">📁 ${esc(settings.download_dir || '')}</span>`);
  $('#capabilities').innerHTML = pills.join('');
}

function renderDetections(detections) {
  const panel = $('#detections-panel');
  const container = $('#detections');
  panel.hidden = detections.length === 0;

  const withVideo = detections.filter((d) => d.status === 'ok').length;
  const without = detections.filter((d) => d.status === 'none').length;
  const busy = detections.filter((d) => d.status === 'queued' || d.status === 'running').length;
  $('#detections-count').textContent = [
    busy ? `${busy} scanning` : '',
    `${withVideo} with video`,
    without ? `${without} with none` : '',
  ].filter(Boolean).join(' · ');

  const seen = new Set();
  detections
    .slice()
    .sort((a, b) => (b.detected_at || 0) - (a.detected_at || 0))
    .forEach((detection) => {
      seen.add(detection.url);
      const signature = `${detection.status}|${detection.detected_at}|${(detection.streams || []).length}`;
      let card = container.querySelector(`[data-url="${CSS.escape(detection.url)}"]`);

      if (!card || cardKeys.get(detection.url) !== signature) {
        const fresh = buildCard(detection);
        if (card) card.replaceWith(fresh); else container.appendChild(fresh);
        cardKeys.set(detection.url, signature);
      }
    });

  container.querySelectorAll('[data-url]').forEach((node) => {
    if (!seen.has(node.dataset.url)) {
      cardKeys.delete(node.dataset.url);
      node.remove();
    }
  });
}

function buildCard(detection) {
  const state = localFor(detection.url, detection);
  const streams = detection.streams || [];
  const card = document.createElement('div');
  card.className = `card ${detection.status}`;
  card.dataset.url = detection.url;

  const badges = {
    queued: 'Queued', running: 'Scanning…', ok: 'Video found',
    none: 'No video detected', error: 'Failed',
  };

  card.innerHTML = `
    <div class="card-head">
      <div class="card-title">
        <input type="text" class="title-input" value="${esc(state.title || detection.title || '')}"
               placeholder="File name (from the page title)" />
        <a class="card-url" href="${esc(detection.url)}" target="_blank" rel="noreferrer">${esc(detection.url)}</a>
      </div>
      <span class="badge ${detection.status}">${badges[detection.status] || detection.status}</span>
    </div>
    ${streams.length ? `<div class="streams">${streams.map(streamRow).join('')}</div>` : ''}
    ${detection.notes?.length
      ? `<div class="notes">${detection.notes.map((n) => `<div>${esc(n)}</div>`).join('')}</div>` : ''}
    <div class="card-foot">
      ${streams.length ? '<button class="primary download">Download selected</button>' : ''}
      <button class="ghost tiny rescan">Detect again</button>
      <button class="ghost tiny dismiss">Dismiss</button>
      <span class="preview"></span>
    </div>`;

  // Pre-select the recommended stream the first time we see this page.
  if (!state.selected.size) {
    streams.filter((s) => s.recommended).forEach((s) => state.selected.add(s.id));
  }
  card.querySelectorAll('.stream input').forEach((box) => {
    box.checked = state.selected.has(box.value);
  });

  wireCard(card, detection, state);
  updatePreview(card, detection, state);
  return card;
}

function streamRow(stream) {
  const meta = [];
  if (stream.width && stream.height) meta.push(`${stream.width}×${stream.height}`);
  if (stream.codecs) meta.push(stream.codecs);
  if (stream.duration) meta.push(clock(stream.duration));
  if (stream.filesize) meta.push(humanSize(stream.filesize));
  if (stream.audio_url) meta.push('+ separate audio');
  meta.push(`via ${stream.source}`);

  return `
    <label class="stream">
      <input type="checkbox" value="${esc(stream.id)}" />
      <span class="stream-grow">
        <span class="stream-label">${esc(stream.label || stream.url)}</span>
        <span class="stream-meta"> ${esc(meta.join(' · '))}</span>
      </span>
      <span class="tag ${esc(stream.kind)}">${esc(stream.kind)}</span>
    </label>`;
}

function wireCard(card, detection, state) {
  const titleInput = card.querySelector('.title-input');
  titleInput.addEventListener('input', () => {
    state.title = titleInput.value;
    state.touchedTitle = true;
    updatePreview(card, detection, state);
  });

  card.querySelectorAll('.stream input').forEach((box) => {
    box.addEventListener('change', () => {
      if (box.checked) state.selected.add(box.value); else state.selected.delete(box.value);
      updatePreview(card, detection, state);
    });
  });

  card.querySelector('.download')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const chosen = (detection.streams || []).filter((s) => state.selected.has(s.id));
    if (!chosen.length) return toast('Select at least one stream first', true);

    button.disabled = true;
    try {
      await api('/api/download', {
        method: 'POST',
        body: JSON.stringify({
          page_url: detection.url,
          title: state.title || detection.title || detection.url,
          subfolder: $('#subfolder').value.trim() || null,
          items: chosen.map((stream) => ({ stream })),
        }),
      });
      toast(`Queued ${chosen.length} download${chosen.length > 1 ? 's' : ''}`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  card.querySelector('.rescan').addEventListener('click', () => {
    local.delete(detection.url);
    startDetection([detection.url]);
  });

  card.querySelector('.dismiss').addEventListener('click', async () => {
    local.delete(detection.url);
    await api(`/api/detections?url=${encodeURIComponent(detection.url)}`, { method: 'DELETE' })
      .catch((error) => toast(error.message, true));
  });
}

function updatePreview(card, detection, state) {
  const preview = card.querySelector('.preview');
  if (!preview) return;
  const count = state.selected.size;
  if (!count) { preview.textContent = ''; return; }

  const stem = sanitize(state.title || detection.title || '');
  const chosen = (detection.streams || []).filter((s) => state.selected.has(s.id));
  const names = chosen.map((stream, index) => {
    const base = count > 1 ? `${stem} ${index + 1}` : stem;
    return `${base}.${stream.container || 'mp4'}`;
  });
  preview.textContent = `→ ${names.slice(0, 3).join(', ')}${names.length > 3 ? `, +${names.length - 3} more` : ''}`;
}

/* ------------------------------------------------------------------- queue */

function renderJobs(jobs) {
  const container = $('#queue');
  const active = jobs.filter((j) => j.status === 'running' || j.status === 'queued').length;
  $('#queue-count').textContent = jobs.length
    ? `${jobs.length} total${active ? ` · ${active} active` : ''}` : '';

  if (!jobs.length) {
    container.innerHTML = '<div class="empty">Nothing queued yet — detect a page above to get started.</div>';
    jobNodes.clear();
    return;
  }
  if (container.querySelector('.empty')) container.innerHTML = '';

  const seen = new Set();
  jobs.forEach((job) => {
    seen.add(job.id);
    let node = jobNodes.get(job.id);
    if (!node) {
      node = document.createElement('div');
      node.className = 'job';
      node.dataset.id = job.id;
      jobNodes.set(job.id, node);
      container.appendChild(node);
    }
    paintJob(node, job);
  });

  jobNodes.forEach((node, id) => {
    if (!seen.has(id)) { node.remove(); jobNodes.delete(id); }
  });
}

function paintJob(node, job) {
  const statusText = {
    queued: 'Queued', running: 'Downloading', done: 'Done',
    error: 'Failed', cancelled: 'Cancelled',
  }[job.status] || job.status;

  const bits = [statusText];
  if (job.status === 'running') {
    if (job.percent != null) bits.push(`${job.percent.toFixed(1)}%`);
    if (job.downloaded_bytes) {
      bits.push(job.total_bytes
        ? `${humanSize(job.downloaded_bytes)} / ${humanSize(job.total_bytes)}`
        : humanSize(job.downloaded_bytes));
    }
    if (job.speed) bits.push(job.speed);
    if (job.eta) bits.push(`ETA ${job.eta}`);
  } else if (job.message) {
    bits.push(job.message);
  }
  bits.push(job.stream?.label || '');

  const indeterminate = job.status === 'running' && job.percent == null;
  const width = job.status === 'done' ? 100 : (job.percent || 0);
  const barClass = ['bar', job.status === 'done' ? 'done' : '',
                    job.status === 'error' ? 'error' : '',
                    indeterminate ? 'indeterminate' : ''].filter(Boolean).join(' ');

  const relative = job.output_path && serverSettings.download_dir
    ? job.output_path.replace(`${serverSettings.download_dir}/`, '')
    : '';
  const fileHref = relative ? `/files/${relative.split('/').map(encodeURIComponent).join('/')}` : '';

  node.innerHTML = `
    <div>
      <div class="job-name">${esc(job.filename)}</div>
      <div class="job-sub">${esc(bits.filter(Boolean).join(' · '))}</div>
    </div>
    <div class="job-actions">
      ${job.status === 'done' && fileHref
        ? `<a class="tiny" href="${esc(fileHref)}" target="_blank" rel="noreferrer"><button class="tiny">Open</button></a>
           <button class="tiny reveal">Show in folder</button>` : ''}
      ${job.status === 'running' || job.status === 'queued'
        ? '<button class="tiny cancel">Cancel</button>' : ''}
      ${job.status === 'error' || job.status === 'cancelled'
        ? '<button class="tiny retry">Retry</button>' : ''}
      ${job.status !== 'running' ? '<button class="ghost tiny remove">✕</button>' : ''}
    </div>
    <div class="${barClass}"><span style="width:${width}%"></span></div>`;

  node.querySelector('.cancel')?.addEventListener('click', () =>
    api(`/api/jobs/${job.id}/cancel`, { method: 'POST' }).catch((e) => toast(e.message, true)));
  node.querySelector('.retry')?.addEventListener('click', () =>
    api(`/api/jobs/${job.id}/retry`, { method: 'POST' }).catch((e) => toast(e.message, true)));
  node.querySelector('.remove')?.addEventListener('click', () =>
    api(`/api/jobs/${job.id}`, { method: 'DELETE' }).catch((e) => toast(e.message, true)));
  node.querySelector('.reveal')?.addEventListener('click', () =>
    api('/api/reveal', { method: 'POST', body: JSON.stringify({ path: job.output_path }) })
      .catch((e) => toast(e.message, true)));
}

/* ----------------------------------------------------------------- actions */

async function startDetection(urls) {
  const quick = $('#quick').checked;
  const button = $('#detect');
  button.disabled = true;
  $('#detect-status').textContent = `Scanning ${urls.length} page${urls.length > 1 ? 's' : ''}…`;
  try {
    await api('/api/detect', { method: 'POST', body: JSON.stringify({ urls, quick }) });
    $('#detect-status').textContent = quick
      ? 'Extractor-only scan running.'
      : 'Loading each page in a headless browser and watching its network traffic…';
  } catch (error) {
    $('#detect-status').textContent = '';
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

$('#detect').addEventListener('click', () => {
  const urls = $('#urls').value.split('\n').map((line) => line.trim()).filter(Boolean);
  if (!urls.length) return toast('Paste at least one URL', true);
  startDetection(urls);
});

$('#urls').addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') $('#detect').click();
});

$('#clear-finished').addEventListener('click', () =>
  api('/api/queue/clear', { method: 'POST' }).catch((e) => toast(e.message, true)));

$('#clear-detections').addEventListener('click', async () => {
  local.clear();
  cardKeys.clear();
  await api('/api/detections', { method: 'DELETE' }).catch((e) => toast(e.message, true));
});

connect();
