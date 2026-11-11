'use strict';

/* ------------------------------------------------------------------ helpers */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const TOKEN = document.querySelector('meta[name="moddeck-token"]').content;

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

// Show ~ for the user's home folder so paths stay readable (and shareable).
const prettyPath = (p) => String(p ?? '')
  .replace(/^([A-Za-z]:\\Users\\[^\\]+)(?=\\|$)/i, '~')
  .replace(/^\/home\/[^/]+(?=\/|$)/, '~');

const ICON = {
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 3.2 21 19.5H3L12 3.2Z"/><path d="M12 9v4.5M12 16.8v.2"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7"/></svg>',
  chev: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m6 9 6 6 6-6"/></svg>',
  box: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M12 2.6 20.4 7v10L12 21.4 3.6 17V7L12 2.6Z"/><path d="M3.6 7 12 11.4 20.4 7"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0 4.5-4.5M12 15l-4.5-4.5"/><path d="M4 19h16"/></svg>',
};

const state = {
  instances: [],
  inst: null,
  tab: 'mods',
  scan: null,
  loadingScan: false,
  scanError: null,
  q: '',
  filter: 'all',
  sort: 'name',
  issuesOpen: true,
  tags: null,
  discover: { q: '', source: 'modrinth', sort: 'downloads', hits: [], total: 0, offset: 0, loading: false, loaded: false, error: null },
  updates: null,
  updatesLoading: false,
  updatesError: null,
  hasCfKey: false,
  busy: false,
  detail: null,
  trash: null,
  askDeleteOnDisable: true,
  dataDir: '',
  frozen: false,
};

async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (init.method === 'POST') {
    init.headers['X-ModDeck-Token'] = TOKEN;
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body || {});
  }
  const res = await fetch('/api/' + path, init);
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.error || msg; } catch { /* ignore */ }
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function toast(message, kind = 'ok', ms = 3600) {
  const el = document.createElement('div');
  el.className = 'toast' + (kind === 'error' ? ' error' : kind === 'warn' ? ' warn' : '');
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .2s'; }, ms - 250);
  setTimeout(() => el.remove(), ms);
}

const fmtNum = (n) => {
  if (n == null) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(n);
};

const fmtDate = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
};

const relDate = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const secs = (Date.now() - d.getTime()) / 1000;
  if (secs < 3600) return Math.max(1, Math.round(secs / 60)) + ' min ago';
  if (secs < 86400) return Math.round(secs / 3600) + ' h ago';
  if (secs < 86400 * 30) return Math.round(secs / 86400) + ' d ago';
  return d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
};

const loaderChip = (loader) => {
  const l = String(loader || 'unknown').toLowerCase();
  return `<span class="chip ${esc(l)}">${esc(l === 'unknown' ? '?' : l)}</span>`;
};

/* --------------------------------------------------------- markdown renderer
 *
 * Project descriptions come from the internet, so nothing is ever injected as
 * raw HTML. Markdown is escaped first, then a small safe subset is rendered.
 * Raw <br>, <img> and <a> tags commonly found in Modrinth/CurseForge bodies are
 * converted to their Markdown equivalents and everything else is stripped.
 */

const stripTags = (s) => String(s || '').replace(/<[^>]*>/g, '');

function decodeEntities(s) {
  const el = document.createElement('textarea');
  el.innerHTML = String(s || '');
  return el.value;
}

function safeHref(url) {
  const raw = decodeEntities(String(url || '').trim()).replace(/^<|>$/g, '');
  return /^https?:\/\//i.test(raw) ? esc(raw) : '';
}

function rawTagsToMarkdown(text) {
  let s = String(text || '');
  s = s.replace(/<img\b[^>]*>/gi, (tag) => {
    const src = (tag.match(/\bsrc\s*=\s*["']([^"']+)["']/i) || [])[1] || '';
    const alt = (tag.match(/\balt\s*=\s*["']([^"']*)["']/i) || [])[1] || '';
    return src ? `\n\n![${alt}](${src})\n\n` : '';
  });
  s = s.replace(/<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi,
    (m, href, label) => `[${stripTags(label).trim()}](${href})`);
  s = s.replace(/<br\s*\/?>/gi, '\n');
  s = s.replace(/<h([1-6])\b[^>]*>/gi, (m, n) => '\n\n' + '#'.repeat(Number(n)) + ' ');
  s = s.replace(/<li\b[^>]*>/gi, '\n- ');
  s = s.replace(/<\/(p|div|section|article|details|summary|li|ul|ol|h[1-6]|blockquote|tr)>/gi, '\n');
  s = s.replace(/<hr\b[^>]*>/gi, '\n\n---\n\n');
  s = stripTags(s);
  return decodeEntities(s);
}

function inlineMd(text) {
  let s = esc(String(text ?? ''));
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (m, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });

  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+["'][^"']*["'])?\)/g, (m, alt, url) => {
    const href = safeHref(url);
    return href ? `<img class="md-img" src="${href}" alt="${alt}" loading="lazy">` : alt;
  });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+["'][^"']*["'])?\)/g, (m, label, url) => {
    const href = safeHref(url);
    return href ? `<a href="${href}" target="_blank" rel="noreferrer">${label}</a>` : label;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
  s = s.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
  s = s.replace(/\u0000(\d+)\u0000/g, (m, n) => `<code>${codes[Number(n)]}</code>`);
  return s;
}

function isBlockStart(line) {
  return /^\s*(```|~~~|#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s?|([-*_])(?:\s*\1){2,}\s*$)/.test(line);
}

function mdToHtml(source) {
  const lines = rawTagsToMarkdown(source).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let list = null;
  let inCode = false;
  let code = [];
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.replace(/\s+$/, '');

    const fence = line.match(/^\s*(```+|~~~+)\s*([\w-]*)\s*$/);
    if (fence) {
      if (!inCode) { closeList(); inCode = true; code = []; }
      else {
        out.push(`<pre class="md-code"><code>${esc(code.join('\n'))}</code></pre>`);
        inCode = false;
      }
      continue;
    }
    if (inCode) { code.push(raw); continue; }

    if (!line.trim()) { closeList(); continue; }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = Math.min(6, heading[1].length + 2);
      out.push(`<h${level}>${inlineMd(heading[2])}</h${level}>`);
      continue;
    }

    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { closeList(); out.push('<hr>'); continue; }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      closeList();
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, ''));
        i++;
      }
      i--;
      out.push(`<blockquote>${mdToHtml(buf.join('\n'))}</blockquote>`);
      continue;
    }

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      const want = ul ? 'ul' : 'ol';
      if (list !== want) { closeList(); out.push(`<${want}>`); list = want; }
      out.push(`<li>${inlineMd((ul || ol)[1])}</li>`);
      continue;
    }

    closeList();
    const para = [line];
    let j = i + 1;
    while (j < lines.length && lines[j].trim() && !isBlockStart(lines[j])) {
      para.push(lines[j].replace(/\s+$/, ''));
      j++;
    }
    out.push(`<p>${inlineMd(para.join(' '))}</p>`);
    i = j - 1;
  }

  if (inCode && code.length) out.push(`<pre class="md-code"><code>${esc(code.join('\n'))}</code></pre>`);
  closeList();
  return out.join('\n');
}

/* -------------------------------------------------------------------- modal */

let modalCloser = null;

function openModal(html, { wide = false } = {}) {
  const root = $('#modal-root');
  root.innerHTML = `<div class="modal-backdrop" data-act="close-modal">
    <div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true">${html}</div>
  </div>`;
  modalCloser = () => { root.innerHTML = ''; modalCloser = null; };
  const input = $('.modal input[type=text]', root);
  if (input) setTimeout(() => input.focus(), 30);
}

function closeModal() { if (modalCloser) modalCloser(); }

function confirmModal({ title, body, label = 'Confirm', danger = true, onOk }) {
  openModal(`
    <div class="modal-head">
      <div>
        <h2>${esc(title)}</h2>
        <p>${body}</p>
      </div>
      <button class="modal-x" data-act="close-modal">×</button>
    </div>
    <div class="modal-foot">
      <button class="btn ghost" data-act="close-modal">Cancel</button>
      <button class="btn ${danger ? 'danger' : 'primary'}" id="confirm-ok">${esc(label)}</button>
    </div>`);
  $('#confirm-ok').addEventListener('click', async () => {
    closeModal();
    await onOk();
  });
}

/* ------------------------------------------------------------------- render */

function renderSidebar() {
  $('#instances').innerHTML = state.instances.map((inst) => `
    <button class="inst ${state.inst && inst.id === state.inst.id ? 'active' : ''}"
            data-act="select-inst" data-id="${esc(inst.id)}" title="${esc(inst.path)}">
      <span class="inst-dot"></span>
      <span class="inst-body">
        <span class="inst-name">${esc(inst.name)}</span>
        <span class="inst-meta">${esc(inst.loader)}${inst.mcVersion ? ' · ' + esc(inst.mcVersion) : ''} · ${inst.modsCount || 0} mods</span>
      </span>
    </button>`).join('') || '<div class="side-version" style="padding:8px 10px">No instances found</div>';
}

