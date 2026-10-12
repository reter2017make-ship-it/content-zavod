// Shared helpers for all pages: sidebar, API calls, toasts, labels and colours.
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];

const STATUS = {
  DISCOVERED: ['найден', 'info'], DISCOVERY_ONLY: ['только ссылка', ''], DOWNLOADING: ['скачивается', 'info'],
  // no «одобрен / отклонён / на проверке»: every downloaded video can be edited (owner's decision 2026-10-07)
  DOWNLOADED: ['', ''], ANALYZING: ['разбирается', 'info'], MANUAL_REVIEW: ['', ''],
  APPROVED: ['', ''], PROCESSING: ['монтаж', 'accent'], PROCESSED: ['', ''],
  READY_TO_PUBLISH: ['', ''], PUBLISHED: ['опубликован', 'ok'], REJECTED: ['', ''],
  DUPLICATE: ['копия', ''], FAILED: ['не скачалось', 'err'],
};
const CAT_SOURCE = { ai: 'нейросеть', rules: 'правила', folder: 'папка', manual: 'вручную' };
// Hand-picked so every pair (text on tint) stays readable on white.
const CAT_COLORS = {
  recipes: '#c2410c', food: '#b45309', memes: '#7c3aed', funny: '#db2777', stream_clips: '#6d28d9',
  sports: '#15803d', gaming: '#2563eb', animals: '#a16207', cars: '#475569', luxury: '#a16207',
  travel: '#0e7490', facts: '#0369a1', science: '#0f766e', technology: '#4338ca', relationships: '#be185d',
  fails: '#b91c1c', motivation: '#c2410c', news: '#334155', movies: '#9333ea', series: '#7e22ce',
  music: '#c026d3', other: '#64748b', uncertain: '#94a3b8',
};
const catColor = id => CAT_COLORS[id] || '#64748b';
const catBadge = (id, label) => {
  const c = catColor(id);
  return `<span class="badge cat" style="color:${c};background:${c}14">${esc(label || id || '—')}</span>`;
};
const statusBadge = st => { const [t, k] = STATUS[st] || [st, '']; return t ? `<span class="badge ${k}">${t}</span>` : ''; };

function esc(t) {
  return (t ?? '').toString().replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
async function api(url, opt = {}) {
  const r = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opt });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
function toast(text, kind = '') {
  let box = $('.toasts');
  if (!box) { box = document.createElement('div'); box.className = 'toasts'; document.body.append(box); }
  const t = document.createElement('div');
  t.className = 'toast ' + kind; t.textContent = text; box.append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 6000 : 3200);
}
function ago(iso) {
  if (!iso) return 'ещё не проверялся';
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return 'только что';
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
  return new Date(iso).toLocaleString('ru');
}
const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
};

let CATEGORIES = [];
async function loadCategories() {
  CATEGORIES = (await api('/api/categories')).categories;
  return CATEGORIES;
}
const catOptions = sel => CATEGORIES.map(c => `<option value="${c.id}" ${c.id === sel ? 'selected' : ''}>${esc(c.label)}</option>`).join('');

function renderSidebar(active) {
  const link = (href, ico, text, id) => `<a href="${href}" class="${active === id ? 'on' : ''}"><span class="ico">${ico}</span>${text}</a>`;
  $('#sidebar').innerHTML = `
    <div class="brand"><div class="brand-mark">▶</div>Контент-завод</div>
    <nav class="nav">
      ${link('/', '🏠', 'Сводка', 'dashboard')}
      ${link('/sources', '📡', 'Источники', 'sources')}
      ${link('/videos', '🎞', 'Ролики', 'videos')}
      ${link('/editor', '✂️', 'Редактор', 'editor')}
      ${link('/backgrounds', '🖼', 'Фоны', 'backgrounds')}
      ${link('/ready', '✅', 'Готовые видео', 'ready')}
      ${link('/instagram', '📸', 'Instagram', 'instagram')}
      ${link('/calendar', '🗓', 'Календарь', 'calendar')}
    </nav>
    <div class="side-status" id="sideStatus"><span><span class="dot"></span>подключаюсь…</span></div>`;
  refreshSideStatus();
  setInterval(refreshSideStatus, 3000);
}
async function refreshSideStatus() {
  try {
    const w = await api('/api/worker');
    const busy = w.scanning_channels || w.queued_channels || w.downloads_in_progress || w.processing;
    $('#sideStatus').innerHTML = `
      <span><span class="dot ${busy ? 'busy' : 'live'}"></span>${busy ? 'Завод работает' : 'Завод ждёт новых постов'}</span>
      ${w.scanning_channels || w.queued_channels ? `<span>Каналы: <b>${w.scanning_channels}</b> сканируется, <b>${w.queued_channels}</b> в очереди</span>` : ''}
      ${w.downloads_in_progress ? `<span>Скачивается: <b>${w.downloads_in_progress}</b></span>` : ''}
      ${w.processing ? '<span>Идёт анализ и монтаж</span>' : ''}
      ${w.blocked_until ? `<span style="color:var(--err)">Telegram ограничил запросы до ${new Date(w.blocked_until).toLocaleTimeString('ru')}</span>` : ''}
      <span class="muted">${w.next_scan ? 'Следующий парсинг: ' + new Date(w.next_scan).toLocaleString('ru', { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : 'Парсинг по расписанию выключен'}</span>`;
  } catch { $('#sideStatus').innerHTML = '<span><span class="dot"></span>нет связи с сервером</span>'; }
}

