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
/** Server clock from the last snapshot — retry countdowns use it, not ours. */
let serverNow = Date.now() / 1000;

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
  serverNow = state.now || Date.now() / 1000;
  renderCapabilities(serverSettings);
  renderSettings(serverSettings.schema, serverSettings.values);
  renderDetections(state.detections || []);
  renderJobs(state.jobs || []);
}

/* ---------------------------------------------------------------- settings */

let settingsBuilt = false;
/** Edits made but not yet saved: key -> value. Also shields those fields from
 *  being overwritten by the state broadcasts arriving 4x a second. */
const pendingSettings = new Map();

/**
 * The form is generated from the server's schema, so a new setting appears here
 * automatically — no changes needed in this file.
 */
function renderSettings(schema, values) {
  if (!schema || !values) return;

  if (!settingsBuilt) {
    const groups = [];
    schema.forEach((knob) => {
      let group = groups.find((g) => g.name === knob.group);
      if (!group) groups.push((group = { name: knob.group, knobs: [] }));
      group.knobs.push(knob);
    });

    $('#settings-form').innerHTML = groups.map((group) => `
      <div class="settings-group">
        <h3>${esc(group.name)}</h3>
        ${group.knobs.map((knob) => `
          <div class="setting ${knob.deferred ? 'deferred' : ''}">
            <span class="setting-label">${esc(knob.label)}</span>
            <span class="setting-control">
              ${knob.kind === 'bool'
                ? `<input type="checkbox" data-key="${esc(knob.key)}" />`
                : knob.kind === 'path'
                ? `<input type="text" class="path-input" data-key="${esc(knob.key)}"
                     spellcheck="false" placeholder="/path/to/folder" />`
                : `<input type="number" data-key="${esc(knob.key)}"
                     min="${knob.minimum ?? ''}" max="${knob.maximum ?? ''}" step="1" />
                   <span class="setting-unit">${esc(knob.unit || '')}</span>`}
            </span>
            <span class="setting-help">${esc(knob.help || '')}</span>
          </div>`).join('')}
      </div>`).join('');

    $('#settings-form').querySelectorAll('[data-key]').forEach((input) => {
      const event = input.type === 'checkbox' ? 'change' : 'input';
      input.addEventListener(event, () => noteChange(input));
      input.addEventListener('keydown', (keyEvent) => {
        if (keyEvent.key === 'Enter') $('#save-settings').click();
      });
    });
    settingsBuilt = true;
  }

  // Leave edited fields alone; everything else follows the server.
  $('#settings-form').querySelectorAll('[data-key]').forEach((input) => {
    const key = input.dataset.key;
    if (pendingSettings.has(key) || input === document.activeElement) return;
    const value = values[key];
    if (value === undefined) return;
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else if (input.value !== String(value)) input.value = value;
  });
}

function readInput(input) {
  if (input.type === 'checkbox') return input.checked;
  if (input.type === 'number') return Number(input.value);
  return input.value.trim();
}

function noteChange(input) {
  const key = input.dataset.key;
  const saved = serverSettings.values?.[key];
  const value = readInput(input);
  // Typing a value back to what it already was is not a change.
  if (String(value) === String(saved)) pendingSettings.delete(key);
  else pendingSettings.set(key, value);
  refreshSettingsButtons();
}

function refreshSettingsButtons() {
  const dirty = pendingSettings.size > 0;
  $('#save-settings').disabled = !dirty;
  $('#discard-settings').disabled = !dirty;
  $('#save-settings').textContent = dirty
    ? `Save ${pendingSettings.size} change${pendingSettings.size > 1 ? 's' : ''}`
    : 'Save settings';
}