function renderHeader() {
  const inst = state.inst;
  if (!inst) { $('#topbar').innerHTML = ''; return; }
  const score = state.scan ? state.scan.score : null;
  const color = score == null ? 'var(--line-2)' : score >= 90 ? 'var(--accent)' : score >= 65 ? 'var(--warn)' : 'var(--danger)';
  $('#topbar').innerHTML = `
    <div>
      <h1>${esc(inst.name)}</h1>
      <div class="inst-line">
        ${loaderChip(inst.loader)}
        <button class="pill" data-act="edit-version" title="Change the Minecraft version ModDeck checks against">
          Minecraft <b>${esc(inst.mcVersion || 'set version')}</b>
        </button>
        <span class="muted">${esc(inst.type)}</span>
        <span class="path" title="${esc(inst.modsPath)}">${esc(prettyPath(inst.modsPath))}</span>
      </div>
    </div>
    <div class="top-actions">
      ${score == null ? '' : `<div class="ring" style="--score:${score};--ring-color:${color}">
        <div><div class="ring-num">${score}</div><div class="ring-label">health</div></div>
      </div>`}
      <button class="btn ghost" data-act="open-folder" title="Open the mods folder in Explorer">
        ${ICON.box}<span>Folder</span>
      </button>
      <button class="btn" data-act="rescan" ${state.loadingScan ? 'disabled' : ''}>
        ${state.loadingScan ? '<span class="spinner"></span>' : ''} Rescan
      </button>
    </div>`;
}

function renderTabs() {
  const c = state.scan ? state.scan.counts : null;
  const upd = state.updates ? state.updates.rows.filter((r) => r.status === 'update').length : null;
  const tabs = [
    ['mods', 'Mods', c ? c.enabled : null, c && c.errors ? 'bad' : ''],
    ['discover', 'Discover', null, ''],
    ['updates', 'Updates', upd, upd ? 'warn' : ''],
    ['settings', 'Settings', null, ''],
  ];
  $('#tabs').innerHTML = tabs.map(([id, label, count, cls]) => `
    <button class="tab ${state.tab === id ? 'active' : ''}" data-act="tab" data-tab="${id}">
      ${label}${count != null ? `<span class="count ${cls}">${count}</span>` : ''}
    </button>`).join('');
}

function render() {
  renderSidebar();
  renderHeader();
  renderTabs();
  const settingsBtn = $('.side-item[data-tab="settings"]');
  if (settingsBtn) settingsBtn.classList.toggle('active', !state.detail && state.tab === 'settings');
  if (state.detail) renderProject();
  else if (state.tab === 'mods') renderMods();
  else if (state.tab === 'discover') renderDiscover();
  else if (state.tab === 'updates') renderUpdates();
  else renderSettings();
}

/* --------------------------------------------------------------------- mods */

function renderMods() {
  const view = $('#view');
  if (!state.inst) {
    view.innerHTML = `<div class="empty">${ICON.box}<h3>No instance selected</h3><p>Add your Minecraft folder from the sidebar to get started.</p></div>`;
    return;
  }
  if (state.loadingScan && !state.scan) {
    view.innerHTML = `<div class="view-inner">
      <div class="skeleton" style="height:78px;margin-bottom:16px"></div>
      <div class="skeleton" style="height:44px;margin-bottom:12px;width:60%"></div>
      <div class="skeleton" style="height:300px"></div></div>`;
    return;
  }
  if (state.scanError) {
    view.innerHTML = `<div class="view-inner"><div class="empty">${ICON.warn}<h3>Could not read this instance</h3><p>${esc(state.scanError)}</p></div></div>`;
    return;
  }
  if (!state.scan) return;

  const issues = state.scan.issues || [];
  view.innerHTML = `<div class="view-inner">
    <div id="issues"></div>
    <div class="toolbar">
      <div class="search">
        ${ICON.search}
        <input id="mod-search" type="search" placeholder="Filter installed mods…  (press /)" value="${esc(state.q)}" autocomplete="off">
      </div>
      <div class="filters" id="mod-filters"></div>
      <select class="sort" id="mod-sort">
        <option value="name">Name</option>
        <option value="status">Problems first</option>
        <option value="added">Recently added</option>
        <option value="size">Largest first</option>
      </select>
    </div>
    <div id="bulk"></div>
    <div id="mod-list"></div>
  </div>`;

  $('#mod-sort').value = state.sort;
  paintIssues();
  paintFilters();
  paintModRows();

  $('#mod-search').addEventListener('input', (e) => { state.q = e.target.value; paintModRows(); });
  $('#mod-sort').addEventListener('change', (e) => { state.sort = e.target.value; paintModRows(); });
  $('#mod-list').addEventListener('change', (e) => {
    if (e.target.classList.contains('mod-check')) paintBulk();
  });
}

function paintIssues() {
  const host = $('#issues');
  if (!host) return;
  const issues = state.scan ? state.scan.issues || [] : [];
  if (!issues.length) {
    host.innerHTML = `<div class="issues ok" data-open="true">
      <div class="issues-head">
        <span class="i-ico">${ICON.check}</span>
        <b>No conflicts detected</b>
        <span class="sub">— every installed mod looks loadable for this instance</span>
      </div></div>`;
    return;
  }
  const errors = issues.filter((i) => i.severity === 'error').length;
  const warnings = issues.length - errors;
  const cls = errors ? '' : 'warn-only';
  host.innerHTML = `<div class="issues ${cls}" data-open="${state.issuesOpen}">
    <button class="issues-head" data-act="toggle-issues">
      <span class="i-ico">${ICON.warn}</span>
      <b>${errors ? `${errors} blocking problem${errors === 1 ? '' : 's'}` : `${warnings} warning${warnings === 1 ? '' : 's'}`}</b>
      <span class="sub">${errors && warnings ? `${warnings} warning${warnings === 1 ? '' : 's'} too` : ''}</span>
      <span class="chev">${ICON.chev}</span>
    </button>
    <div class="issues-body">${issues.map(issueCard).join('')}</div>
  </div>`;
}

function issueActions(issue) {
  const out = [];
  if (issue.kind === 'duplicate') {
    out.push(`<button class="btn sm primary" data-act="fix-duplicate"
      data-keep="${esc(issue.keep)}" data-disable="${esc((issue.disable || []).join('|'))}">
      Keep newest, disable ${issue.disable ? issue.disable.length : 0} older</button>`);
  }
  if (issue.kind === 'missing-dep') {
    out.push(`<button class="btn sm primary" data-act="find-dep" data-dep="${esc(issue.search || issue.dep)}">
      Find “${esc(issue.dep)}” on Modrinth</button>`);
  }
  if (issue.kind === 'mc-mismatch') {
    out.push(`<button class="btn sm" data-act="edit-version">Check target version</button>`);
  }
  const shown = new Set();
  if (issue.kind !== 'duplicate') {
    for (const f of issue.files || []) {
      if (shown.has(f)) continue;
      shown.add(f);
      const mod = (state.scan.mods || []).find((m) => m.file === f && m.enabled);
      if (mod) {
        out.push(`<button class="btn sm" data-act="disable-file" data-file="${esc(f)}">Disable ${esc(shortName(f))}</button>`);
      }
    }
  }
  return out.length ? `<div class="issue-actions">${out.join('')}</div>` : '';
}

function issueCard(issue) {
  return `<div class="issue ${issue.severity}">
    <div class="issue-top">
      <span class="issue-dot"></span>
      <span class="issue-title">${esc(issue.title)}</span>
    </div>
    <div class="issue-detail">${esc(issue.detail)}</div>
    ${issue.files && issue.files.length ? `<div class="issue-files">${[...new Set(issue.files)].map((f) => `<span class="file-chip">${esc(f)}</span>`).join('')}</div>` : ''}
    ${issueActions(issue)}
  </div>`;
}

const shortName = (file) => {
  const base = String(file).replace(/\.jar(\.disabled)?$/i, '');
  return base.length > 26 ? base.slice(0, 24) + '…' : base;
};

function paintFilters() {
  const host = $('#mod-filters');
  if (!host) return;
  const counts = state.scan.counts;
  const filters = [
    ['all', 'All'],
    ['issues', 'Issues', counts.errors + counts.warnings],
    ['disabled', 'Disabled', counts.disabled],
  ];
  host.innerHTML = filters.map(([id, label, n]) => `
    <button class="filter ${state.filter === id ? 'active' : ''}" data-act="filter" data-filter="${id}">
      ${label}${n ? ` <span class="muted">${n}</span>` : ''}
    </button>`).join('');
}

function currentMods() {
  let mods = [...(state.scan.mods || [])];
  const q = state.q.trim().toLowerCase();
  if (state.filter === 'issues') mods = mods.filter((m) => m.status === 'error' || m.status === 'warn');
  if (state.filter === 'disabled') mods = mods.filter((m) => !m.enabled);
  if (state.filter === 'errors') mods = mods.filter((m) => m.status === 'error');
  if (q) {
    mods = mods.filter((m) => (`${m.name} ${m.id || ''} ${m.description || ''} ${m.file}`).toLowerCase().includes(q));
  }
  const rank = { error: 0, warn: 1, ok: 2, disabled: 3 };
  if (state.sort === 'status') mods.sort((a, b) => (rank[a.status] ?? 9) - (rank[b.status] ?? 9) || (a.name || '').localeCompare(b.name || ''));
  else if (state.sort === 'added') mods.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  else if (state.sort === 'size') mods.sort((a, b) => (b.size || 0) - (a.size || 0));
  else mods.sort((a, b) => (a.name || a.file).localeCompare(b.name || b.file));
  return mods;
}