// ---------- publication description (written by a person; nothing is generated) ----------
const PUB_STATUS = {
  QUEUED: ['в очереди', 'accent'], PROCESSING: ['отправляется', 'info'], PUBLISHED: ['опубликован', 'ok'],
  FAILED: ['ошибка, будет повтор', 'warn'], WAITING_USER: ['ждёт вас', 'err'], CANCELLED: ['отменён', ''],
};
const pubBadge = st => { const [t, k] = PUB_STATUS[st] || [st, '']; return `<span class="badge ${k}">${t}</span>`; };
const fmtLocal = iso => iso ? new Date(iso).toLocaleString('ru', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';

// Renders a textarea with counters and the ~10 latest descriptions; a click on one puts it in the field.
async function descEditor(root, value, onChange) {
  root.innerHTML = `
    <textarea class="desc" maxlength="2200" placeholder="Текст под роликом в Instagram: напишите сами или выберите из последних ниже">${esc(value || '')}</textarea>
    <div class="row small muted" style="justify-content:space-between;margin-top:4px"><span class="dc"></span><span>не больше 2200 символов, 30 хэштегов, 20 упоминаний</span></div>
    <div class="recent"><div class="small muted" style="margin:8px 0 6px">Последние описания — нажмите, чтобы подставить:</div><div class="chips rc"></div></div>`;
  const ta = $('textarea', root);
  const count = () => {
    const t = ta.value;
    const tags = (t.match(/(^|[^\w])#[\p{L}\p{N}_]+/gu) || []).length, ats = (t.match(/(^|[^\w@])@[\w.]+/g) || []).length;
    const over = t.length > 2200 || tags > 30 || ats > 20;
    $('.dc', root).innerHTML = `<span style="color:${over ? 'var(--err)' : 'inherit'}">${t.length} симв. · ${tags} хэштегов · ${ats} упоминаний</span>`;
    onChange && onChange(t);
  };
  ta.oninput = count; count();
  try {
    const list = (await api('/api/publishing/descriptions')).descriptions;
    $('.rc', root).innerHTML = list.length ? list.map((d, i) => `<button type="button" class="chip desc-chip" data-i="${i}" title="${esc(d)}">${esc(d.length > 60 ? d.slice(0, 60) + '…' : d)}</button>`).join('')
      : '<span class="small muted">Пока пусто: здесь появятся описания, которые вы уже писали.</span>';
    $$('.desc-chip', root).forEach(b => b.onclick = () => { ta.value = list[+b.dataset.i]; count(); ta.focus(); });
  } catch { $('.recent', root).hidden = true; }
  return ta;
}

// Source resolution with a warning when it is too small for a sharp 1080×1920 result.
function srcQuality(q) {
  if (!q || !q.width) return '<span class="muted">неизвестно</span>';
  const short = Math.min(q.width, q.height);
  const warn = short < 540 ? 'err' : short < 720 ? 'warn' : '';
  return `<span class="badge ${warn || 'ok'}" title="${warn ? 'Исходник низкого разрешения: в 1080×1920 будет мягкая картинка' : 'Нормальное разрешение'}">${q.width}×${q.height}${q.fps ? ' · ' + Math.round(q.fps) + ' fps' : ''}</span>${warn ? ' <span class="small muted">' + (warn === 'err' ? 'низкое качество исходника' : 'среднее качество исходника') + '</span>' : ''}`;
}

// Telegram channel as @name; a local folder as its folder name.
const chanLabel = i => i.source === 'telegram' && i.channel ? '@' + esc(i.channel)
  : i.channel ? '📁 ' + esc(i.channel.split('/').filter(Boolean).pop()) : esc(i.source);

// ---------- Instagram AutoPoster: «Добавить Reel» (one video or many) ----------
async function igEnabled() {
  try { return (await api('/api/instagram-web/enabled')).enabled; } catch { return false; }
}
function igAddDialog(items, onDone) {
  // items: [{id, title, description}] — one description for all of them
  const one = items.length === 1, pad = n => String(n).padStart(2, '0');
  const d = new Date(Date.now() + 3600e3), date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`, time = `${pad(d.getHours())}:00`;
  const shared = new Set(items.map(i => (i.description || '').trim())), start = shared.size === 1 ? [...shared][0] : '';
  const bg = document.createElement('div'); bg.className = 'modal-bg open'; bg.style.zIndex = 30;
  bg.innerHTML = `<div class="modal" style="display:block;max-width:600px;padding:22px 24px;max-height:calc(100vh - 48px);overflow:auto">
    <div class="row" style="justify-content:space-between"><h2>${one ? 'Добавить Reel' : `Добавить в Instagram: ${items.length} ${plural(items.length, 'ролик', 'ролика', 'роликов')}`}</h2><button class="btn ghost" data-x>✕</button></div>
    <div style="display:flex;gap:6px;overflow-x:auto;margin:10px 0">${items.map(i => `<img src="/media/thumb/${i.id}" title="${esc(i.title || '#' + i.id)}" alt="" style="width:46px;aspect-ratio:9/16;object-fit:cover;border-radius:6px;flex:none;background:var(--surface-2)">`).join('')}</div>
    <label class="field">Описание${one ? '' : ' (одно для всех отмеченных)'}<textarea id="igCap" maxlength="2200" style="min-height:120px" placeholder="Текст под Reel: эмодзи, хэштеги и переносы строк сохранятся как есть">${esc(start)}</textarea></label>
    <div class="row small" style="justify-content:space-between;margin-top:4px"><select id="igRecent" style="max-width:72%;font-size:12px;padding:3px 6px"><option value="">вставить из последних…</option></select><span class="muted" id="igCnt"></span></div>
    <div style="margin-top:12px"><b class="small">Обложка</b> <span class="small muted">— выбранный для ролика фон</span>
      <div style="display:flex;gap:6px;overflow-x:auto;margin-top:6px">${items.map(i => `<span style="flex:none;text-align:center"><img src="/api/instagram-web/cover-preview/${i.id}" alt="" style="width:46px;aspect-ratio:9/16;object-fit:cover;border-radius:6px;background:var(--surface-2);display:block" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'badge',textContent:'кадр Instagram'}))"></span>`).join('')}</div>
      ${one ? `<details style="margin-top:6px"><summary class="small muted" style="cursor:pointer">выбрать другую обложку вместо фона</summary><input type="file" id="igCover" accept="image/*" style="margin-top:6px"></details>` : ''}</div>
    <label class="field" style="margin-top:10px">Аккаунт<select id="igAcc"><option value="">активный</option></select></label>
    <div style="margin-top:12px"><b class="small">Публикация</b>
      <div class="row small" style="margin-top:6px"><label class="row" style="gap:6px"><input type="radio" name="when" value="now" checked> ${one ? 'как можно скорее' : 'по очереди, как можно скорее'}</label>
        <label class="row" style="gap:6px"><input type="radio" name="when" value="scheduled"> по расписанию</label>
        <input type="date" id="igDate" value="${date}" style="font:inherit;padding:5px 8px;border:1px solid var(--line-2);border-radius:8px" disabled>
        <input type="time" id="igTime" value="${time}" style="font:inherit;padding:5px 8px;border:1px solid var(--line-2);border-radius:8px" disabled></div>
      <p class="small muted" style="margin:6px 0 0">В очереди ролик ждёт кнопки «Опубликовать сейчас» на странице «Instagram». Описание можно поправить там же.</p></div>
    <div class="row" style="margin-top:16px;justify-content:flex-end"><button class="btn" data-x>Отмена</button><button class="btn primary" id="igGo">Добавить в очередь</button></div>
  </div>`;
  document.body.append(bg);
  const close = () => bg.remove(), cap = $('#igCap', bg);
  $$('[data-x]', bg).forEach(b => b.onclick = close);
  const paint = () => { const n = cap.value.trim().length; $('#igCnt', bg).textContent = n ? `${cap.value.length} / 2200` : 'нет описания'; cap.style.borderColor = n ? '' : 'var(--warn)'; };
  cap.oninput = paint; paint();
  api('/api/publishing/descriptions?limit=15').then(r => {
    const sel = $('#igRecent', bg);
    sel.innerHTML += r.descriptions.map((t, i) => `<option value="${i}">${esc(t.length > 70 ? t.slice(0, 70) + '…' : t)}</option>`).join('');
    sel.onchange = () => { if (sel.value === '') return; cap.value = r.descriptions[+sel.value]; sel.value = ''; paint(); };
  }).catch(() => { $('#igRecent', bg).hidden = true; });
  api('/api/instagram-web/status').then(st => {
    const cur = st.account && st.account.username;
    $('#igAcc', bg).innerHTML = (st.accounts && st.accounts.length ? st.accounts.filter(a => !a.excluded) : (cur ? [{ username: cur }] : []))
      .map(a => `<option value="${esc(a.username)}" ${a.username === cur ? 'selected' : ''}>@${esc(a.username)}${a.username === cur ? ' (активный)' : ''}</option>`).join('') || '<option value="">активный</option>';
  }).catch(() => {});
  $$('[name=when]', bg).forEach(r => r.onchange = () => { const s = $('[name=when]:checked', bg).value === 'scheduled'; $('#igDate', bg).disabled = $('#igTime', bg).disabled = !s; });
  $('#igGo', bg).onclick = async () => {
    if (!cap.value.trim() && !confirm('Опубликовать без описания?')) return;
    const b = $('#igGo', bg); b.disabled = true;
    try {
      let cover = null;
      const f = one && $('#igCover', bg).files[0];
      if (f) { const fd = new FormData(); fd.append('file', f); const r = await fetch('/api/instagram-web/queue/cover', { method: 'POST', body: fd }); const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText); cover = j.cover_path; }
      const when = $('[name=when]:checked', bg).value, captions = {};
      items.forEach(i => captions[i.id] = cap.value);  // saved to every video's card as well
      const r = await api('/api/instagram-web/queue', { method: 'POST', body: JSON.stringify({ item_ids: items.map(i => i.id), captions, cover_path: cover, account: $('#igAcc', bg).value || null, when, scheduled_at: when === 'scheduled' ? `${$('#igDate', bg).value}T${$('#igTime', bg).value}` : null }) });
      const bad = Object.values(r.errors);
      toast(`В очереди Instagram: ${r.added.length}${bad.length ? ` · не добавлено ${bad.length}: ${bad[0]}` : ''}`, bad.length && !r.added.length ? 'err' : 'ok');
      close(); onDone && onDone(r);
    } catch (e) { toast(e.message, 'err'); b.disabled = false; }
  };
}

// edit the description of a queued Reel
function igEditCaption(pid, current, onDone) {
  const bg = document.createElement('div'); bg.className = 'modal-bg open'; bg.style.zIndex = 30;
  bg.innerHTML = `<div class="modal" style="display:block;max-width:560px;padding:22px 24px"><div class="row" style="justify-content:space-between"><h2>Описание</h2><button class="btn ghost" data-x>✕</button></div>
    <textarea id="igEd" maxlength="2200" style="min-height:140px;margin-top:10px">${esc(current || '')}</textarea>
    <div class="row" style="margin-top:12px;justify-content:flex-end"><button class="btn" data-x>Отмена</button><button class="btn primary" id="igEdGo">Сохранить</button></div></div>`;
  document.body.append(bg);
  $$('[data-x]', bg).forEach(b => b.onclick = () => bg.remove());
  $('#igEdGo', bg).onclick = async () => {
    try { await api('/api/instagram-web/queue/' + pid, { method: 'PATCH', body: JSON.stringify({ caption: $('#igEd', bg).value }) }); toast('Описание сохранено', 'ok'); bg.remove(); onDone && onDone(); }
    catch (e) { toast(e.message, 'err'); }
  };
}
