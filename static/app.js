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

/**
 * Mirrors app/naming.py so the preview matches the file that gets written.
 * Slash-wrapped lines are regular expressions; everything else is literal.
 */
function compileFilters(raw) {
  const patterns = [];
  for (const line of String(raw || '').split('\n')) {
    const entry = line.trim();
    if (!entry || entry.startsWith('#')) continue;
    try {
      if (entry.length > 2 && entry.startsWith('/') && entry.lastIndexOf('/') > 0) {
        const end = entry.lastIndexOf('/');
        const flags = entry.slice(end + 1).toLowerCase().includes('i') ? 'gi' : 'g';
        patterns.push(new RegExp(entry.slice(1, end), flags));
      } else {
        patterns.push(new RegExp(entry.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'));
      }
    } catch (_) {
      // The server rejects bad patterns on save; ignore them in the preview.
    }
  }
  return patterns;
}

/** Mirrors apply_filters in app/naming.py: longest match wins, order-independent. */
function applyFilters(title, raw) {
  let text = title || '';
  const patterns = compileFilters(raw);
  if (!patterns.length) return text;

  for (let pass = 0; pass < 200; pass += 1) {
    let best = null;
    for (const pattern of patterns) {
      pattern.lastIndex = 0;
      let match;
      while ((match = pattern.exec(text)) !== null) {
        if (match[0].length === 0) { pattern.lastIndex += 1; continue; }
        if (!best || match[0].length > best.length
            || (match[0].length === best.length && match.index < best.start)) {
          best = { start: match.index, length: match[0].length };
        }
      }
    }
    if (!best) break;
    text = `${text.slice(0, best.start)} ${text.slice(best.start + best.length)}`;
  }
  return text;
}

const sanitize = (title) =>
  (title || '').replace(/[<>:"/\\|?*\x00-\x1f]/g, ' ').replace(/\s+/g, ' ').replace(/^[\s.]+|[\s.]+$/g, '')
    .slice(0, 150) || 'video';

/** The filename a title will actually produce, filters included. */
const finalStem = (title) =>
  sanitize(applyFilters(title, pendingSettings.get('title_filters')
    ?? serverSettings.values?.title_filters));

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
/** Detections from the last snapshot, so batch actions know what is on screen. */
let lastDetections = [];
let serverSettings = {};
/** Server clock from the last snapshot — retry countdowns use it, not ours. */
let serverNow = Date.now() / 1000;

function localFor(url, detection) {
  let entry = local.get(url);
  if (!entry) {
    entry = {
      title: detection.title || '',
      selected: new Set(),
      touchedTitle: false,
      // Once someone has chosen, an empty selection is a choice, not a blank slate.
      touchedSelection: false,
    };
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
                : knob.kind === 'lines'
                ? `<textarea class="lines-input" data-key="${esc(knob.key)}" rows="4"
                     spellcheck="false" placeholder="- YouTube&#10;[1080p]&#10;/\\s*\\(\\d{4}\\)/"></textarea>`
                : knob.kind === 'path'
                ? `<input type="text" class="path-input" data-key="${esc(knob.key)}"
                     spellcheck="false" placeholder="/path/to/folder" />`
                : `<input type="number" data-key="${esc(knob.key)}"
                     min="${knob.minimum ?? ''}" max="${knob.maximum ?? ''}" step="1" />
                   <span class="setting-unit">${esc(knob.unit || '')}</span>`}
            </span>
            <span class="setting-help">${esc(knob.help || '')}</span>
            ${knob.kind === 'lines' ? `
              <div class="filter-test">
                <input type="text" id="filter-test-input" spellcheck="false"
                       placeholder="Paste a real title here to see what these patterns do to it" />
                <div class="filter-test-out" id="filter-test-out"></div>
              </div>` : ''}
          </div>`).join('')}
      </div>`).join('');

    $('#settings-form').querySelectorAll('[data-key]').forEach((input) => {
      const event = input.type === 'checkbox' ? 'change' : 'input';
      input.addEventListener(event, () => noteChange(input));
      input.addEventListener('keydown', (keyEvent) => {
        if (keyEvent.key === 'Enter') $('#save-settings').click();
      });
    });
    wireFilterTester();
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

/**
 * Runs a title through the filters server-side and reports which lines matched.
 * Deliberately not evaluated in the browser: the point is to show what the
 * server will really do, so a client/server difference cannot hide the answer.
 */
let filterTestTimer;
function wireFilterTester() {
  const input = $('#filter-test-input');
  const out = $('#filter-test-out');
  if (!input || !out) return;

  const run = async () => {
    const title = input.value;
    if (!title.trim()) { out.innerHTML = ''; return; }
    const filters = pendingSettings.get('title_filters')
      ?? serverSettings.values?.title_filters ?? '';
    try {
      const result = await api('/api/settings/test-title', {
        method: 'POST',
        body: JSON.stringify({ title, filters }),
      });
      const rows = [`<div class="ft-result">→ <b>${esc(result.filename)}</b></div>`];
      if (result.matched.length) {
        rows.push(`<div class="ft-ok">matched: ${result.matched.map(esc).join(' · ')}</div>`);
      }
      if (result.unmatched.length) {
        rows.push(`<div class="ft-bad">did not match: ${result.unmatched.map(esc).join(' · ')}</div>`);
      }
      if (result.invalid.length) {
        rows.push(`<div class="ft-bad">invalid: ${result.invalid.map(esc).join(' · ')}</div>`);
      }
      if (!result.matched.length && result.codepoints) {
        rows.push(`<div class="ft-hint">punctuation in this title: ${esc(result.codepoints)}</div>`);
      }
      out.innerHTML = rows.join('');
    } catch (error) {
      out.innerHTML = `<div class="ft-bad">${esc(error.message)}</div>`;
    }
  };

  input.addEventListener('input', () => {
    clearTimeout(filterTestTimer);
    filterTestTimer = setTimeout(run, 300);
  });
  // Re-run when the patterns change, not just the title.
  $('#settings-form').querySelector('textarea[data-key="title_filters"]')
    ?.addEventListener('input', () => {
      clearTimeout(filterTestTimer);
      filterTestTimer = setTimeout(run, 400);
    });
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

function renderDetections(allDetections) {
  const panel = $('#detections-panel');
  const container = $('#detections');

  // Forget suppressions once the server agrees the card is gone, so a page
  // detected again later still shows up.
  const live = new Set(allDetections.map((d) => d.url));
  justQueued.forEach((url) => { if (!live.has(url)) justQueued.delete(url); });

  // Anything already sent to the queue must not flicker back into view while
  // the server-side removal is still in flight.
  const detections = allDetections.filter((d) => !justQueued.has(d.url));
  lastDetections = detections;
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

  refreshBatchButton();
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
    ${streams.length ? `
      <div class="stream-tools">
        <button class="ghost tiny select-all">Select all</button>
        <button class="ghost tiny select-best">Best of each video</button>
        <button class="ghost tiny select-none">Clear</button>
      </div>
      <div class="streams">${streams.map(streamRow).join('')}</div>` : ''}
    ${detection.notes?.length
      ? `<div class="notes">${detection.notes.map((n) => `<div>${esc(n)}</div>`).join('')}</div>` : ''}
    <div class="card-foot">
      ${streams.length ? '<button class="primary download">Download selected</button>' : ''}
      <button class="ghost tiny rescan">Detect again</button>
      <button class="ghost tiny dismiss">Dismiss</button>
      <span class="preview"></span>
    </div>`;

  // Pre-select every distinct video the first time we see this page. Streams
  // are ordered best-first, so the first of each group is that video's best
  // quality — picking one per group selects all the videos without also
  // queueing the same one at 1080p, 720p and 480p.
  if (!state.selected.size && !state.touchedSelection) {
    const seenGroups = new Set();
    streams.forEach((stream) => {
      if (stream.kind === 'subtitle') return;
      const group = stream.group || stream.url;
      if (seenGroups.has(group)) return;
      seenGroups.add(group);
      state.selected.add(stream.id);
    });
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
      state.touchedSelection = true;
      updatePreview(card, detection, state);
      refreshBatchButton();
    });
  });

  const setSelection = (ids) => {
    state.selected = new Set(ids);
    state.touchedSelection = true;
    card.querySelectorAll('.stream input').forEach((box) => {
      box.checked = state.selected.has(box.value);
    });
    updatePreview(card, detection, state);
    refreshBatchButton();
  };

  card.querySelector('.select-all')?.addEventListener('click', () =>
    setSelection((detection.streams || []).map((s) => s.id)));

  card.querySelector('.select-none')?.addEventListener('click', () => setSelection([]));

  card.querySelector('.select-best')?.addEventListener('click', () => {
    const seenGroups = new Set();
    setSelection((detection.streams || []).filter((stream) => {
      if (stream.kind === 'subtitle') return false;
      const group = stream.group || stream.url;
      if (seenGroups.has(group)) return false;
      seenGroups.add(group);
      return true;
    }).map((s) => s.id));
  });

  card.querySelector('.download')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const count = await queueDetection(detection);
      if (!count) {
        button.disabled = false;
        return toast('Select at least one stream first', true);
      }
      toast(`Downloading ${count} file${count > 1 ? 's' : ''}`);
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

/** Cards taken off screen whose removal the server has not caught up with. */
const justQueued = new Set();

/** Drop a card from the screen immediately, whatever the server says next. */
function removeCard(url) {
  local.delete(url);
  cardKeys.delete(url);
  justQueued.add(url);
  const node = $('#detections').querySelector(`[data-url="${CSS.escape(url)}"]`);
  if (node) node.remove();
}

/**
 * Queue one page's selected streams. Returns how many were queued, 0 if
 * nothing was selected. The card leaves the detection stage on success —
 * removed from the DOM directly rather than waiting on the server round-trip,
 * so a failed cleanup call can never strand a card with a dead button.
 */
async function queueDetection(detection) {
  const state = local.get(detection.url);
  const chosen = (detection.streams || []).filter((s) => state?.selected?.has(s.id));
  if (!chosen.length) return 0;

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

  removeCard(detection.url);
  api(`/api/detections?url=${encodeURIComponent(detection.url)}`, { method: 'DELETE' })
    .catch(() => { /* already gone from the UI; the next snapshot reconciles */ });
  return chosen.length;
}

async function downloadAll() {
  const button = $('#download-all');
  const ready = lastDetections.filter(
    (d) => d.status === 'ok' && local.get(d.url)?.selected?.size,
  );
  if (!ready.length) return toast('Nothing selected to download', true);

  button.disabled = true;
  let files = 0;
  const failed = [];
  // One at a time: a rejected page should not take the rest of the batch down.
  for (const detection of ready) {
    try {
      files += await queueDetection(detection);
    } catch (error) {
      failed.push(error.message);
    }
  }
  if (failed.length) toast(`${failed.length} page(s) failed: ${failed[0]}`, true);
  else toast(`Queued ${files} file${files > 1 ? 's' : ''} from ${ready.length} page${ready.length > 1 ? 's' : ''}`);
  refreshBatchButton();
}

function refreshBatchButton() {
  const button = $('#download-all');
  if (!button) return;
  const ready = lastDetections.filter(
    (d) => d.status === 'ok' && local.get(d.url)?.selected?.size,
  ).length;
  button.disabled = ready === 0;
  button.textContent = ready ? `Download all (${ready})` : 'Download all';
}

function updatePreview(card, detection, state) {
  const preview = card.querySelector('.preview');
  if (!preview) return;
  const count = state.selected.size;
  if (!count) { preview.textContent = ''; return; }

  const stem = finalStem(state.title || detection.title || '');
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

$('#download-all').addEventListener('click', downloadAll);
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