function paintModRows() {
  const host = $('#mod-list');
  if (!host) return;
  const mods = currentMods();
  if (!mods.length) {
    host.innerHTML = `<div class="empty">${ICON.box}<h3>Nothing here</h3><p>${state.q || state.filter !== 'all'
      ? 'No installed mod matches this filter.'
      : 'This instance has no mods yet. Head to Discover to add some.'}</p></div>`;
    paintBulk();
    return;
  }
  host.innerHTML = `<div class="rows">${mods.map(modRow).join('')}</div>`;
  paintBulk();
}

function modRow(m) {
  const statusText = { ok: 'OK', warn: 'Warning', error: 'Problem', disabled: 'Disabled' }[m.status] || m.status;
  const deps = (m.deps || []).filter((d) => d.id !== 'minecraft');
  const details = `
    <dl class="detail-grid">
      <dt>File</dt><dd><code>${esc(m.file)}</code></dd>
      <dt>Size</dt><dd>${esc(m.sizeHuman || '')}</dd>
      <dt>Mod ID</dt><dd><code>${esc(m.id || '—')}</code></dd>
      <dt>Minecraft</dt><dd>${esc(m.mcRange || 'not declared')}</dd>
      <dt>Environment</dt><dd>${esc(m.env || 'both')}</dd>
      <dt>Authors</dt><dd>${esc((m.authors || []).join(', ') || '—')}</dd>
      ${deps.length ? `<dt>Requires</dt><dd>${deps.map((d) => `<code>${esc(d.id)}</code>${d.required ? '' : ' <span class="muted">(optional)</span>'}`).join(', ')}</dd>` : ''}
      ${(m.breaks || []).length ? `<dt>Breaks</dt><dd>${m.breaks.map((d) => `<code>${esc(d.id)} ${esc(d.range || '')}</code>`).join(', ')}</dd>` : ''}
      ${(m.conflicts || []).length ? `<dt>Conflicts</dt><dd>${m.conflicts.map((d) => `<code>${esc(d.id)}</code>`).join(', ')}</dd>` : ''}
      ${m.nested ? `<dt>Bundled jars</dt><dd>${m.nested} nested jar${m.nested === 1 ? '' : 's'}</dd>` : ''}
    </dl>`;
  return `<div class="mod-row ${m.status}" data-file="${esc(m.file)}">
    <input type="checkbox" class="mod-check" value="${esc(m.file)}" aria-label="Select ${esc(m.name)}">
    <div class="micon">${m.icon ? `<img src="${esc(m.icon)}" alt="" loading="lazy" onerror="this.remove()">` : esc((m.name || '?').slice(0, 1).toUpperCase())}</div>
    <div class="mod-main" data-act="toggle-row" data-file="${esc(m.file)}">
      <div class="mod-title">
        <span class="mod-name">${esc(m.name || m.file)}</span>
        <span class="mod-ver">${esc(m.version || '')}</span>
      </div>
      <div class="mod-desc">${esc(m.description || m.file)}</div>
      <div class="mod-tags">
        ${loaderChip(m.loader)}
        ${m.env && m.env !== 'both' ? `<span class="chip ${esc(m.env)}">${esc(m.env)} only</span>` : ''}
        ${(m.issues || []).length ? `<span class="chip muted" title="${esc(m.issues.join(' '))}">read notes</span>` : ''}
      </div>
    </div>
    <div class="mod-status"><span class="dot ${m.status}"></span>${esc(statusText)}</div>
    <div class="mod-actions">
      <button class="btn sm" data-act="toggle-mod" data-file="${esc(m.file)}" data-enabled="${m.enabled}">
        ${m.enabled ? 'Disable' : 'Enable'}
      </button>
      <button class="btn sm ghost" data-act="delete-mod" data-file="${esc(m.file)}" title="Move to ModDeck trash">Remove</button>
    </div>
    <div class="mod-details">${details}</div>
  </div>`;
}

function paintBulk() {
  const host = $('#bulk');
  if (!host) return;
  const checked = $$('.mod-check:checked').map((c) => c.value);
  if (!checked.length) { host.innerHTML = ''; return; }
  host.innerHTML = `<div class="row-gap" style="margin:-4px 0 12px">
    <span class="muted">${checked.length} selected</span>
    <button class="btn sm" data-act="bulk-disable">Disable</button>
    <button class="btn sm ghost" data-act="bulk-enable">Enable</button>
    <button class="btn sm ghost" data-act="clear-selection">Clear</button>
  </div>`;
}

/* ----------------------------------------------------------------- discover */

async function ensureTags() {
  if (state.tags) return state.tags;
  try { state.tags = await api('tags'); } catch { state.tags = { gameVersions: [], loaders: [] }; }
  return state.tags;
}

function renderDiscover() {
  const view = $('#view');
  const d = state.discover;
  view.innerHTML = `<div class="view-inner">
    <div class="toolbar">
      <div class="search">
        ${ICON.search}
        <input id="disc-search" type="search" placeholder="Search ${esc(state.inst ? state.inst.loader : '')} mods for ${esc((state.inst && state.inst.mcVersion) || 'your version')}…"
               value="${esc(d.q)}" autocomplete="off">
      </div>
      <div class="seg" id="disc-source">
        <button class="${d.source === 'modrinth' ? 'active' : ''}" data-act="disc-source" data-source="modrinth">Modrinth</button>
        <button class="${d.source === 'curseforge' ? 'active' : ''}" data-act="disc-source" data-source="curseforge">CurseForge</button>
      </div>
      <select class="sort" id="disc-sort">
        <option value="downloads">Most downloaded</option>
        <option value="relevance">Relevance</option>
        <option value="updated">Recently updated</option>
        <option value="newest">Newest</option>
      </select>
    </div>
    <div id="disc-note"></div>
    <div id="disc-results"></div>
  </div>`;
  $('#disc-sort').value = d.sort;
  $('#disc-search').addEventListener('input', (e) => {
    d.q = e.target.value;
    clearTimeout(renderDiscover._t);
    renderDiscover._t = setTimeout(() => { d.offset = 0; d.loaded = false; searchDiscover(); }, 380);
  });
  $('#disc-sort').addEventListener('change', (e) => { d.sort = e.target.value; d.offset = 0; d.loaded = false; searchDiscover(); });
  if (!d.loaded) searchDiscover();
  else { paintDiscoverResults(); paintDiscoverNote(); }
}

function paintDiscoverNote() {
  const host = $('#disc-note');
  if (!host) return;
  const d = state.discover;
  if (d.source === 'curseforge' && !state.hasCfKey) {
    host.innerHTML = `<div class="issues warn-only" data-open="true" style="margin-bottom:14px">
      <div class="issues-head"><span class="i-ico">${ICON.warn}</span>
        <b>CurseForge needs an API key</b>
        <span class="sub">— free from console.curseforge.com</span>
        <button class="btn sm primary" style="margin-left:auto" data-act="tab" data-tab="settings">Add key</button>
      </div></div>`;
    return;
  }
  host.innerHTML = '';
}

async function searchDiscover(append = false) {
  const d = state.discover;
  if (d.loading) return;
  d.loading = true;
  d.error = null;
  if (!append) d.hits = [];
  paintDiscoverResults();

  const params = new URLSearchParams({
    source: d.source,
    q: d.q,
    sort: d.sort,
    offset: String(append ? d.offset : 0),
  });
  if (state.inst) {
    params.set('loader', state.inst.loader || '');
    params.set('mc', state.inst.mcVersion || '');
  }
  try {
    const data = await api('search?' + params.toString());
    d.hits = append ? d.hits.concat(data.hits) : data.hits;
    d.total = data.total || d.hits.length;
    d.offset = d.hits.length;
    d.loaded = true;
  } catch (e) {
    d.error = e.message;
    d.loaded = true;
  } finally {
    d.loading = false;
    paintDiscoverResults();
    paintDiscoverNote();
  }
}

function paintDiscoverResults() {
  const host = $('#disc-results');
  if (!host) return;
  const d = state.discover;
  if (d.loading && !d.hits.length) {
    host.innerHTML = `<div class="grid">${Array.from({ length: 6 }).map(() => '<div class="skeleton" style="height:168px"></div>').join('')}</div>`;
    return;
  }
  if (d.error) {
    host.innerHTML = `<div class="empty">${ICON.warn}<h3>Search failed</h3><p>${esc(d.error)}</p></div>`;
    return;
  }
  if (!d.hits.length) {
    host.innerHTML = `<div class="empty">${ICON.search}<h3>No results</h3>
      <p>Nothing matched for ${esc(state.inst ? `${state.inst.loader} ${state.inst.mcVersion}` : 'this version')}. Try a different search term
      or change the target Minecraft version from the header.</p></div>`;
    return;
  }
  host.innerHTML = `
    <div class="muted" style="font-size:11.5px;margin-bottom:10px">${fmtNum(d.total)} results · filtered to
      ${esc(state.inst ? `${state.inst.loader} · ${state.inst.mcVersion || 'any version'}` : 'all versions')}</div>
    <div class="grid">${d.hits.map(discoverCard).join('')}</div>
    ${d.hits.length < d.total ? `<div class="load-more"><button class="btn" data-act="disc-more" ${d.loading ? 'disabled' : ''}>
      ${d.loading ? '<span class="spinner"></span>' : ''} Load more</button></div>` : ''}`;
}

