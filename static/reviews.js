'use strict';
// Отзывы об инструментах: вкладка сбоку карточки + выдвижная панель.
// Один человек — один отзыв на инструмент (его можно изменить или удалить).
// Загружается после app.js и пользуется его глобальными K, RATINGS, plural, starsAvgSvg, root, update.
(function () {
  const $ = id => document.getElementById(id);
  const MIN = 3, MAX = 1000, WIDE = 1280;
  const tab = $('revTab'), mBtn = $('revM'), box = $('rev');
  const SEP = ' › ';
  const ICON_CHAT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.4 8.4 0 0 1-12.2 7.5L3.5 20.5l1.6-4.8A8.4 8.4 0 1 1 21 11.5z"/></svg>';

  let tool = '', isOpen = false, seq = 0;
  let COUNTS = {};                 // инструмент -> число отзывов
  let st = null;                   // состояние открытого инструмента: {tool, loading, error, items, more, mine, total, banned}
  let editing = false, confirmDel = false, busy = false;
  const drafts = {};               // неотправленные черновики: инструмент -> текст

  // ---------- мелочи ----------
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? '' : v);
    }
    kids.flat().forEach(c => { if (c != null && c !== false) e.append(c); });
    return e;
  }
  const word = (n, f) => plural(n, f);
  const reviewsWord = n => word(n, ['отзыв', 'отзыва', 'отзывов']);
  const shortName = t => String(t || '').split(SEP).pop();

  function fmtDate(iso) {
    const d = new Date(iso), sec = (Date.now() - d.getTime()) / 1000;
    if (!(sec > 60)) return 'только что';
    if (sec < 3600) return Math.floor(sec / 60) + ' мин назад';
    if (sec < 86400) return Math.floor(sec / 3600) + ' ч назад';
    const o = { day: 'numeric', month: 'short' };
    if (d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric';
    return d.toLocaleDateString('ru-RU', o).replace(' г.', '');
  }

  function starsRow(n) {
    const s = h('span', { class: 'rv-stars', role: 'img', 'aria-label': n + ' из 5' });
    for (let i = 1; i <= 5; i++) s.append(h('span', { class: i <= n ? 'on' : '', text: '★' }));
    return s;
  }

  async function api(path, body) {
    const opt = { headers: { 'X-Access-Key': (typeof K === 'string' && K) || '' } };
    if (body) { opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    try {
      const r = await fetch(path, opt);
      let d = {}; try { d = await r.json(); } catch (e) {}
      return { ok: r.ok, status: r.status, data: d };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const errText = r => (r.data && r.data.message) || ({
    0: 'Нет соединения. Проверьте интернет и попробуйте ещё раз.',
    403: 'Доступ закрыт. Откройте сайт по личной ссылке из Telegram-бота.',
    429: 'Слишком много запросов. Подождите немного и повторите.',
  }[r.status]) || 'Не получилось. Попробуйте ещё раз.';

  // ---------- счётчики ----------
  function badge() {
    const n = COUNTS[tool] || 0;
    [tab, mBtn].forEach(b => {
      const c = b && b.querySelector('.revcnt');
      if (!c) return;
      c.hidden = !n; c.textContent = n > 99 ? '99+' : String(n);
    });
  }
  function setCount(t, n) { COUNTS[t] = n; if (t === tool) badge(); }

  // ---------- шапка: средняя оценка + число отзывов ----------
  function renderSummary() {
    const el = $('revSum');
    if (!st) { el.replaceChildren(); return; }
    const r = (typeof RATINGS !== 'undefined' && RATINGS[tool]) || { avg: 0, count: 0 };
    const total = st.total || 0;
    const rate = r.count ? `${r.count} ${word(r.count, ['оценка', 'оценки', 'оценок'])}` : 'пока нет оценок';
    el.innerHTML = `<div class="rsum"><div class="big">${r.count ? r.avg.toFixed(1).replace('.', ',') : '—'}</div>` +
      `<div class="rs">${starsAvgSvg(r.avg)}<small>${rate} · ${total} ${reviewsWord(total)}</small></div></div>`;   // только числа из кода, текстов пользователей тут нет
  }

  // ---------- карточка отзыва ----------
  function card(r, i) {
    const av = h('span', { class: 'rav', 'aria-hidden': 'true', text: (Array.from(r.name)[0] || '?').toUpperCase() });
    av.style.setProperty('--h', String(r.h | 0));
    const stars = r.mine && typeof RATINGS !== 'undefined' && RATINGS[tool] && RATINGS[tool].mine ? RATINGS[tool].mine : r.stars;
    const main = h('div', { class: 'rmain' },
      h('div', { class: 'rtop' },
        h('span', { class: 'rname', text: r.name }),
        r.mine && r.hidden && h('span', { class: 'rbadge warn', text: 'Скрыт модератором' }),
        stars ? starsRow(stars) : null,
        h('span', { class: 'rdate', text: fmtDate(r.at) + (r.edited ? ' · изменён' : '') })),
      h('p', { class: 'rtext', text: r.text }));
    if (r.mine) main.append(actions());
    const c = h('article', { class: 'rv' + (r.mine ? ' mine' : '') }, av, main);
    c.style.setProperty('--i', String(Math.min(i, 8)));
    return c;
  }

  function actions() {
    if (confirmDel) {
      return h('div', { class: 'ract' },
        h('span', { class: 'q', text: 'Удалить отзыв?' }),
        h('button', { type: 'button', class: 'rbtn sm dng fill', text: 'Да, удалить', disabled: busy, onclick: del }),
        h('button', { type: 'button', class: 'rbtn sm', text: 'Отмена', disabled: busy, onclick: () => { confirmDel = false; renderForm(); } }));
    }
    return h('div', { class: 'ract' },
      h('button', { type: 'button', class: 'rbtn sm', text: 'Изменить', onclick: () => { editing = true; renderForm(true); } }),
      h('button', { type: 'button', class: 'rbtn sm dng', text: 'Удалить', onclick: () => { confirmDel = true; renderForm(); } }));
  }

  // ---------- форма ----------
  function composer(mode) {
    const edit = mode === 'edit', t0 = tool;
    const ta = h('textarea', { rows: 3, maxlength: MAX, 'aria-label': 'Текст отзыва',
      placeholder: 'Работает ли инструмент, что он находит, стоит ли своих денег…' });
    ta.value = edit ? st.mine.text : (drafts[t0] || '');
    const cnt = h('span', { class: 'cnt' });
    const send = h('button', { type: 'button', class: 'rbtn pri', text: edit ? 'Сохранить' : 'Опубликовать' });
    const err = h('div', { class: 'rerr', role: 'alert', hidden: true });
    const sync = () => {
      const n = ta.value.length, ok = ta.value.trim().length >= MIN;
      cnt.textContent = n + ' / ' + MAX;
      cnt.classList.toggle('warn', n > MAX - 80);
      send.disabled = busy || !ok;
      ta.style.height = 'auto';
      ta.style.height = Math.min(ta.scrollHeight, 240) + 'px';
      if (!edit) { if (n) drafts[t0] = ta.value; else delete drafts[t0]; }
    };
    const submit = async () => {
      if (busy || send.disabled) return;
      busy = true; send.disabled = true; send.textContent = 'Отправка…'; err.hidden = true;
      const r = await api('/api/review', { tool: t0, text: ta.value });
      busy = false;
      if (r.ok && r.data.mine) {
        setCount(t0, r.data.total);
        delete drafts[t0];
        if (tool !== t0 || !st || st.tool !== t0) return;
        st.mine = r.data.mine; st.total = r.data.total; editing = false; confirmDel = false;
        render();
        $('revBody').scrollTo({ top: 0, behavior: 'smooth' });
        return;
      }
      if (r.data && r.data.error === 'banned' && st) { st.banned = true; render(); return; }
      send.textContent = edit ? 'Сохранить' : 'Опубликовать';
      err.textContent = errText(r); err.hidden = false;
      sync();
    };
    send.onclick = submit;
    ta.addEventListener('input', () => { err.hidden = true; sync(); });
    ta.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); } });
    const hasStars = typeof RATINGS !== 'undefined' && RATINGS[t0] && RATINGS[t0].mine;
    const foot = h('div', { class: 'rfoot' }, cnt,
      edit && h('button', { type: 'button', class: 'rbtn', text: 'Отмена', onclick: () => { editing = false; renderForm(); } }),
      send);
    const wrap = h('div', { class: 'rform' }, ta, foot);
    const hint = h('p', { class: 'rhint', text: edit
      ? 'Изменения заменят прежний текст отзыва.'
      : 'Один отзыв на инструмент — позже его можно изменить или удалить.' + (hasStars ? '' : ' Оценку звёздами можно поставить в карточке: она появится рядом с отзывом.') });
    requestAnimationFrame(sync);
    return { nodes: [wrap, err, hint], ta };
  }

  function renderForm(focus) {
    const f = $('revForm');
    if (!st || (st.loading && !st.mine)) { f.replaceChildren(); return; }
    if (st.mine && !editing) { f.replaceChildren(h('div', { class: 'rlabel' }, h('span', { text: 'Ваш отзыв' })), card(st.mine, 0)); return; }
    if (st.banned && !st.mine) {
      f.replaceChildren(h('div', { class: 'rnote bad', text: 'Администратор закрыл вам возможность оставлять отзывы.' }));
      return;
    }
    const c = composer(editing ? 'edit' : 'new');
    f.replaceChildren(h('div', { class: 'rlabel' }, h('span', { text: editing ? 'Изменить отзыв' : 'Ваш отзыв' })), ...c.nodes);
    if (focus) requestAnimationFrame(() => { c.ta.focus(); c.ta.setSelectionRange(c.ta.value.length, c.ta.value.length); });
  }

  // ---------- список ----------
  function renderList() {
    const l = $('revList'), m = $('revMore');
    m.replaceChildren();
    if (!st) { l.replaceChildren(); return; }
    if (st.loading) {
      l.replaceChildren(...[0, 1, 2].map(() => h('div', { class: 'rsk' }, h('i'), h('div', {}, h('b'), h('b'), h('b')))));
      return;
    }
    if (st.error) {
      l.replaceChildren(h('div', { class: 'rempty' }, h('b', { text: 'Не удалось загрузить' }), h('span', { text: st.error }),
        h('button', { type: 'button', class: 'rbtn', text: 'Повторить', onclick: () => load() })));
      return;
    }
    if (!st.items.length) {
      if (st.mine) { l.replaceChildren(); return; }
      const empty = h('div', { class: 'rempty' },
        h('b', { text: 'Пока нет отзывов' }),
        h('span', { text: 'Станьте первым: расскажите, работает ли инструмент и что в нём хорошего.' }));
      empty.insertAdjacentHTML('afterbegin', ICON_CHAT);        // статичная иконка из кода
      l.replaceChildren(empty);
      return;
    }
    const others = st.total - (st.mine && !st.mine.hidden ? 1 : 0);
    l.replaceChildren(
      h('div', { class: 'rlabel' }, h('span', { text: st.mine ? 'Другие отзывы' : 'Отзывы' }), h('b', { text: String(others) })),
      ...st.items.map((r, i) => card(r, i)));
    if (st.more) m.append(h('div', { class: 'rmore' },
      h('button', { type: 'button', class: 'rbtn', text: st.moreErr || 'Показать ещё', disabled: st.moreBusy, onclick: () => load(true) })));
  }

  function render() { renderSummary(); renderForm(); renderList(); }

  // ---------- загрузка ----------
  async function load(more) {
    const my = ++seq, t = tool;
    if (!more) {
      st = { tool: t, at: Date.now(), loading: true, error: '', items: [], more: false, mine: null, total: COUNTS[t] || 0, banned: false };
      editing = false; confirmDel = false;
      render();
    } else { st.moreBusy = true; st.moreErr = ''; renderList(); }
    const r = await api('/api/reviews?tool=' + encodeURIComponent(t) + '&offset=' + (more ? st.items.length : 0));
    if (my !== seq || !st || st.tool !== t) return;
    if (!r.ok) {
      if (more) { st.moreBusy = false; st.moreErr = 'Не удалось — повторить'; renderList(); }
      else { st.loading = false; st.error = errText(r); render(); }
      return;
    }
    const d = r.data;
    st.loading = false; st.moreBusy = false; st.error = '';
    st.items = more ? st.items.concat(d.items) : d.items;
    st.more = !!d.more; st.mine = d.mine; st.total = d.total; st.banned = !!d.banned;
    setCount(t, d.total);
    render();
  }

  async function del() {
    if (busy) return;
    const t = tool; busy = true; renderForm();
    const r = await api('/api/review/delete', { tool: t });
    busy = false;
    if (r.ok) {
      setCount(t, r.data.total);
      if (st && st.tool === t) { st.mine = null; st.total = r.data.total; editing = false; confirmDel = false; render(); }
    } else if (st && st.tool === t) {
      confirmDel = false; renderForm();
      $('revForm').append(h('div', { class: 'rerr', role: 'alert', text: errText(r) }));
    }
  }

  // ---------- открыть / закрыть ----------
  function setOpen(on, silent) {
    if (on === isOpen) return;
    isOpen = on;
    box.classList.toggle('open', on);
    box.setAttribute('aria-hidden', String(!on));
    tab.setAttribute('aria-expanded', String(on));
    document.body.classList.toggle('rev-open', on);
    if (on) { $('revBody').scrollTop = 0; if (!st || st.tool !== tool || Date.now() - st.at > 60000) load(); else render(); }
    // на широких экранах дерево подвигается, чтобы выбранный инструмент не оказался под панелью
    if (!silent && innerWidth > WIDE) { try { if (root) update(root); } catch (e) {} }
  }

  tab.addEventListener('click', () => { if (tool) setOpen(!isOpen); });
  mBtn.addEventListener('click', () => { if (tool) setOpen(true); });
  $('revClose').addEventListener('click', () => setOpen(false));

  // ---------- вызывается из app.js ----------
  window.HReviews = {
    isOpen: () => isOpen,
    close: silent => setOpen(false, silent),
    onTool(t) {
      if (t === tool) return;
      tool = t;
      $('revTitle').textContent = shortName(t);
      badge();
      if (isOpen) load(); else st = null;
    },
    setCounts(c) { COUNTS = c && typeof c === 'object' ? c : {}; badge(); },
    sync() { if (!isOpen) return; renderSummary(); if (st && st.mine && !editing) renderForm(); },
  };
})();
