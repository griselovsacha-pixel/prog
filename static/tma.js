'use strict';
/* Harledy Project — Telegram Mini App.
 * Вход: заголовок X-Access-Key: "tma <initData>". Сервер проверяет подпись, соглашение и подписки (/api/tma/state),
 * после чего весь остальной API (рейтинги, отзывы, ИИ, статусы) работает так же, как на сайте.
 * Весь пользовательский текст попадает в DOM только через textContent (функция h), innerHTML — только для статичных иконок. */
(() => {
  const TG = window.Telegram && Telegram.WebApp;
  const $ = id => document.getElementById(id);
  const SEP = ' › ';
  const view = $('view'), tabs = $('tabs'), q = $('q'), qx = $('qx'), ttl = $('ttl');

  // ---------- вспомогательное ----------
  function h(tag, a, ...kids) {
    const el = document.createElement(tag);
    for (const k in (a || {})) {
      const v = a[k];
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of kids.flat()) if (c != null && c !== false) el.append(c.nodeType ? c : document.createTextNode(String(c)));
    return el;
  }
  const add = (el, ...kids) => { kids.flat().forEach(c => { if (c != null && c !== false) el.append(c); }); return el; };
  const ICON = {
    chev: '<svg class="chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>',
    folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
    send: '<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
    plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
    heart: '<svg viewBox="0 0 24 24"><path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.5A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/></svg>',
    chan: '<svg viewBox="0 0 24 24"><path d="M21 4 3 11l6 2 2 6 3-4 5 3z"/></svg>',
    doc: '<svg viewBox="0 0 24 24"><path d="M7 3h7l5 5v13H7zM14 3v5h5"/></svg>',
    user: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4-6 8-6s7 2 8 6"/></svg>',
  };
  const ico = (name, cls) => { const s = h('span', { class: cls || '' }); s.innerHTML = ICON[name]; return s; };
  const plural = (n, f) => { const m = n % 100, d = n % 10; return m > 10 && m < 20 ? f[2] : d === 1 ? f[0] : d > 1 && d < 5 ? f[1] : f[2]; };
  const safeUrl = u => { try { const x = new URL(u, location.href); return /^https?:$/.test(x.protocol) ? x.href : ''; } catch (e) { return ''; } };
  const isTg = u => /^https:\/\/(t|telegram)\.me\//i.test(u);
  function openUrl(u) {
    u = safeUrl(u);
    if (!u) return;
    if (isTg(u) && TG && TG.openTelegramLink) TG.openTelegramLink(u);
    else if (TG && TG.openLink) TG.openLink(u);
    else window.open(u, '_blank', 'noopener');
  }
  const tap = (t) => { try { TG.HapticFeedback.impactOccurred(t || 'light'); } catch (e) {} };
  const notify = (t) => { try { TG.HapticFeedback.notificationOccurred(t); } catch (e) {} };
  let toastT;
  function toast(msg) {
    const t = $('toast'); t.textContent = msg; t.classList.add('on');
    clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('on'), 2600);
  }

  // ---------- состояние ----------
  const S = {
    st: null, tree: null, tools: [], bySlug: {}, ratings: {}, status: {}, counts: {},
    path: [],            // стек открытых категорий
    q: '', tab: 'cat', tool: null, bnClosed: false,
  };
  const SID = (window.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random().toString(16).slice(2)).replace(/[^\w-]/g, '');

  // ---------- сеть ----------
  async function api(path, body, retry = true) {
    const opt = { headers: { 'X-Access-Key': 'tma ' + (TG ? TG.initData : '') } };
    if (body) { opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path, opt); } catch (e) { return { ok: false, status: 0, data: {} }; }
    let data = {}; try { data = await r.json(); } catch (e) {}
    // сервер перезапускался и забыл сессию: один раз заново проверяем доступ и повторяем запрос
    if (r.status === 403 && retry && !path.startsWith('/api/tma/')) {
      const st = await api('/api/tma/state', null, false);
      if (st.ok && st.data && st.data.ok) return api(path, body, false);
    }
    return { ok: r.ok, status: r.status, data };
  }
  const track = (ev, tool) => { if (S.st && S.st.ok) api('/api/track', { sid: SID, ev, tool: tool || null, src: 'tma' }, false); };

  // ---------- каталог ----------
  function flatten(node, path) {
    for (const c of node.children || []) {
      if (c.children) { c._tools = 0; flatten(c, path.concat(c.name)); node._tools = (node._tools || 0) + c._tools; }
      else { c.path = path; c.hay = [c.name, ...(c.aka || []), ...(c.tags || []), c.desc || '', path.join(' ')].join(' ').toLowerCase();
        S.tools.push(c); S.bySlug[c.slug] = c; node._tools = (node._tools || 0) + 1; }
    }
  }
  function cur() { let n = S.tree; for (const i of S.path) n = n.children[i]; return n; }

  function search(text) {
    const words = text.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return [];
    const out = [];
    for (const t of S.tools) {
      if (!words.every(w => t.hay.includes(w))) continue;
      const nm = t.name.toLowerCase();
      let sc = 1;
      for (const w of words) sc += nm.startsWith(w) ? 3 : nm.includes(w) ? 2 : (t.tags || []).some(x => String(x).toLowerCase().includes(w)) ? 1 : 0;
      out.push([sc, t]);
    }
    return out.sort((a, b) => b[0] - a[0] || a[1].name.localeCompare(b[1].name)).slice(0, 60).map(x => x[1]);
  }

  const STATE_RU = { ok: 'работает', no_reply: 'не отвечает', deleted: 'удалён', site_error: 'ошибка сайта' };
  const stOf = t => S.status[t.key];
  const dotCls = t => { const s = stOf(t); return 'dot' + (s ? (s.bad ? ' bad' : s.s === 'ok' ? ' ok' : '') : ''); };
  function stars(avg) { return '★ ' + avg.toFixed(1).replace('.', ','); }

  function toolRow(t, showPath) {
    const r = S.ratings[t.key];
    return h('button', { class: 'row', type: 'button', onclick: () => openTool(t) },
      h('div', { class: 'm' },
        h('div', { class: 'n' }, h('i', { class: dotCls(t) }), h('span', { text: t.name }), r && r.count ? h('span', { class: 'rt', text: stars(r.avg) }) : null),
        t.desc ? h('div', { class: 'd', text: t.desc }) : null,
        showPath ? h('div', { class: 'path', text: t.path.join(SEP) }) : null),
      ico('chev'));
  }
  function catRow(c, i) {
    return h('button', { class: 'row', type: 'button', onclick: () => { tap(); S.path.push(i); render(); view.scrollTop = 0; } },
      ico('folder', 'ico'),
      h('div', { class: 'm' }, h('div', { class: 'n' }, h('span', { text: c.name }))),
      h('span', { class: 'c', text: c._tools }), ico('chev'));
  }

  function renderCatalog() {
    view.className = ''; view.replaceChildren();
    const text = S.q.trim();
    if (text) {
      const res = search(text);
      ttl.textContent = 'Поиск';
      if (!res.length) { view.append(h('p', { class: 'empty', text: 'Ничего не нашлось. Попробуйте другое слово или спросите ИИ.' })); return; }
      view.append(h('div', { class: 'h2', text: `Найдено: ${res.length}` }), h('div', { class: 'sec' }, res.map(t => toolRow(t, true))));
      return;
    }
    const node = cur();
    ttl.textContent = S.path.length ? node.name : 'Harledy Project';
    if (!S.path.length && S.banner && !S.bnClosed) view.append(bannerCard(S.banner));
    const cats = [], tl = [];
    (node.children || []).forEach((c, i) => (c.children ? cats.push(catRow(c, i)) : tl.push(toolRow(c, false))));
    if (cats.length) view.append(h('div', { class: 'sec' }, cats));
    if (tl.length) add(view, S.path.length && cats.length ? h('div', { class: 'h2', text: 'Инструменты' }) : null, h('div', { class: 'sec' }, tl));
    if (!S.path.length) view.append(h('p', { class: 'empty', text: `Инструментов в каталоге: ${S.count}` }));
  }

  function bannerCard(b) {
    const url = safeUrl(b.url);
    return h('div', { class: 'bn' },
      h('button', { class: 'x', type: 'button', 'aria-label': 'Закрыть', text: '×', onclick: () => {
        S.bnClosed = true; try { localStorage.setItem('bn_closed', String(b.v)); } catch (e) {} render();
      } }),
      h('b', { text: b.title }), b.text ? h('p', { text: b.text }) : null,
      url ? h('button', { class: 'btn sm', type: 'button', text: b.button || 'Открыть', onclick: () => openUrl(url) }) : null);
  }

  // ---------- ИИ ----------
  const AI = { hist: [], busy: false, items: [] };
  const CHIPS = ['Какие инструменты есть для поиска по нику?', 'Как проверить, утекла ли моя почта?', 'Чем искать человека по фото?', 'Кто создатель проекта?'];

  function inline(el, text) {
    text.split(/(\*\*[^*]+\*\*)/g).forEach(p => {
      if (/^\*\*[^*]+\*\*$/.test(p)) el.append(h('b', { text: p.slice(2, -2) }));
      else if (p) el.append(p.replace(/`/g, ''));
    });
  }
  function renderAnswer(el, text) {
    let list = null, para = null;
    for (const raw of text.replace(/\r/g, '').split('\n')) {
      const src = raw.trimEnd();
      const m = src.match(/^\s*(?:([-*•])|(\d+)[.)])\s+(.*)$/);
      if (m) {
        const tag = m[1] ? 'ul' : 'ol';
        if (!list || list.tagName.toLowerCase() !== tag) { list = h(tag); el.append(list); }
        const li = h('li'); inline(li, m[3]); list.append(li); para = null;
      } else if (!src.trim()) { list = null; para = null; }
      else {
        list = null;
        if (!para) { para = h('p'); el.append(para); } else para.append(h('br'));
        inline(para, src.replace(/^#{1,6}\s+/, ''));
      }
    }
  }
  const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  function mentioned(text) {
    const low = text.toLowerCase(), out = [];
    for (const t of S.tools) {
      if (t.name.length < 4) continue;
      const m = new RegExp('(^|[^\\p{L}\\p{N}_])' + esc(t.name.toLowerCase()) + '($|[^\\p{L}\\p{N}_])', 'u');
      if (m.test(low)) out.push(t);
      if (out.length >= 4) break;
    }
    return out;
  }

  function renderAI() {
    view.className = 'ai'; view.replaceChildren(); ttl.textContent = 'Harledy AI';
    const box = h('div', { class: 'msgs', id: 'msgs' });
    if (!AI.items.length) {
      box.append(h('div', { class: 'msg a' }, h('p', { text: 'Спросите, какой инструмент подойдёт под вашу задачу. Я отвечаю по каталогу Harledy Project.' })),
        h('div', { class: 'chips' }, CHIPS.map(c => h('button', { class: 'chip', type: 'button', text: c, onclick: () => ask(c) }))));
    }
    AI.items.forEach(m => box.append(msgNode(m)));
    view.append(box);
    const input = h('input', { id: 'aiq', type: 'text', maxlength: '1000', placeholder: 'Ваш вопрос…', enterkeyhint: 'send', autocomplete: 'off' });
    const btn = h('button', { type: 'submit', 'aria-label': 'Отправить', disabled: true }); btn.innerHTML = ICON.send;
    input.addEventListener('input', () => { btn.disabled = AI.busy || !input.value.trim(); });
    const form = h('form', { id: 'ask', onsubmit: e => { e.preventDefault(); const v = input.value; input.value = ''; btn.disabled = true; ask(v); } }, input, btn);
    document.body.append(form);
    view.scrollTop = view.scrollHeight;
  }
  function msgNode(m) {
    if (m.role === 'u') return h('div', { class: 'msg u', text: m.text });
    if (m.typing) return h('div', { class: 'msg a' }, h('span', { class: 'typing' }, h('i'), h('i'), h('i')));
    const el = h('div', { class: 'msg a' + (m.err ? ' err' : '') });
    if (m.err) el.textContent = m.text; else renderAnswer(el, m.text);
    if (m.tools && m.tools.length) el.append(h('div', { class: 'chips' }, m.tools.map(t => h('button', { class: 'chip', type: 'button', text: t.name, onclick: () => openTool(t) }))));
    (m.btns || []).forEach(b => el.append(h('div', { class: 'chips' }, h('button', { class: 'btn sm ghost', type: 'button', text: b.label, onclick: () => openUrl(b.url) }))));
    return el;
  }
  async function ask(text) {
    text = text.trim();
    if (!text || AI.busy) return;
    AI.busy = true;
    AI.items.push({ role: 'u', text }, { role: 'a', typing: true });
    renderAI(); tap();
    const r = await api('/api/ai', { q: text, history: AI.hist.slice(-8), src: 'tma' });
    AI.items.pop();
    if (r.ok && r.data.answer) {
      let ans = String(r.data.answer), btns = [];
      const links = r.data.links || {};
      for (const [k, label] of [['dev', 'Написать разработчику'], ['channel', 'Канал проекта']]) {
        const u = links[k];
        if (u && ans.includes(u)) { ans = ans.split(u).join(''); btns.push({ label, url: u }); }
      }
      ans = ans.replace(/\[([^\]]*)\]\(\s*\)/g, '$1').replace(/\(\s*\)/g, '').trim();
      AI.hist.push({ role: 'user', text }, { role: 'assistant', text: ans });
      AI.items.push({ role: 'a', text: ans, tools: mentioned(ans), btns });
    } else {
      const msg = { 429: 'Слишком много вопросов. Подождите минуту.', 503: 'ИИ сейчас занят. Попробуйте через несколько секунд.', 0: 'Нет соединения.' }[r.status];
      AI.items.push({ role: 'a', err: true, text: msg || 'Не удалось получить ответ. Попробуйте ещё раз.' });
      notify('error');
    }
    AI.busy = false;
    if (S.tab === 'ai') renderAI();
  }

  // ---------- «Ещё» ----------
  function moreRow(icon, title, sub, fn) {
    return h('button', { class: 'row', type: 'button', onclick: () => { tap(); fn(); } },
      ico(icon, 'ico'), h('div', { class: 'm' }, h('div', { class: 'n' }, h('span', { text: title })), sub ? h('div', { class: 'd', text: sub }) : null), ico('chev'));
  }
  function renderMore() {
    view.className = ''; view.replaceChildren(); ttl.textContent = 'Ещё';
    const st = S.st, bot = st.bot ? `https://t.me/${st.bot}` : '';
    view.append(
      h('div', { class: 'h2', text: st.name || 'Профиль' }),
      h('div', { class: 'sec' },
        bot ? moreRow('plus', 'Добавить свой инструмент', 'Откроется чат с ботом — нажмите кнопку в меню', () => openUrl(bot + '?start=menu')) : null,
        bot ? moreRow('heart', 'Поддержать проект', 'Откроется чат с ботом', () => openUrl(bot + '?start=menu')) : null),
      h('div', { class: 'h2', text: 'Проект' }),
      h('div', { class: 'sec' },
        moreRow('chan', 'Канал Harledy Project', '', () => openUrl(st.channel)),
        moreRow('user', 'Контакт разработчика', '', () => openUrl(st.dev)),
        st.site ? moreRow('doc', 'Пользовательское соглашение', '', () => openUrl(st.site + '/terms')) : null));
  }

  // ---------- шторки ----------
  const scrim = $('scrim'), sheet = $('sheet'), rsheet = $('rsheet');
  let mainCb = null;
  function setMain(text, fn) {
    if (!TG || !TG.MainButton) return;
    if (mainCb) TG.MainButton.offClick(mainCb);
    mainCb = fn; TG.MainButton.setText(text); TG.MainButton.onClick(mainCb); TG.MainButton.show();
  }
  function hideMain() { if (TG && TG.MainButton) { if (mainCb) TG.MainButton.offClick(mainCb); mainCb = null; TG.MainButton.hide(); } }
  function openSheet(el) { el.classList.add('open'); el.setAttribute('aria-hidden', 'false'); scrim.classList.add('on'); updateBack(); }
  function closeSheet(el) {
    el.classList.remove('open'); el.setAttribute('aria-hidden', 'true');
    if (!sheet.classList.contains('open') && !rsheet.classList.contains('open')) scrim.classList.remove('on');
    if (el === sheet) { hideMain(); S.tool = null; }
    updateBack();
  }
  // тянем шторку вниз за «ручку»
  [sheet, rsheet].forEach(el => {
    const g = el.querySelector('.grab'); let y0 = null, dy = 0;
    g.addEventListener('touchstart', e => { y0 = e.touches[0].clientY; dy = 0; el.style.transition = 'none'; }, { passive: true });
    g.addEventListener('touchmove', e => { if (y0 == null) return; dy = Math.max(0, e.touches[0].clientY - y0); el.style.transform = `translateY(${dy}px)`; }, { passive: true });
    g.addEventListener('touchend', () => { if (y0 == null) return; y0 = null; el.style.transition = ''; el.style.transform = ''; if (dy > 90) closeSheet(el); });
  });
  scrim.addEventListener('click', () => { if (rsheet.classList.contains('open')) closeSheet(rsheet); else closeSheet(sheet); });

  const PRICE = { free: ['Бесплатно', 'g'], freemium: ['Есть бесплатные запросы', 'g'], paid: ['Платно', ''] };
  const KIND = { bot: 'Бот', site: 'Сайт', 'bot+site': 'Бот и сайт', 'bot+miniapp': 'Бот с мини-аппом' };

  function shareUrl(t) { return S.st.share ? `${S.st.share}?startapp=tool_${t.slug}` : ''; }

  function openTool(t) {
    S.tool = t; track('select', t.key);
    const body = $('sbody'); body.className = 'sbody mb'; body.replaceChildren();
    const s = stOf(t), bad = !!(s && s.bad), main = safeUrl(t.bot), eternal = safeUrl(t.eternal);
    const badges = h('div', { class: 'badges' });
    if (KIND[t.type]) badges.append(h('span', { class: 'bdg', text: KIND[t.type] }));
    const pr = PRICE[t.price]; if (pr) badges.append(h('span', { class: 'bdg ' + pr[1], text: t.price_text && t.price !== 'free' ? t.price_text : pr[0] }));
    if (t.vpn === 'yes') badges.append(h('span', { class: 'bdg b', text: 'Нужен VPN' })); else if (t.vpn === 'no') badges.append(h('span', { class: 'bdg g', text: 'Без VPN' }));
    add(body, h('div', { class: 'path', text: t.path.join(SEP) }), h('h2', { text: t.name }), badges,
      t.desc ? h('p', { class: 'desc', text: t.desc }) : null,
      s ? h('div', { class: 'stat' }, h('i', { class: dotCls(t) }), 'Статус: ' + (STATE_RU[s.s] || s.s || '—') + (s.m && !STATE_RU[s.s] ? ' — ' + s.m : '')) : null,
      (t.tags || []).length ? h('div', { class: 'tags' }, t.tags.map(x => h('span', { class: 'tag', text: '#' + x }))) : null,
      h('div', { id: 'rating' }));
    const acts = h('div', { class: 'acts' },
      h('button', { class: 'btn ghost', type: 'button', onclick: () => openReviews(t) }, 'Отзывы', S.counts[t.key] ? ` · ${S.counts[t.key]}` : ''),
      eternal && !bad ? h('button', { class: 'btn ghost', type: 'button', text: 'Вечная ссылка', onclick: () => { track('eternal', t.key); openUrl(eternal); } }) : null,
      main && bad && eternal ? h('button', { class: 'btn ghost', type: 'button', text: 'Ссылка на бота', onclick: () => { track('bot', t.key); openUrl(main); } }) : null,
      shareUrl(t) ? h('button', { class: 'btn ghost', type: 'button', text: 'Поделиться', onclick: () => share(t) }) : null);
    body.append(acts);
    renderRating(t);
    body.scrollTop = 0;
    const primary = bad && eternal ? eternal : main;
    if (primary) setMain(bad && eternal ? 'Открыть вечную ссылку' : (isTg(main) ? 'Перейти в бота' : 'Открыть сайт'), () => { track(bad && eternal ? 'eternal' : 'bot', t.key); openUrl(primary); });
    openSheet(sheet); tap();
  }
  function share(t) {
    const u = shareUrl(t); if (!u) return;
    openUrl('https://t.me/share/url?url=' + encodeURIComponent(u) + '&text=' + encodeURIComponent(t.name + ' — в каталоге Harledy Project'));
  }
  function renderRating(t) {
    const box = $('rating'); if (!box) return;
    const r = S.ratings[t.key] || { avg: 0, count: 0, mine: 0 };
    box.className = 'rate'; box.replaceChildren(
      h('b', { text: r.count ? r.avg.toFixed(1).replace('.', ',') + ' / 5' : '—' }),
      h('span', { class: 'path', text: r.count ? `${r.count} ${plural(r.count, ['оценка', 'оценки', 'оценок'])}` : 'пока нет оценок' }),
      h('span', { class: 'stars', role: 'radiogroup', 'aria-label': 'Ваша оценка' },
        [1, 2, 3, 4, 5].map(n => h('button', { type: 'button', class: n <= (r.mine || 0) ? 'on' : '', 'aria-label': n + ' из 5', text: '★', onclick: () => rate(t, n) }))));
  }
  async function rate(t, n) {
    tap('medium');
    const old = S.ratings[t.key]; S.ratings[t.key] = { ...(old || { avg: 0, count: 0 }), mine: n }; renderRating(t);
    const r = await api('/api/rate', { tool: t.key, stars: n });
    if (r.ok) { S.ratings[t.key] = { avg: r.data.avg, count: r.data.count, mine: r.data.mine }; notify('success'); }
    else { if (old) S.ratings[t.key] = old; else delete S.ratings[t.key]; toast(r.status === 429 ? 'Слишком часто, подождите немного' : 'Не удалось сохранить оценку'); }
    renderRating(t); if (!S.tool || S.tool !== t) return; render(true);
  }

  // ---------- отзывы ----------
  const RV = { t: null, items: [], mine: null, more: false, total: 0, banned: false, off: 0, loading: false, edit: false };
  const fmtDate = iso => { try { return new Date(iso).toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' }).replace(' г.', ''); } catch (e) { return ''; } };

  async function openReviews(t) {
    RV.t = t; RV.items = []; RV.mine = null; RV.off = 0; RV.edit = false;
    const body = $('rbody'); body.className = 'sbody'; body.replaceChildren(h('p', { class: 'load', text: 'Загрузка…' }));
    openSheet(rsheet);
    const r = await api('/api/reviews?tool=' + encodeURIComponent(t.key) + '&offset=0');
    if (!r.ok) { body.replaceChildren(h('p', { class: 'empty', text: 'Не удалось загрузить отзывы. Попробуйте позже.' })); return; }
    Object.assign(RV, { items: r.data.items, mine: r.data.mine, more: r.data.more, total: r.data.total, banned: r.data.banned, off: r.data.items.length });
    S.counts[t.key] = RV.total; drawReviews();
  }
  function reviewNode(r) {
    return h('article', { class: 'rv' + (r.mine ? ' mine' : '') },
      h('div', { class: 't' }, h('span', { class: 'nm', text: r.name }),
        r.stars ? h('span', { class: 'rt', text: '★'.repeat(r.stars) }) : null,
        h('span', { class: 'dt', text: fmtDate(r.at) + (r.edited ? ' · изменён' : '') }),
        r.mine && r.hidden ? h('span', { class: 'warn', text: 'скрыт модератором' }) : null),
      h('p', { text: r.text }));
  }
  function drawReviews() {
    const body = $('rbody'), t = RV.t; body.replaceChildren();
    body.append(h('h2', { text: 'Отзывы' }), h('div', { class: 'path', text: t.name + ` · ${RV.total} ${plural(RV.total, ['отзыв', 'отзыва', 'отзывов'])}` }));
    if (RV.banned) body.append(h('p', { class: 'warn', text: 'Вам закрыта возможность оставлять отзывы.' }));
    else if (RV.mine && !RV.edit) {
      body.append(h('div', { class: 'h2', text: 'Ваш отзыв' }), reviewNode(RV.mine),
        h('div', { class: 'acts' },
          h('button', { class: 'btn sm ghost', type: 'button', text: 'Изменить', onclick: () => { RV.edit = true; drawReviews(); } }),
          h('button', { class: 'btn sm dng', type: 'button', text: 'Удалить', onclick: delReview })));
    } else {
      const ta = h('textarea', { maxlength: '1000', placeholder: 'Что скажете об инструменте? Без ссылок.', 'aria-label': 'Текст отзыва' }, '');
      ta.value = RV.mine ? RV.mine.text : '';
      const cnt = h('div', { class: 'cnt', text: `${ta.value.length} / 1000` });
      const send = h('button', { class: 'btn', type: 'button', text: RV.mine ? 'Сохранить' : 'Опубликовать', onclick: () => saveReview(ta.value, send) });
      ta.addEventListener('input', () => { cnt.textContent = `${ta.value.length} / 1000`; });
      add(body, h('div', { class: 'h2', text: RV.mine ? 'Изменить отзыв' : 'Ваш отзыв' }), ta, cnt, send,
        RV.edit ? h('button', { class: 'btn ghost', type: 'button', style: 'margin-left:8px', text: 'Отмена', onclick: () => { RV.edit = false; drawReviews(); } }) : null);
    }
    body.append(h('div', { class: 'h2', text: 'Все отзывы' }));
    if (!RV.items.length) body.append(h('p', { class: 'empty', text: 'Пока нет отзывов. Будьте первым!' }));
    RV.items.forEach(r => body.append(reviewNode(r)));
    if (RV.more) body.append(h('button', { class: 'btn ghost', type: 'button', style: 'width:100%;margin-top:8px', text: 'Показать ещё', onclick: moreReviews }));
  }
  async function moreReviews() {
    if (RV.loading) return; RV.loading = true;
    const r = await api('/api/reviews?tool=' + encodeURIComponent(RV.t.key) + '&offset=' + RV.off);
    RV.loading = false;
    if (!r.ok) return toast('Не удалось загрузить');
    RV.items = RV.items.concat(r.data.items); RV.more = r.data.more; RV.off += r.data.items.length; drawReviews();
  }
  async function saveReview(text, btn) {
    text = text.trim();
    if (text.length < 3) return toast('Напишите хотя бы несколько слов');
    btn.disabled = true;
    const r = await api('/api/review', { tool: RV.t.key, text });
    btn.disabled = false;
    if (!r.ok) { notify('error'); return toast((r.data && r.data.message) || 'Не получилось. Попробуйте ещё раз.'); }
    RV.mine = r.data.mine; RV.total = r.data.total; RV.edit = false; S.counts[RV.t.key] = RV.total; notify('success'); drawReviews();
  }
  async function delReview() {
    const ok = TG && TG.showConfirm ? await new Promise(res => TG.showConfirm('Удалить отзыв?', res)) : confirm('Удалить отзыв?');
    if (!ok) return;
    const r = await api('/api/review/delete', { tool: RV.t.key });
    if (!r.ok) return toast('Не удалось удалить');
    RV.mine = null; RV.total = r.data.total; S.counts[RV.t.key] = RV.total; drawReviews();
  }

  // ---------- вход: соглашение и подписка ----------
  function renderGate() {
    const st = S.st; tabs.hidden = true; document.querySelector('#top').hidden = true; hideMain();
    view.className = ''; view.replaceChildren(); ttl.textContent = 'Harledy Project';
    const box = h('div', { class: 'gate' });
    if (!st.terms) {
      add(box, h('h2', { text: 'Добро пожаловать' }),
        h('p', { text: 'Перед началом ознакомьтесь с пользовательским соглашением. Нажав кнопку ниже, вы подтверждаете, что полностью с ним согласны.' }),
        st.site ? h('button', { class: 'btn ghost', type: 'button', text: 'Прочитать соглашение', onclick: () => openUrl(st.site + '/terms') }) : null,
        h('button', { class: 'btn', type: 'button', text: 'Я ознакомился и согласен', onclick: async e => {
          e.target.disabled = true;
          const r = await api('/api/tma/terms', {}, false);
          if (r.ok) { S.st = r.data; await afterState(); } else { e.target.disabled = false; toast('Не получилось, попробуйте ещё раз'); }
        } }));
    } else {
      box.append(h('h2', { text: 'Нужна подписка' }),
        h('p', { text: 'Чтобы открыть каталог, подпишитесь на канал и группу проекта.' }),
        h('div', { class: 'sec' }, st.subs.map(s => h('div', { class: 'row' },
          h('div', { class: 'm' }, h('div', { class: 'n' }, h('span', { text: s.title }))),
          s.ok ? h('span', { class: 'c', text: '✓ готово' }) : h('button', { class: 'btn sm', type: 'button', text: 'Подписаться', onclick: () => openUrl(s.link) })))),
        h('button', { class: 'btn', type: 'button', text: 'Я подписался — проверить', onclick: async e => {
          e.target.disabled = true;
          const r = await api('/api/tma/state', null, false);
          e.target.disabled = false;
          if (!r.ok) return toast('Не удалось проверить, попробуйте ещё раз');
          S.st = r.data;
          if (r.data.ok) await afterState(); else { toast('Подписка пока не найдена'); notify('warning'); renderGate(); }
        } }));
    }
    view.append(box);
  }
  function renderOutside() {
    document.querySelector('#top').hidden = true;
    view.replaceChildren(h('div', { class: 'gate' }, h('h2', { text: 'Откройте через Telegram' }),
      h('p', { text: 'Этот каталог работает как мини-приложение внутри Telegram. Откройте его из бота Harledy Project.' })));
  }

  // ---------- каркас ----------
  function render(keep) {
    document.querySelectorAll('#ask').forEach(n => n.remove());
    const sc = keep ? view.scrollTop : 0;
    if (S.tab === 'cat') renderCatalog(); else if (S.tab === 'ai') renderAI(); else renderMore();
    if (keep) view.scrollTop = sc;
    document.querySelector('#sbox').style.display = S.tab === 'cat' ? '' : 'none';
    tabs.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.tab === S.tab));
    updateBack();
  }
  function updateBack() {
    if (!TG || !TG.BackButton) return;
    const need = sheet.classList.contains('open') || rsheet.classList.contains('open') || S.tab !== 'cat' || !!S.q || S.path.length > 0;
    need ? TG.BackButton.show() : TG.BackButton.hide();
  }
  function goBack() {
    if (rsheet.classList.contains('open')) return closeSheet(rsheet);
    if (sheet.classList.contains('open')) return closeSheet(sheet);
    if (S.q) { q.value = ''; S.q = ''; qx.hidden = true; return render(); }
    if (S.tab !== 'cat') { S.tab = 'cat'; return render(); }
    if (S.path.length) { S.path.pop(); render(); view.scrollTop = 0; }
  }
  tabs.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b || b.dataset.tab === S.tab) return;
    if (sheet.classList.contains('open')) closeSheet(sheet);
    if (rsheet.classList.contains('open')) closeSheet(rsheet);
    try { TG.HapticFeedback.selectionChanged(); } catch (e2) {}
    S.tab = b.dataset.tab; render();
  });
  let qt;
  q.addEventListener('input', () => {
    qx.hidden = !q.value;
    clearTimeout(qt); qt = setTimeout(() => { S.q = q.value; if (S.tab === 'cat') render(); }, 120);
  });
  q.addEventListener('keydown', e => { if (e.key === 'Enter') q.blur(); });
  qx.addEventListener('click', () => { q.value = ''; S.q = ''; qx.hidden = true; render(); q.focus(); });

  function openFromParam(p) {
    const m = /^tool_([a-z0-9-]{1,80})$/.exec(p || '');
    const t = m && S.bySlug[m[1]];
    if (!t) return;
    // раскрываем путь до категории инструмента, чтобы «назад» вёл по каталогу
    let n = S.tree; S.path = [];
    for (const name of t.path) { const i = n.children.findIndex(c => c.children && c.name === name); if (i < 0) break; S.path.push(i); n = n.children[i]; }
    render(); openTool(t);
  }

  async function loadCatalog() {
    const [tools, ratings, status, counts, banner] = await Promise.all([
      api('/api/tma/tools'), api('/api/ratings'), api('/api/status'), api('/api/reviews/counts'), api('/api/banner')]);
    if (!tools.ok) {
      view.replaceChildren(h('p', { class: 'empty' }, tools.status === 429 ? 'Слишком много запросов. Подождите немного.' : 'Не удалось загрузить каталог.', h('br'),
        h('button', { class: 'btn', type: 'button', style: 'margin-top:12px', text: 'Повторить', onclick: () => loadCatalog() })));
      return false;
    }
    S.tree = tools.data.tree; S.count = tools.data.count; S.tools = []; S.bySlug = {};
    flatten(S.tree, []);
    if (ratings.ok) S.ratings = ratings.data;
    if (status.ok) S.status = status.data;
    if (counts.ok) S.counts = counts.data;
    S.banner = banner.ok && banner.data && banner.data.on ? banner.data : null;
    try { if (S.banner && localStorage.getItem('bn_closed') === String(S.banner.v)) S.bnClosed = true; } catch (e) {}
    return true;
  }
  async function afterState() {
    if (!S.st.ok) return renderGate();
    document.querySelector('#top').hidden = false; tabs.hidden = false;
    view.replaceChildren(h('p', { class: 'load', text: 'Загрузка…' }));
    if (!(await loadCatalog())) return;
    render();
    openFromParam((TG.initDataUnsafe && TG.initDataUnsafe.start_param) || S.st.start);
    track('hb');
  }

  async function boot() {
    if (!TG || !TG.initData) return renderOutside();
    try {
      TG.ready(); TG.expand();
      TG.setHeaderColor && TG.setHeaderColor('secondary_bg_color');
      TG.setBackgroundColor && TG.setBackgroundColor('secondary_bg_color');
      TG.disableVerticalSwipes && TG.disableVerticalSwipes();      // свайп по списку не закрывает мини-апп
      TG.BackButton.onClick(goBack);
    } catch (e) {}
    const r = await api('/api/tma/state', null, false);
    if (!r.ok) {
      view.replaceChildren(h('p', { class: 'empty' }, r.status === 403 ? 'Не удалось подтвердить вход. Закройте и откройте мини-апп заново.' : 'Нет соединения с сервером.', h('br'),
        h('button', { class: 'btn', type: 'button', style: 'margin-top:12px', text: 'Повторить', onclick: () => location.reload() })));
      return;
    }
    S.st = r.data;
    await afterState();
    setInterval(() => { if (!document.hidden) track('hb'); }, 15000);
  }
  boot();
})();