function discoverCard(hit) {
  return `<div class="card" data-act="open-project" data-source="${esc(hit.source)}"
      data-id="${esc(hit.id)}" data-title="${esc(hit.title)}" role="button" tabindex="0">
    <div class="card-top">
      <img class="card-icon" src="${esc(hit.icon || '')}" alt="" loading="lazy" onerror="this.style.opacity=.15">
      <div style="min-width:0">
        <div class="card-title">${esc(hit.title)}</div>
        <div class="card-by">${esc(hit.author || 'unknown author')}</div>
      </div>
    </div>
    <div class="card-desc">${esc(hit.description || '')}</div>
    <div class="card-foot">
      <div class="card-stats">
        <span>${ICON.download.replace('<svg', '<svg width="12" height="12" style="vertical-align:-1px"')} ${fmtNum(hit.downloads)}</span>
        <span>${esc(relDate(hit.updated))}</span>
      </div>
      <button class="btn sm primary" data-act="pick-version"
        data-source="${esc(hit.source)}" data-id="${esc(hit.id)}" data-title="${esc(hit.title)}">Install</button>
    </div>
  </div>`;
}

/* ------------------------------------------------------------ version picker */

async function pickVersion(source, id, title) {
  openModal(`<div class="modal-head"><div><h2>${esc(title)}</h2>
      <p>Loading compatible versions…</p></div><button class="modal-x" data-act="close-modal">×</button></div>
    <div class="modal-body"><div class="skeleton" style="height:120px"></div></div>`, { wide: true });

  const params = new URLSearchParams({ source, id });
  if (state.inst) { params.set('loader', state.inst.loader || ''); params.set('mc', state.inst.mcVersion || ''); }
  let project;
  try { project = await api('project?' + params.toString()); }
  catch (e) {
    openModal(`<div class="modal-head"><div><h2>${esc(title)}</h2><p>Could not load versions.</p></div>
      <button class="modal-x" data-act="close-modal">×</button></div>
      <div class="modal-body"><div class="note">${esc(e.message)}</div></div>
      <div class="modal-foot"><button class="btn" data-act="close-modal">Close</button></div>`);
    return;
  }
  const versions = project.versions || [];
  const rows = versions.length ? versions.slice(0, 40).map((v) => `
    <div class="version">
      <div class="v-main">
        <div class="v-name">${esc(v.versionNumber || v.name)}
          ${v.releaseType && v.releaseType !== 'release' ? `<span class="chip muted">${esc(v.releaseType)}</span>` : ''}
        </div>
        <div class="v-meta">
          <span>${esc(fmtDate(v.published))}</span>
          <span>${esc(v.fileName || '')}</span>
          ${v.fileSize ? `<span>${(v.fileSize / 1048576).toFixed(1)} MB</span>` : ''}
          ${(v.loaders || []).map((l) => `<span class="chip ${esc(l)}" style="font-size:9px;padding:0 5px">${esc(l)}</span>`).join('')}
          ${(v.dependencies || []).filter((d) => d.type === 'required').length
            ? `<span class="muted">+${v.dependencies.filter((d) => d.type === 'required').length} required dep(s)</span>` : ''}
        </div>
      </div>
      <button class="btn sm primary" data-act="do-install" data-source="${esc(source)}"
        data-project="${esc(project.id)}" data-version="${esc(v.id)}" data-name="${esc(v.versionNumber || v.name)}"
        ${v.downloadUrl === null ? 'disabled title="Author disabled third-party downloads"' : ''}>Install</button>
    </div>`).join('') : `<div class="empty" style="padding:26px">${ICON.box}<h3>No compatible builds</h3>
      <p>This project has no files for ${esc(state.inst.loader)} ${esc(state.inst.mcVersion)}. Change the target version from the header, or check the project page.</p></div>`;

  openModal(`<div class="modal-head">
      <img class="card-icon" src="${esc(project.icon || '')}" alt="" onerror="this.style.opacity=.15">
      <div style="min-width:0">
        <h2>${esc(project.title || title)}</h2>
        <p>${esc(project.description || '')}</p>
      </div>
      <button class="modal-x" data-act="close-modal">×</button>
    </div>
    <div class="modal-body">
      <label class="row-gap" style="margin-bottom:12px;font-size:12.5px;color:var(--text-2)">
        <input type="checkbox" id="install-deps" checked style="accent-color:var(--accent)">
        Also install required dependencies
      </label>
      ${rows}
    </div>
    <div class="modal-foot">
      ${project.website ? `<a class="btn ghost" href="${esc(project.website)}" target="_blank" rel="noreferrer">Open project page</a>` : ''}
      <button class="btn" data-act="close-modal">Close</button>
    </div>`, { wide: true });
}

/* ------------------------------------------------------------- project detail */

function setHash(hash) {
  try { history.replaceState(null, '', '#' + hash); } catch { /* ignore */ }
}