async function saveSettings() {
  if (!pendingSettings.size) return;
  const payload = Object.fromEntries(pendingSettings);
  const count = Object.keys(payload).length;
  $('#save-settings').disabled = true;

  try {
    const result = await api('/api/settings', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    // The server clamps and normalises (out-of-range numbers, resolved paths),
    // so redraw from what it actually stored rather than what was typed.
    pendingSettings.clear();
    serverSettings.values = result.values;
    $('#settings-form').querySelectorAll('[data-key]').forEach((input) => {
      const stored = result.values?.[input.dataset.key];
      if (stored === undefined) return;
      if (input.type === 'checkbox') input.checked = Boolean(stored);
      else input.value = stored;
    });
    flashSaved(`Saved ${count} setting${count > 1 ? 's' : ''}`);
  } catch (error) {
    // Nothing was stored, so hold on to the edits for another attempt.
    toast(error.message, true);
  } finally {
    refreshSettingsButtons();
  }
}

function discardSettings() {
  pendingSettings.clear();
  renderSettings(serverSettings.schema, serverSettings.values);
  refreshSettingsButtons();
}

let savedTimer;
function flashSaved(message = 'Saved') {
  const el = $('#settings-saved');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

function renderCapabilities(settings) {
  const pills = [
    ['Browser capture', settings.sniffer],
    ['ffmpeg', settings.ffmpeg],
  ].map(([label, on]) =>
    `<span class="pill ${on ? 'on' : 'off'}">${esc(label)}: ${on ? 'ready' : 'missing'}</span>`);
  pills.push(
    `<span class="pill" title="Where finished files are written — change it in Settings">` +
    `📁 ${esc(settings.download_dir || '')}</span>`);
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
      ${detection.subfolder
        ? `<span class="tag folder" title="Saves into this subfolder">📁 ${esc(detection.subfolder)}</span>` : ''}
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
          // Captured when this batch was submitted, not read from the box now.
          subfolder: detection.subfolder || null,
          items: chosen.map((stream) => ({ stream })),
        }),
      });
      // Stage 2 -> 3: it is downloading now, so it leaves the detection list.
      local.delete(detection.url);
      await api(`/api/detections?url=${encodeURIComponent(detection.url)}`, { method: 'DELETE' })
        .catch(() => { /* the card is gone from the UI either way */ });
      toast(`Downloading ${chosen.length} file${chosen.length > 1 ? 's' : ''}`);
    } catch (error) {
      toast(error.message, true);
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

  if (job.status === 'done' && job.verified) bits.push(`✓ ${job.verify_note}`);

  const maxRetries = serverSettings.values?.max_retries;
  if (job.retry_count) {
    bits.push(`retry ${job.retry_count}${maxRetries ? ` of ${maxRetries}` : ''}`);
  }
  if (job.retry_at) {
    bits.push(`next attempt in ${Math.max(0, Math.round(job.retry_at - serverNow))}s`);
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
      ${job.retry_at ? '<button class="tiny cancel">Stop retrying</button>' : ''}
      ${job.status === 'error' || job.status === 'cancelled'
        ? '<button class="tiny retry">Retry now</button>' : ''}
      ${(job.status === 'error' || job.status === 'cancelled') && job.page_url
        ? '<button class="tiny redetect" title="Scan the page again and pick a stream">Back to detect</button>' : ''}
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
  // Stage 3 -> 2: drop the failed job and put its page back up for detection.
  node.querySelector('.redetect')?.addEventListener('click', async () => {
    try {
      await api(`/api/jobs/${job.id}/redetect`, { method: 'POST' });
      toast('Scanning the page again');
    } catch (error) {
      toast(error.message, true);
    }
  });
}

/* ----------------------------------------------------------------- actions */

/**
 * Stage 1 -> 2. The URLs leave the input box and become detection cards, so the
 * box is emptied on submit; the subfolder travels with the batch because it is
 * needed later, at the download stage.
 */
async function startDetection(urls, { clearInputs = false } = {}) {
  const quick = $('#quick').checked;
  const subfolder = $('#subfolder').value.trim() || null;
  const button = $('#detect');
  button.disabled = true;
  $('#detect-status').textContent = `Scanning ${urls.length} page${urls.length > 1 ? 's' : ''}…`;
  try {
    await api('/api/detect', {
      method: 'POST',
      body: JSON.stringify({ urls, quick, subfolder }),
    });
    if (clearInputs) {
      $('#urls').value = '';
      $('#subfolder').value = '';
    }
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
  startDetection(urls, { clearInputs: true });
});

$('#urls').addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') $('#detect').click();
});

$('#clear-finished').addEventListener('click', () =>
  api('/api/queue/clear', { method: 'POST' }).catch((e) => toast(e.message, true)));

$('#save-settings').addEventListener('click', saveSettings);
$('#discard-settings').addEventListener('click', discardSettings);

$('#reset-settings').addEventListener('click', async () => {
  if (!confirm('Reset every setting to its default?')) return;
  try {
    const result = await api('/api/settings/reset', { method: 'POST' });
    pendingSettings.clear();
    serverSettings.values = result.values;
    renderSettings(result.schema, result.values);
    refreshSettingsButtons();
    flashSaved('Reset to defaults');
  } catch (error) {
    toast(error.message, true);
  }
});

// Losing typed-but-unsaved settings to a stray tab close is a bad surprise.
window.addEventListener('beforeunload', (event) => {
  if (!pendingSettings.size) return;
  event.preventDefault();
  event.returnValue = '';
});

$('#clear-detections').addEventListener('click', async () => {
  local.clear();
  cardKeys.clear();
  await api('/api/detections', { method: 'DELETE' }).catch((e) => toast(e.message, true));
});

connect();
