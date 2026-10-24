/* Yester Time Machine — client logic */
const state = { revs: [], active: null, view: 'tree' };

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
};

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

function fmtTime(epoch) {
  const d = new Date(epoch * 1000);
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit'
  });
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

/* ------------------------------------------------------------------ */
async function loadStatus() {
  const s = await (await fetch('/api/status')).json();
  $('#chips').innerHTML = [
    ['branch', s.branch],
    ['head', s.head],
    ['snapshots', s.snapshots],
    ['dirty files', s.dirty_files],
    ['blob store', s.blobs + ' files / ' + s.blob_size_mb + ' MB'],
    ['encryption', s.crypto ? 'on' : 'off'],
    ['retention', s.retention ? 'on' : 'off'],
  ].map(([k, v]) => `<span class="chip">${k} <b>${esc(v)}</b></span>`).join('');
}

async function loadTimeline() {
  state.revs = await (await fetch('/api/timeline')).json();
  const box = $('#revs');
  box.innerHTML = '';
  state.revs.forEach((r, i) => {
    const row = el('div', 'rev');
    row.innerHTML = `
      <div><span class="dot" style="background:${r.color}"></span>
      <span class="kind">${esc(r.kind)}</span>
      <span class="when">${fmtTime(r.ts)}</span></div>
      <div class="what">${esc(r.subject)}</div>`;
    row.onclick = () => selectRev(r, row);
    box.appendChild(row);
    if (i === 0) selectRev(r, row);
  });
}

function selectRev(rev, row) {
  document.querySelectorAll('.rev').forEach(n => n.classList.remove('active'));
  if (row) row.classList.add('active');
  state.active = rev;
  state.view = 'tree';
  loadTree(rev.rev);
}

/* ------------------------------------------------------------------ */
function renderTree(node, container, prefix = '') {
  const entries = Object.entries(node).sort(([a, av], [b, bv]) => {
    const ad = av === null ? 0 : 1, bd = bv === null ? 0 : 1;
    return ad - bd || a.localeCompare(b);
  });
  for (const [name, child] of entries) {
    if (child === null) {
      const row = el('div', 'row');
      row.innerHTML = `<span class="name">${esc(name)}</span>`;
      row.onclick = () => openFile(prefix + name);
      container.appendChild(row);
    } else {
      const folder = el('div', 'row');
      folder.innerHTML = `<span class="name">&#128193; ${esc(name)}/</span>`;
      container.appendChild(folder);
      renderTree(child, container, prefix + name + '/');
    }
  }
}

async function loadTree(rev) {
  const tree = await (await fetch(`/api/tree?rev=${encodeURIComponent(rev)}`)).json();
  $('#panel-head').innerHTML =
    `<span class="path">${esc(state.active.subject)}</span>` +
    `<span>@ ${esc(rev.slice(0, 8))}</span>`;
  const body = $('#panel-body');
  body.innerHTML = '';
  const wrap = el('div', 'tree');
  renderTree(tree, wrap);
  body.appendChild(wrap);
}

async function openFile(path) {
  const rev = state.active.rev;
  const data = await (await fetch(
    `/api/file?rev=${encodeURIComponent(rev)}&path=${encodeURIComponent(path)}`)).json();
  if (data.error) { toast(data.error); return; }

  const head = $('#panel-head');
  head.innerHTML = `
    <span class="path">${esc(path)}</span>
    <span>@ ${esc(rev.slice(0, 8))} &middot; ${data.size} bytes</span>
    <button class="btn ghost" id="diff-btn">Diff vs previous</button>
    <button class="btn ghost" id="restore-btn">Restore this version</button>`;

  $('#diff-btn').onclick = () => showDiff(rev, path);
  $('#restore-btn').onclick = async () => {
    if (!confirm(`Restore ${path} to ${rev.slice(0, 8)}?\nCurrent state is committed first as a safety snapshot.`)) return;
    const res = await (await fetch('/api/restore', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rev, path}),
    })).json();
    if (res.error) { toast(res.error); return; }
    toast(`Restored ${path} from ${res.from.slice(0, 8)}`);
    loadStatus(); loadTimeline();
  };

  const body = $('#panel-body');
  body.innerHTML = '';
  if (data.binary) {
    body.appendChild(el('div', 'empty', 'Binary file — preview not available.'));
    return;
  }
  const pre = el('pre', 'file');
  pre.textContent = data.content + (data.truncated ? '\n… (truncated)' : '');
  body.appendChild(pre);
}

async function showDiff(rev, path) {
  const data = await (await fetch(
    `/api/diff?from=${encodeURIComponent(rev + '~1')}&to=${encodeURIComponent(rev)}` +
    `&path=${encodeURIComponent(path)}`)).json();
  const body = $('#panel-body');
  body.innerHTML = '';
  const pre = el('pre', 'diff');
  pre.innerHTML = (data.diff || '(no changes)').split('\n').map(line => {
    if (line.startsWith('+') && !line.startsWith('+++')) return `<span class="add">${esc(line)}</span>`;
    if (line.startsWith('-') && !line.startsWith('---')) return `<span class="del">${esc(line)}</span>`;
    if (line.startsWith('@@')) return `<span class="hunk">${esc(line)}</span>`;
    if (line.startsWith('diff ') || line.startsWith('index ') || line.startsWith('--- ') || line.startsWith('+++ '))
      return `<span class="meta">${esc(line)}</span>`;
    return esc(line);
  }).join('\n');
  body.appendChild(pre);
}

/* ------------------------------------------------------------------ */
let searchTimer = null;
$('#search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  searchTimer = setTimeout(() => runSearch(q), 350);
});

async function runSearch(q) {
  const body = $('#panel-body');
  $('#panel-head').innerHTML = `<span>Search results for “${esc(q)}”</span>`;
  if (!q) { if (state.active) loadTree(state.active.rev); return; }
  body.innerHTML = '<div class="empty">Searching every version…</div>';
  const results = await (await fetch(`/api/search?q=${encodeURIComponent(q)}`)).json();
  body.innerHTML = '';
  if (!results.length) {
    body.appendChild(el('div', 'empty', 'No matches across any version.'));
    return;
  }
  results.forEach(r => {
    const card = el('div', 'result');
    card.innerHTML = `
      <span class="score">${r.score}</span>
      <div class="path">${esc(r.path)}</div>
      <div class="rev">@ ${esc(r.rev.slice(0, 10))}</div>
      <div class="snippet">${esc(r.snippet)}</div>`;
    card.onclick = () => {
      const rev = state.revs.find(x => x.rev === r.rev);
      if (rev) { selectRev(rev, null); openFile(r.path); }
      else openFileAt(r.rev, r.path);
    };
    body.appendChild(card);
  });
}

async function openFileAt(rev, path) {
  const tree = await (await fetch(`/api/tree?rev=${encodeURIComponent(rev)}`)).json();
  state.active = { rev, subject: 'search result', ts: Math.floor(Date.now() / 1000), kind: 'manual' };
  const body = $('#panel-body');
  body.innerHTML = '';
  const wrap = el('div', 'tree');
  renderTree(tree, wrap);
  body.appendChild(wrap);
  openFile(path);
}

/* ------------------------------------------------------------------ */
$('#sync').onclick = async () => {
  const res = await (await fetch('/api/sync', {method: 'POST'})).json();
  toast(res.committed ? `Committed ${res.head}` : 'Nothing to commit');
  loadStatus(); loadTimeline();
};

loadStatus();
loadTimeline();