function handleHash() {
  const hash = location.hash.replace(/^#/, '');
  if (hash.startsWith('project/')) {
    const [, source, id] = hash.split('/');
    if (source && id) openProject(source, id);
    return;
  }
  if (['mods', 'discover', 'updates', 'settings'].includes(hash)) {
    state.detail = null;
    state.tab = hash;
    render();
    if (hash === 'updates' && !state.updates && !state.updatesLoading) checkUpdates();
  }
}

async function openProject(source, id, title = '', author = '') {
  if (!source || !id) return;
  if (state.detail && state.detail.source === source && state.detail.id === id
      && (state.detail.loading || state.detail.data)) return;

  state.detail = { source, id, title, author, tab: 'overview', data: null, loading: true, error: null };
  setHash(`project/${source}/${id}`);
  render();

  const params = new URLSearchParams({ source, id });
  if (state.inst) {
    params.set('loader', state.inst.loader || '');
    params.set('mc', state.inst.mcVersion || '');
  }
  try {
    const data = await api('project?' + params.toString());
    if (!state.detail || state.detail.source !== source || state.detail.id !== id) return;
    state.detail.data = data;
  } catch (e) {
    if (!state.detail || state.detail.source !== source || state.detail.id !== id) return;
    state.detail.error = e.message;
  } finally {
    if (state.detail && state.detail.source === source && state.detail.id === id) {
      state.detail.loading = false;
      render();
    }
  }
}

function closeProject() {
  state.detail = null;
  setHash(state.tab);
  render();
}

function renderProject() {
  const view = $('#view');
  const d = state.detail;
  if (!d) return;
  const back = `<div class="proj-back"><button class="btn ghost sm" data-act="close-project">← Back to ${esc(state.tab)}</button></div>`;

  if (d.loading && !d.data) {
    view.innerHTML = `<div class="view-inner">${back}
      <div class="proj-hero"><div class="skeleton" style="width:88px;height:88px;border-radius:18px"></div>
        <div style="flex:1"><div class="skeleton" style="height:26px;width:52%;margin-bottom:10px"></div>
        <div class="skeleton" style="height:14px;width:80%;margin-bottom:6px"></div>
        <div class="skeleton" style="height:14px;width:60%"></div></div></div>
      <div class="skeleton" style="height:340px;margin-top:16px"></div></div>`;
    return;
  }

  if (d.error) {
    view.innerHTML = `<div class="view-inner">${back}
      <div class="empty">${ICON.warn}<h3>Could not open this project</h3><p>${esc(d.error)}</p>
      ${d.source === 'curseforge' && !state.hasCfKey
        ? '<div style="margin-top:16px"><button class="btn primary" data-act="tab" data-tab="settings">Add a CurseForge key</button></div>'
        : ''}</div></div>`;
    return;
  }

  const p = d.data || {};
  const versions = p.versions || [];
  const gallery = (p.gallery || []).filter((g) => g && g.url);
  const stats = [];
  if (p.downloads) stats.push(`${ICON.download.replace('<svg', '<svg width="13" height="13" style="vertical-align:-2px"')} ${fmtNum(p.downloads)} downloads`);
  if (p.follows) stats.push(`♥ ${fmtNum(p.follows)} followers`);
  if (p.updated) stats.push(`Updated ${relDate(p.updated)}`);

  const categories = (p.categories || []).filter(Boolean).slice(0, 6);
  const authorName = p.author || d.author || '';
  const author = authorName ? `<div class="card-by">by ${esc(authorName)}</div>` : '';

  const galleryHtml = gallery.length ? `
    <div class="proj-gallery">
      <button class="gallery-main" data-act="lightbox" data-url="${esc(gallery[0].url)}">
        <img id="proj-hero-img" src="${esc(gallery[0].url)}" alt="" loading="lazy">
      </button>
      ${gallery.length > 1 ? `<div class="gallery-thumbs">${gallery.slice(0, 12).map((g, i) => `
        <button class="gallery-thumb ${i === 0 ? 'active' : ''}" data-act="gallery-set"
          data-url="${esc(g.url)}" title="${esc(g.title || '')}">
          <img src="${esc(g.url)}" alt="" loading="lazy">
        </button>`).join('')}</div>` : ''}
    </div>` : '';

  const overview = `${galleryHtml}
    <div class="md">${p.body ? mdToHtml(p.body) : '<p class="muted">No description provided.</p>'}</div>`;

  const versionsHtml = versions.length ? `<div class="proj-versions">${versions.map((v) => `
    <div class="version">
      <div class="v-main">
        <div class="v-name">${esc(v.versionNumber || v.name || v.id)}
          ${v.releaseType && v.releaseType !== 'release' ? `<span class="chip muted">${esc(v.releaseType)}</span>` : ''}
        </div>
        <div class="v-meta">
          <span>${esc(fmtDate(v.published))}</span>
          <span>${esc(v.fileName || '')}</span>
          ${v.fileSize ? `<span>${(v.fileSize / 1048576).toFixed(1)} MB</span>` : ''}
          ${(v.loaders || []).map((l) => `<span class="chip ${esc(l)}" style="font-size:9px;padding:0 5px">${esc(l)}</span>`).join('')}
          ${(v.dependencies || []).filter((x) => x.type === 'required').length
            ? `<span class="muted">+${v.dependencies.filter((x) => x.type === 'required').length} required dep(s)</span>` : ''}
        </div>
      </div>
      ${v.downloadUrl === null
        ? '<button class="btn sm primary" disabled title="Author disabled third-party downloads">Unavailable</button>'
        : `<button class="btn sm primary" data-act="do-install" data-source="${esc(p.source)}"
             data-project="${esc(p.id)}" data-version="${esc(v.id)}" data-name="${esc(v.versionNumber || v.name)}">Install</button>`}
    </div>`).join('')}</div>`
    : `<div class="empty" style="padding:26px">${ICON.box}<h3>No compatible builds</h3>
       <p>No files for ${esc(state.inst ? `${state.inst.loader} ${state.inst.mcVersion}` : 'this version')}.
       Change the target version from the header, or check the project page.</p></div>`;

  const website = safeHref(p.website || '');
  const links = Object.entries(p.links || {})
    .map(([label, url]) => [label, safeHref(url)])
    .filter(([, url]) => url);
  const sideLinks = [
    website ? ['Project page', website] : null,
    ...links,
  ].filter(Boolean);

  view.innerHTML = `<div class="view-inner proj">${back}
    <div class="proj-hero">
      <div class="proj-icon">${p.icon
        ? `<img src="${esc(p.icon)}" alt="" onerror="this.remove()">`
        : esc(String(p.title || '?').slice(0, 1).toUpperCase())}</div>
      <div class="proj-headline">
        <div class="row-gap proj-chips">
          <span class="chip ${p.source === 'curseforge' ? 'forge' : 'fabric'}">${esc(p.source === 'curseforge' ? 'CurseForge' : 'Modrinth')}</span>
          ${categories.map((c) => `<span class="chip muted">${esc(c)}</span>`).join('')}
        </div>
        <h1>${esc(p.title || d.title || 'Project')}</h1>
        ${author}
        ${p.description ? `<p class="proj-tagline">${esc(p.description)}</p>` : ''}
        <div class="proj-stats">${stats.map((s) => `<span>${s}</span>`).join('')}</div>
        <div class="row-gap proj-cta">
          <button class="btn primary" data-act="pick-version"
            data-source="${esc(p.source)}" data-id="${esc(p.id)}" data-title="${esc(p.title || '')}">Install</button>
          <button class="btn" data-act="proj-tab" data-tab="versions">Versions${versions.length ? ` (${versions.length})` : ''}</button>
          ${website ? `<a class="btn ghost" href="${website}" target="_blank" rel="noreferrer">Open project page</a>` : ''}
        </div>
      </div>
    </div>

    <div class="proj-grid">
      <div class="proj-main">
        <div class="proj-tabsrow">
          <button class="tab ${d.tab === 'overview' ? 'active' : ''}" data-act="proj-tab" data-tab="overview">Description</button>
          <button class="tab ${d.tab === 'versions' ? 'active' : ''}" data-act="proj-tab" data-tab="versions">Versions${versions.length ? `<span class="count">${versions.length}</span>` : ''}</button>
        </div>
        ${d.tab === 'versions' ? versionsHtml : overview}
      </div>
      <aside class="proj-side">
        <div class="side-panel">
          <h4>Details</h4>
          <dl class="detail-grid">
            <dt>Source</dt><dd>${esc(p.source === 'curseforge' ? 'CurseForge' : 'Modrinth')}</dd>
            ${p.license ? `<dt>License</dt><dd>${esc(p.license)}</dd>` : ''}
            ${p.published ? `<dt>Published</dt><dd>${esc(fmtDate(p.published))}</dd>` : ''}
            ${p.updated ? `<dt>Updated</dt><dd>${esc(fmtDate(p.updated))}</dd>` : ''}
            ${p.clientSide ? `<dt>Client</dt><dd>${esc(p.clientSide)}</dd>` : ''}
            ${p.serverSide ? `<dt>Server</dt><dd>${esc(p.serverSide)}</dd>` : ''}
            ${versions.length ? `<dt>Builds</dt><dd>${versions.length} for your version</dd>` : ''}
          </dl>
        </div>
        ${sideLinks.length ? `<div class="side-panel">
          <h4>Links</h4>
          <div class="side-links">${sideLinks.map(([label, url]) => `
            <a class="btn sm ghost" href="${url}" target="_blank" rel="noreferrer">${esc(label)}</a>`).join('')}
          </div>
        </div>` : ''}
      </aside>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------ updates */

function renderUpdates() {
  const view = $('#view');
  view.innerHTML = `<div class="view-inner">
    <div class="toolbar" style="justify-content:space-between">
      <div class="muted" style="font-size:12px">Identifies installed jars on Modrinth by their SHA-1 hash, then checks for newer builds that support your exact loader and Minecraft version.</div>
      <div class="row-gap">
        <button class="btn" data-act="check-updates" ${state.updatesLoading ? 'disabled' : ''}>
          ${state.updatesLoading ? '<span class="spinner"></span>' : ''} Check for updates
        </button>
        <button class="btn primary" data-act="update-all" ${!state.updates || !state.updates.rows.some((r) => r.status === 'update') ? 'disabled' : ''}>
          Update all
        </button>
      </div>
    </div>
    <div id="upd-body"></div>
  </div>`;
  paintUpdates();
}

function paintUpdates() {
  const host = $('#upd-body');
  if (!host) return;
  if (state.updatesLoading) {
    host.innerHTML = `<div class="skeleton" style="height:60px;margin-bottom:8px"></div>
      <div class="skeleton" style="height:60px;margin-bottom:8px"></div>
      <div class="skeleton" style="height:60px"></div>`;
    return;
  }
  if (state.updatesError) {
    host.innerHTML = `<div class="empty">${ICON.warn}<h3>Update check failed</h3><p>${esc(state.updatesError)}</p></div>`;
    return;
  }
  if (!state.updates) {
    host.innerHTML = `<div class="empty">${ICON.download}<h3>Not checked yet</h3>
      <p>Run a check to see which of your mods have newer builds for ${esc(state.inst ? state.inst.mcVersion : 'your version')}.</p></div>`;
    return;
  }
  const rows = state.updates.rows;
  if (!rows.length) {
    host.innerHTML = `<div class="empty">${ICON.box}<h3>No mods installed</h3><p>Add mods from Discover, then check for updates here.</p></div>`;
    return;
  }
  const label = {
    update: 'Update available', current: 'Up to date', 'no-compatible': 'No build for your version',
    unidentified: 'Not on Modrinth', error: 'Lookup failed', duplicate: 'Duplicate copy',
  };
  host.innerHTML = `<div class="rows">${rows.map((r) => `
    <div class="upd-row">
      <div class="micon">${r.icon ? `<img src="${esc(r.icon)}" alt="" loading="lazy" onerror="this.remove()">` : esc((r.title || r.file).slice(0, 1).toUpperCase())}</div>
      <div style="min-width:0">
        <div class="upd-title">${esc(r.title || r.file)} <span class="tag ${esc(r.status)}">${esc(label[r.status] || r.status)}</span></div>
        <div class="upd-sub">
          <code>${esc(r.file)}</code>
          ${r.currentVersion ? ` · installed ${esc(r.currentVersion)}` : ''}
          ${r.status === 'update' && r.latest ? ` → <b style="color:var(--accent)">${esc(r.latest.versionNumber)}</b> (${esc(relDate(r.latest.published))})` : ''}
          ${r.status === 'no-compatible' ? ` · no ${esc(state.inst.loader)} ${esc(state.inst.mcVersion)} build yet` : ''}
          ${r.status === 'duplicate' ? ` · older copy of the same project as ${esc(r.duplicateOf || 'another jar')}` : ''}
          ${r.status === 'unidentified' ? ' · hash not found on Modrinth (may be a dev build or from CurseForge)' : ''}
        </div>
      </div>
      <div class="upd-actions">
        ${r.status === 'update' ? `<button class="btn sm primary" data-act="do-update"
            data-project="${esc(r.projectId)}" data-version="${esc(r.latest.id)}" data-file="${esc(r.file)}">Update</button>` : ''}
        ${r.status === 'duplicate' ? `<button class="btn sm" data-act="disable-file" data-file="${esc(r.file)}">Disable copy</button>` : ''}
        ${r.status === 'no-compatible' || r.status === 'unidentified' ? `<button class="btn sm" data-act="find-name" data-q="${esc(r.title || r.file.replace(/\.jar$/, ''))}">Find</button>` : ''}
      </div>
    </div>`).join('')}</div>`;
}

async function checkUpdates() {
  if (!state.inst) return;
  state.updatesLoading = true;
  state.updatesError = null;
  paintUpdates();
  const btn = $('[data-act="check-updates"]');
  if (btn) btn.disabled = true;
  try {
    state.updates = await api('updates?instance=' + encodeURIComponent(state.inst.id));
    renderTabs();
  } catch (e) {
    state.updatesError = e.message;
  } finally {
    state.updatesLoading = false;
    const host = $('#upd-body');
    if (host) paintUpdates(); else renderUpdates();
    const b = $('[data-act="check-updates"]');
    if (b) b.disabled = false;
    const all = $('[data-act="update-all"]');
    if (all) all.disabled = !state.updates || !state.updates.rows.some((r) => r.status === 'update');
  }
}

async function doUpdate(projectId, versionId, file) {
  try {
    toast(`Updating ${file}…`);
    const res = await api('install', { method: 'POST', body: {
      instance: state.inst.id, source: 'modrinth', projectId, versionId, installDeps: true,
    } });
    if (res.needsManual) { toast(res.message, 'warn', 7000); return; }
    toast(`Updated ${res.installed.map((i) => i.file).join(', ')}`);
    state.updates = null;
    await refreshScan();
    await checkUpdates();
  } catch (e) {
    toast(e.message, 'error', 7000);
  }
}

async function updateAll() {
  if (!state.updates) return;
  const todo = state.updates.rows.filter((r) => r.status === 'update');
  const skipped = state.updates.rows.filter((r) => r.status === 'duplicate').length;
  if (!todo.length) return;
  let ok = 0, fail = 0;
  for (const row of todo) {
    try {
      const res = await api('install', { method: 'POST', body: {
        instance: state.inst.id, source: 'modrinth', projectId: row.projectId,
        versionId: row.latest.id, installDeps: true,
      } });
      if (res.needsManual) fail++; else ok++;
    } catch { fail++; }
  }
  toast(`Updated ${ok} mod${ok === 1 ? '' : 's'}${fail ? `, ${fail} failed` : ''}${skipped ? ` · skipped ${skipped} duplicate cop${skipped === 1 ? 'y' : 'ies'}` : ''}`,
    fail ? 'warn' : 'ok', 6000);
  await refreshScan();
  await checkUpdates();
}

/* ----------------------------------------------------------------- settings */

function renderSettings() {
  const view = $('#view');
  const inst = state.inst;
  view.innerHTML = `<div class="view-inner" style="max-width:880px">
    <div class="panel-block">
      <h3>CurseForge API key</h3>
      <p class="hint">CurseForge requires a free key to search its catalogue. Create one at
        <a href="https://console.curseforge.com" target="_blank" rel="noreferrer">console.curseforge.com</a>
        and paste it here. It is stored locally in <code>settings.json</code> inside ModDeck's data folder and
        only sent to api.curseforge.com.
        Modrinth works without a key.</p>
      <div class="field">
        <input type="password" id="cf-key" placeholder="${state.hasCfKey ? 'A key is already saved — paste a new one to replace it' : 'Paste your CurseForge API key'}" autocomplete="off">
        <button class="btn primary" data-act="save-key">Save key</button>
        ${state.hasCfKey ? '<button class="btn ghost" data-act="clear-key">Remove</button>' : ''}
      </div>
    </div>

    <div class="panel-block">
      <h3>Instances</h3>
      <p class="hint">Auto-detected launchers appear here automatically. Add any other folder (an instance folder or its mods folder) manually.</p>
      <div class="settings-list">
        ${state.instances.map((i) => `<div class="settings-row">
          <span class="inst-dot" style="background:${i.id === (inst && inst.id) ? 'var(--accent)' : 'var(--line-2)'}"></span>
          <div class="grow">
            <div class="nm">${esc(i.name)} ${loaderChip(i.loader)}</div>
            <div class="pt">${esc(prettyPath(i.path))}</div>
          </div>
          <button class="btn sm ghost" data-act="open-path" data-path="${esc(i.path)}">Open</button>
          ${i.type === 'custom' ? `<button class="btn sm ghost danger" data-act="remove-instance" data-id="${esc(i.id)}">Remove</button>` : ''}
        </div>`).join('')}
      </div>
      <div class="row-gap" style="margin-top:12px">
        <button class="btn" data-act="add-instance">+ Add folder</button>
      </div>
    </div>

    <div class="panel-block">
      <h3>How ModDeck decides what's safe</h3>
      <div class="note">
        It reads each jar's own metadata (<code>fabric.mod.json</code> / <code>META-INF/mods.toml</code>):
        mod id, version, declared dependencies, and any <code>breaks</code>/<code>conflicts</code> entries —
        and only flags a conflict if the installed version actually falls inside the broken range.
        It also checks for duplicate mod ids, loader mismatches, missing required dependencies,
        Minecraft version ranges, and a small list of known-bad pairs (OptiFine + Sodium, etc.).
        It cannot see class-level mixin clashes or behavioural bugs; treat a green score as
        "nothing obviously wrong", not a guarantee.
      </div>
    </div>

    <div class="panel-block">
      <h3>Behaviour</h3>
      <label class="check-line">
        <input type="checkbox" id="ask-delete-toggle" ${state.askDeleteOnDisable ? 'checked' : ''}>
        Ask to delete a file after disabling it
      </label>
      <p class="hint" style="margin:8px 0 0">When enabled, disabling a mod (including older duplicate copies)
        offers to move the disabled jar to the trash. Keep it in <code>mods.disabled</code> if you might
        want to re-enable it later.</p>
    </div>

    <div class="panel-block">
      <div class="row-gap" style="justify-content:space-between;align-items:flex-start;margin-bottom:10px">
        <div>
          <h3>Trash</h3>
          <p class="hint" style="margin:0">Removed and replaced jars are kept here. Restore one to the current
            instance, or delete it permanently.</p>
        </div>
        <button class="btn sm danger" data-act="trash-empty">Empty trash</button>
      </div>
      <div id="trash-body"></div>
    </div>

    <div class="panel-block">
      <h3>Data & safety</h3>
      <p class="hint">Disabling moves a jar to <code>mods.disabled</code> next to your mods folder. Removing moves it to
        <code>${esc(state.dataDir || 'ModDeck/data')}/trash/</code>. Nothing is permanently deleted unless you empty
        the trash, so you can always undo by hand.</p>
      <div class="row-gap">
        <button class="btn ghost" data-act="open-data">Open ModDeck data folder</button>
        <button class="btn ghost danger" data-act="quit">Quit ModDeck</button>
      </div>
    </div>
  </div>`;

  const askToggle = $('#ask-delete-toggle');
  if (askToggle) {
    askToggle.addEventListener('change', async (e) => {
      await setAskDelete(e.target.checked);
      toast(e.target.checked ? 'ModDeck will ask before deleting disabled files' : 'Disabled files will be kept without asking');
    });
  }
  loadTrash();
}

async function loadTrash() {
  try {
    state.trash = await api('trash');
  } catch (e) {
    state.trash = { files: [], totalSize: 0, totalHuman: '—', error: e.message };
  }
  paintTrash();
}

function paintTrash() {
  const host = $('#trash-body');
  if (!host) return;
  const t = state.trash;
  if (!t) {
    host.innerHTML = '<div class="skeleton" style="height:46px"></div>';
    return;
  }
  if (t.error) {
    host.innerHTML = `<div class="note">Could not read the trash: ${esc(t.error)}</div>`;
    return;
  }
  if (!t.files.length) {
    host.innerHTML = '<div class="note muted" style="border-left-color:var(--line-2)">Trash is empty.</div>';
    return;
  }
  host.innerHTML = `<div class="trash-list">${t.files.map((f) => `
    <div class="trash-row">
      <div class="grow">
        <div class="nm">${esc(f.file)}</div>
        <div class="pt">${esc(f.instance || 'unknown instance')} · ${esc(f.sizeHuman)} ·
          ${esc(relDate(new Date((f.mtime || 0) * 1000).toISOString()))}</div>
      </div>
      <button class="btn sm" data-act="trash-restore" data-rel="${esc(f.rel)}">Restore</button>
      <button class="btn sm ghost danger" data-act="trash-delete" data-rel="${esc(f.rel)}">Delete</button>
    </div>`).join('')}</div>
    <div class="trash-total">${t.files.length} file${t.files.length === 1 ? '' : 's'} · ${esc(t.totalHuman)}</div>`;
}

/* -------------------------------------------------------------- interactions */

async function refreshScan() {
  if (!state.inst) return;
  state.loadingScan = true;
  if (!state.scan) render();
  else renderHeader();
  try {
    state.scan = await api('mods?instance=' + encodeURIComponent(state.inst.id));
    state.scanError = null;
    const found = state.instances.find((i) => i.id === state.inst.id);
    if (found) found.modsCount = state.scan.counts.enabled;
  } catch (e) {
    state.scanError = e.message;
  } finally {
    state.loadingScan = false;
    render();
  }
}

async function selectInstance(id) {
  const inst = state.instances.find((i) => i.id === id);
  if (!inst) return;
  state.inst = inst;
  state.detail = null;
  state.scan = null;
  state.q = '';
  state.filter = 'all';
  state.updates = null;
  state.tab = state.tab === 'settings' ? 'settings' : state.tab;
  render();
  await refreshScan();
  api('settings', { method: 'POST', body: { lastInstance: id } }).catch(() => {});
  localStorage.setItem('moddeck.inst', id);
}

async function scanAfterAction() {
  await refreshScan();
  state.updates = null;
  if (state.tab === 'updates') await checkUpdates();
}

async function setAskDelete(value) {
  state.askDeleteOnDisable = !!value;
  try { await api('settings', { method: 'POST', body: { askDeleteOnDisable: state.askDeleteOnDisable } }); }
  catch { /* keep the in-memory preference */ }
}

function maybeOfferDelete(files) {
  if (!state.askDeleteOnDisable || !files.length) return;
  const many = files.length > 1;
  openModal(`<div class="modal-head">
      <div><h2>Delete the disabled file${many ? 's' : ''}?</h2>
      <p>${many ? 'They are' : 'It is'} now in <code>mods.disabled</code>. Move ${many ? 'them' : 'it'} to the trash,
        or keep ${many ? 'them' : 'it'} so you can re-enable ${many ? 'them' : 'it'} later.</p></div>
      <button class="modal-x" data-act="close-modal">×</button>
    </div>
    <div class="modal-body">
      <div class="confirm-files">${files.map((f) => `<code>${esc(f)}</code>`).join('')}</div>
      <label class="check-line"><input type="checkbox" id="dont-ask-delete">
        Don't ask me again — disabled files stay in <code>mods.disabled</code></label>
    </div>
    <div class="modal-foot">
      <button class="btn ghost" data-act="close-modal">Keep ${many ? 'them' : 'it'}</button>
      <button class="btn danger" id="disable-delete-ok">Move to trash</button>
    </div>`);
  $('#dont-ask-delete').addEventListener('change', (e) => setAskDelete(!e.target.checked));
  $('#disable-delete-ok').addEventListener('click', async () => {
    closeModal();
    await doModAction('delete', files);
  });
}

async function doModAction(action, files) {
  try {
    const res = await api('mods/action', { method: 'POST', body: { instance: state.inst.id, action, files } });
    const failed = res.results.filter((r) => !r.ok);
    if (failed.length) toast(failed.map((f) => `${f.file}: ${f.error}`).join('; '), 'error', 7000);
    const done = res.results.filter((r) => r.ok);
    if (done.length) toast(`${action === 'delete' ? 'Moved to trash' : action === 'enable' ? 'Enabled' : 'Disabled'} ${done.length} file${done.length === 1 ? '' : 's'}`);
    await refreshScan();
    if (action === 'disable' && done.length) {
      maybeOfferDelete(done.map((r) => r.file).filter(Boolean));
    }
    state.updates = null;
    if (state.tab === 'updates') await checkUpdates();
  } catch (e) {
    toast(e.message, 'error', 7000);
  }
}

/* ------------------------------------------------------------------ shortcuts */

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (e.key === '/' && !typing) {
    const box = $('#mod-search') || $('#disc-search');
    if (box) { e.preventDefault(); box.focus(); }
  }
  if (!typing && (e.key === 'Enter' || e.key === ' ')) {
    const card = document.activeElement && document.activeElement.closest
      ? document.activeElement.closest('.card[data-act="open-project"]')
      : null;
    if (card) { e.preventDefault(); card.click(); }
  }
});

document.addEventListener('click', async (e) => {
  const el = e.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;

  if (act === 'close-modal') return closeModal();

  switch (act) {
    case 'select-inst': return selectInstance(el.dataset.id);
    case 'open-project':
      return openProject(el.dataset.source, el.dataset.id, el.dataset.title);
    case 'close-project':
      return closeProject();
    case 'proj-tab': {
      if (!state.detail) return;
      state.detail.tab = el.dataset.tab;
      render();
      return;
    }
    case 'gallery-set': {
      const img = $('#proj-hero-img');
      if (img) img.src = el.dataset.url;
      $$('.gallery-thumb').forEach((t) => t.classList.toggle('active', t === el));
      return;
    }
    case 'lightbox': {
      openModal(`<div class="lightbox"><img src="${esc(el.dataset.url)}" alt="">
        <button class="modal-x lightbox-x" data-act="close-modal">×</button></div>`, { wide: true });
      return;
    }
    case 'tab':
    case 'go-tab': {
      const tab = el.dataset.tab;
      state.detail = null;
      state.tab = tab;
      try { history.replaceState(null, '', '#' + tab); } catch { /* ignore */ }
      if (tab === 'discover') { render(); }
      else if (tab === 'updates') { render(); if (!state.updates && !state.updatesLoading) checkUpdates(); }
      else render();
      return;
    }
    case 'rescan': return refreshScan();
    case 'open-folder': {
      try { await api('open', { method: 'POST', body: { instance: state.inst.id, what: 'mods' } }); }
      catch (err) { toast(err.message, 'error'); }
      return;
    }
    case 'open-path': {
      try { await api('open', { method: 'POST', body: { path: el.dataset.path } }); }
      catch (err) { toast(err.message, 'error'); }
      return;
    }
    case 'open-data': {
      try { const st = await api('state'); await api('open', { method: 'POST', body: { path: st.dataDir } }); }
      catch (err) { toast(err.message, 'error'); }
      return;
    }
    case 'toggle-issues': {
      state.issuesOpen = !state.issuesOpen;
      const box = $('.issues');
      if (box) box.dataset.open = String(state.issuesOpen);
      return;
    }
    case 'filter': {
      state.filter = el.dataset.filter;
      paintFilters();
      paintModRows();
      return;
    }
    case 'toggle-row': {
      const row = el.closest('.mod-row');
      if (row) row.classList.toggle('open');
      return;
    }
    case 'toggle-mod': {
      const enabled = el.dataset.enabled === 'true';
      return doModAction(enabled ? 'disable' : 'enable', [el.dataset.file]);
    }
    case 'disable-file': return doModAction('disable', [el.dataset.file]);
    case 'fix-duplicate': {
      const keep = el.dataset.keep;
      const disable = (el.dataset.disable || '').split('|').filter(Boolean);
      return confirmModal({
        title: 'Fix duplicate mod?',
        body: `<span>Keeping <code>${esc(keep)}</code> and moving ${disable.length} older jar(s) to <code>mods.disabled</code>. You can re-enable them any time.</span>`,
        label: 'Disable older copies',
        onOk: () => doModAction('disable', disable),
      });
    }
    case 'delete-mod': {
      const file = el.dataset.file;
      return confirmModal({
        title: 'Remove this mod?',
        body: `<span><code>${esc(file)}</code> will be moved to <code>ModDeck/data/trash/</code> — not deleted, so you can restore it.</span>`,
        label: 'Move to trash',
        onOk: () => doModAction('delete', [file]),
      });
    }
    case 'bulk-disable':
    case 'bulk-enable': {
      const files = $$('.mod-check:checked').map((c) => c.value);
      if (!files.length) return;
      return doModAction(act === 'bulk-disable' ? 'disable' : 'enable', files);
    }
    case 'clear-selection': {
      $$('.mod-check:checked').forEach((c) => { c.checked = false; });
      paintBulk();
      return;
    }
    case 'disc-source': {
      state.discover.source = el.dataset.source;
      state.discover.offset = 0;
      state.discover.loaded = false;
      renderDiscover();
      return;
    }
    case 'disc-more': {
      const d = state.discover;
      d.offset = d.hits.length;
      return searchDiscover(true);
    }
    case 'pick-version': {
      return pickVersion(el.dataset.source, el.dataset.id, el.dataset.title);
    }
    case 'do-install': {
      const deps = $('#install-deps') ? $('#install-deps').checked : true;
      el.disabled = true;
      el.innerHTML = '<span class="spinner"></span>';
      try {
        const res = await api('install', { method: 'POST', body: {
          instance: state.inst.id, source: el.dataset.source, projectId: el.dataset.project,
          versionId: el.dataset.version, installDeps: deps,
        } });
        if (res.needsManual) {
          toast(res.message, 'warn', 8000);
          if (res.website) window.open(res.website, '_blank');
        } else {
          const names = res.installed.map((i) => i.file);
          toast(`Installed ${names.slice(0, 3).join(', ')}${names.length > 3 ? ` +${names.length - 3} more` : ''}`);
          if (res.errors && res.errors.length) toast(res.errors.join('; '), 'warn', 8000);
        }
        closeModal();
        await scanAfterAction();
        if (state.tab === 'discover' && $('#disc-results')) searchDiscover();
      } catch (err) {
        toast(err.message, 'error', 8000);
        el.disabled = false;
        el.textContent = 'Install';
      }
      return;
    }
    case 'check-updates': return checkUpdates();
    case 'do-update': return doUpdate(el.dataset.project, el.dataset.version, el.dataset.file);
    case 'update-all': return updateAll();
    case 'find-name': {
      state.detail = null;
      state.tab = 'discover';
      state.discover.q = el.dataset.q || '';
      state.discover.offset = 0;
      state.discover.loaded = false;
      render();
      return searchDiscover();
    }
    case 'find-dep': {
      state.detail = null;
      state.tab = 'discover';
      state.discover.q = el.dataset.dep || '';
      state.discover.offset = 0;
      state.discover.loaded = false;
      render();
      return searchDiscover();
    }
    case 'save-key': {
      const key = ($('#cf-key') && $('#cf-key').value.trim()) || '';
      try {
        await api('settings', { method: 'POST', body: { curseforgeKey: key } });
        state.hasCfKey = !!key;
        toast(key ? 'CurseForge key saved' : 'Key cleared');
        renderSettings();
      } catch (err) { toast(err.message, 'error'); }
      return;
    }
    case 'clear-key': {
      await api('settings', { method: 'POST', body: { curseforgeKey: '' } });
      state.hasCfKey = false;
      toast('CurseForge key removed');
      renderSettings();
      return;
    }
    case 'trash-restore': {
      if (!state.inst) return toast('Select an instance first', 'warn');
      try {
        const res = await api('trash/restore', { method: 'POST', body: { instance: state.inst.id, rel: el.dataset.rel } });
        toast(`Restored ${res.file} to ${state.inst.name}`);
        await loadTrash();
        await scanAfterAction();
      } catch (err) { toast(err.message, 'error', 7000); }
      return;
    }
    case 'trash-delete': {
      const rel = el.dataset.rel;
      return confirmModal({
        title: 'Delete this file forever?',
        body: `<span><code>${esc(rel.split('/').pop())}</code> will be permanently deleted from the trash.
          This cannot be undone.</span>`,
        label: 'Delete forever',
        onOk: async () => {
          try {
            await api('trash/empty', { method: 'POST', body: { file: rel } });
            toast('Deleted from trash');
            await loadTrash();
          } catch (err) { toast(err.message, 'error'); }
        },
      });
    }
    case 'trash-empty': {
      return confirmModal({
        title: 'Empty the trash?',
        body: '<span>Every removed and replaced jar in the trash will be permanently deleted. This cannot be undone.</span>',
        label: 'Empty trash',
        onOk: async () => {
          try {
            await api('trash/empty', { method: 'POST', body: {} });
            toast('Trash emptied');
            await loadTrash();
          } catch (err) { toast(err.message, 'error'); }
        },
      });
    }
    case 'add-instance': return addInstanceModal();
    case 'remove-instance': {
      const iid = el.dataset.id;
      return confirmModal({
        title: 'Remove this instance?',
        body: '<span>This only removes the folder from ModDeck. Your files are untouched.</span>',
        label: 'Remove',
        onOk: async () => {
          try {
            const res = await api('instance/remove', { method: 'POST', body: { id: iid } });
            state.instances = res.instances;
            if (state.inst && state.inst.id === iid) state.inst = state.instances[0] || null;
            render();
            if (state.inst) await refreshScan();
          } catch (err) { toast(err.message, 'error'); }
        },
      });
    }
    case 'edit-version': return versionModal();
    case 'quit': {
      return confirmModal({
        title: 'Quit ModDeck?',
        body: '<span>The local server will stop. Your mods are untouched. You can close this window afterwards.</span>',
        label: 'Quit',
        onOk: async () => {
          try { await api('shutdown', { method: 'POST', body: {} }); } catch { /* server is gone */ }
          if (window.pywebview && window.pywebview.api && window.pywebview.api.quit_app) {
            try { await window.pywebview.api.quit_app(); return; } catch { /* fall through */ }
          }
          document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;color:#8b9bb0;font:14px system-ui">ModDeck has stopped. You can close this window.</div>';
        },
      });
    }
  }
});

/* -------------------------------------------------------------------- modals */

async function addInstanceModal() {
  openModal(`<div class="modal-head">
      <div><h2>Add an instance folder</h2>
      <p>Point this at any Minecraft folder — the one that contains <code>mods</code> — or at the mods folder itself.</p></div>
      <button class="modal-x" data-act="close-modal">×</button>
    </div>
    <div class="modal-body">
      <div class="field" style="margin-bottom:10px">
        <input type="text" id="new-path" placeholder="C:\\Users\\you\\AppData\\Roaming\\.minecraft">
        <button class="btn" id="browse-btn">Browse…</button>
      </div>
      <div class="field">
        <input type="text" id="new-name" placeholder="Display name (optional)">
      </div>
      <div class="note" style="margin-top:12px">Loader and Minecraft version are detected automatically from
        <code>mmc-pack.json</code> or <code>launcher_profiles.json</code>. You can correct them afterwards from the header.</div>
    </div>
    <div class="modal-foot">
      <button class="btn ghost" data-act="close-modal">Cancel</button>
      <button class="btn primary" id="add-ok">Add folder</button>
    </div>`);

  $('#browse-btn').addEventListener('click', async () => {
    const btn = $('#browse-btn');
    btn.disabled = true; btn.textContent = 'Choosing…';
    try {
      const res = await api('browse', { method: 'POST', body: {} });
      if (res.path) $('#new-path').value = res.path;
      else if (res.error) toast('Folder picker failed — paste the path instead.', 'warn');
    } catch (e) { toast(e.message, 'error'); }
    btn.disabled = false; btn.textContent = 'Browse…';
  });

  $('#add-ok').addEventListener('click', async () => {
    const path = $('#new-path').value.trim();
    const name = $('#new-name').value.trim();
    if (!path) return toast('Paste a folder path first', 'warn');
    try {
      const res = await api('instance/add', { method: 'POST', body: { path, name } });
      state.instances = res.instances;
      closeModal();
      toast('Instance added');
      await selectInstance(res.instance.id);
      if (!state.scan || !state.inst) render();
    } catch (e) { toast(e.message, 'error', 7000); }
  });
}

async function versionModal() {
  const tags = await ensureTags();
  const inst = state.inst;
  const mcList = (tags.allVersions || tags.gameVersions || []).map((v) => `<option value="${esc(v)}">`).join('');
  openModal(`<div class="modal-head">
      <div><h2>Target version for this instance</h2>
      <p>ModDeck checks mod compatibility and filters search results against these two values.</p></div>
      <button class="modal-x" data-act="close-modal">×</button>
    </div>
    <div class="modal-body">
      <div class="field" style="margin-bottom:12px">
        <select id="ver-loader" style="flex:0 0 180px;min-width:150px">
          ${['fabric', 'neoforge', 'forge', 'quilt', 'vanilla', 'unknown'].map((l) => `<option value="${l}" ${inst.loader === l ? 'selected' : ''}>${l}</option>`).join('')}
        </select>
        <input type="text" id="ver-mc" list="mc-versions" value="${esc(inst.mcVersion || '')}" placeholder="e.g. 26.3">
        <datalist id="mc-versions">${mcList}</datalist>
      </div>
      <div class="note">Detected automatically: <b>${esc(inst.detected.loader)}</b>${inst.detected.mcVersion ? ' · <b>' + esc(inst.detected.mcVersion) + '</b>' : ''}.
        Change this if you switch versions inside SKLauncher without changing folders.</div>
    </div>
    <div class="modal-foot">
      <button class="btn ghost" data-act="close-modal">Cancel</button>
      <button class="btn primary" id="ver-save">Save</button>
    </div>`);

  $('#ver-save').addEventListener('click', async () => {
    const loader = $('#ver-loader').value;
    const mcVersion = $('#ver-mc').value.trim();
    try {
      const res = await api('instance/update', { method: 'POST', body: { id: inst.id, loader, mcVersion } });
      const idx = state.instances.findIndex((i) => i.id === inst.id);
      if (idx >= 0) state.instances[idx] = res.instance;
      state.inst = res.instance;
      closeModal();
      toast('Target version saved');
      state.updates = null;
      state.discover.loaded = false;
      await refreshScan();
    } catch (e) { toast(e.message, 'error'); }
  });
}

/* ---------------------------------------------------------------------- boot */

window.addEventListener('hashchange', handleHash);

(async function boot() {
  try {
    const st = await api('state');
    state.instances = st.instances || [];
    state.hasCfKey = !!(st.settings && st.settings.curseforgeKey);
    state.askDeleteOnDisable = !(st.settings && st.settings.askDeleteOnDisable === false);
    state.dataDir = st.dataDir || '';
    state.frozen = !!st.frozen;
    if (!state.instances.length) {
      render();
      $('#view').innerHTML = `<div class="empty">${ICON.box}<h3>No Minecraft folders found</h3>
        <p>ModDeck looked in <code>%APPDATA%\\.minecraft</code> and the usual PollyMC/Prism folders.
        Add your instance folder manually to begin.</p>
        <div style="margin-top:16px"><button class="btn primary" data-act="add-instance">Add folder</button></div></div>`;
      return;
    }
    const saved = localStorage.getItem('moddeck.inst');
    const wanted = state.instances.find((i) => i.id === st.settings.lastInstance)
      || state.instances.find((i) => i.id === saved)
      || state.instances[0];
    const hash = location.hash.replace('#', '');
    const projectHash = hash.match(/^project\/(modrinth|curseforge)\/(.+)$/);
    if (['mods', 'discover', 'updates', 'settings'].includes(hash)) state.tab = hash;
    await selectInstance(wanted.id);
    if (projectHash) await openProject(projectHash[1], decodeURIComponent(projectHash[2]));
    else if (state.tab === 'updates') checkUpdates();
  } catch (e) {
    $('#view').innerHTML = `<div class="empty">${ICON.warn}<h3>ModDeck could not start</h3><p>${esc(e.message)}</p></div>`;
  }
})();
