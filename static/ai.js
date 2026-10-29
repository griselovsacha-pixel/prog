'use strict';
(function () {
  const $ = (id) => document.getElementById(id);
  const log = $('log'), q = $('q'), send = $('send'), form = $('form'), intro = $('intro');

  // тема — та же, что выбрана на главной
  try { const t = localStorage.getItem('theme'); if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t; } catch (e) {}

  // токен доступа: тот же, что использует главная страница
  let K = new URLSearchParams(location.search).get('k');
  try { if (K) sessionStorage.setItem('k', K); else K = sessionStorage.getItem('k'); } catch (e) {}
  if (new URLSearchParams(location.search).has('k')) {
    try { history.replaceState(null, '', location.pathname); } catch (e) {}
  }

  const history_ = [];           // {role: 'user'|'assistant', text}
  let busy = false;

  // ---------- безопасный рендер ответа (без innerHTML) ----------
  const INLINE = /\*\*([^*]+?)\*\*|`([^`]+?)`|\[([^\]]+?)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"')\]]+)/g;

  function safeHref(u) {
    try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.href : ''; } catch (e) { return ''; }
  }
  function link(href, label) {
    const h = safeHref(href);
    if (!h) return document.createTextNode(label);
    const a = document.createElement('a');
    a.href = h; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.textContent = label;
    return a;
  }
  function inline(parent, text) {
    let last = 0, m;
    INLINE.lastIndex = 0;
    while ((m = INLINE.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1] !== undefined) { const b = document.createElement('b'); b.textContent = m[1]; parent.appendChild(b); }
      else if (m[2] !== undefined) { const c = document.createElement('code'); c.textContent = m[2]; parent.appendChild(c); }
      else if (m[3] !== undefined) parent.appendChild(link(m[4], m[3]));
      else {
        let url = m[5], tail = '';
        const t = url.match(/[.,;:!?]+$/);
        if (t) { tail = t[0]; url = url.slice(0, -tail.length); }
        parent.appendChild(link(url, url));
        if (tail) parent.appendChild(document.createTextNode(tail));
      }
      last = INLINE.lastIndex;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }
  // кнопки Telegram: ИИ пишет токены {{DEV}} / {{CHANNEL}}, а сайт рисует из них синие кнопки
  const TG_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>';
  const BTN_LABEL = { dev: 'Контакт разработчика', channel: 'Harledy Project' };
  const esc = (u) => u.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  function extractButtons(text, links) {
    const found = [];
    const add_ = (k) => { if (links && safeHref(links[k]) && found.indexOf(k) < 0) found.push(k); };
    text = text.replace(/\{\{\s*(DEV|CHANNEL)\s*\}\}/gi, (m, k) => { add_(k.toLowerCase()); return ''; });
    // страховка: если модель всё же написала саму ссылку, тоже превращаем её в кнопку
    ['dev', 'channel'].forEach((k) => {
      const u = links && links[k];
      if (!u) return;
      const re = new RegExp('(?:\\[[^\\]]*\\]\\(\\s*' + esc(u) + '\\s*\\)|' + esc(u) + ')', 'gi');
      text = text.replace(re, () => { add_(k); return ''; });
    });
    text = text.replace(/\(\s*\)/g, '').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
    return { text, found };
  }
  function tgButtons(el, found, links) {
    if (!found.length) return;
    const row = document.createElement('div');
    row.className = 'tgrow';
    found.forEach((k) => {
      const a = document.createElement('a');
      a.className = 'btn-tg'; a.href = safeHref(links[k]); a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.innerHTML = TG_ICON;                           // статичная иконка из кода, пользовательский текст сюда не попадает
      a.appendChild(document.createTextNode(' ' + BTN_LABEL[k]));
      row.appendChild(a);
    });
    el.appendChild(row);
  }
  function render(el, text, links) {
    const ex = extractButtons(text, links);
    text = ex.text;
    const lines = text.replace(/\r/g, '').split('\n');
    let list = null, para = null;
    const flush = () => { list = null; para = null; };
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, '');
      const ul = line.match(/^\s*[-*•]\s+(.*)$/), ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        const tag = ul ? 'UL' : 'OL';
        if (!list || list.tagName !== tag) { list = document.createElement(tag.toLowerCase()); el.appendChild(list); para = null; }
        const li = document.createElement('li');
        inline(li, (ul || ol)[1]);
        list.appendChild(li);
      } else if (!line.trim()) {
        flush();
      } else {
        list = null;
        if (!para) { para = document.createElement('p'); el.appendChild(para); }
        else para.appendChild(document.createElement('br'));
        inline(para, line.replace(/^#{1,6}\s+/, ''));
      }
    }
    tgButtons(el, ex.found, links);
  }

  // ---------- UI ----------
  function add(cls, text, links) {
    const d = document.createElement('div');
    d.className = 'msg ' + cls;
    if (text !== undefined) { if (cls.indexOf('ai') >= 0) render(d, text, links); else d.textContent = text; }
    log.appendChild(d);
    d.scrollIntoView({ block: 'end', behavior: 'smooth' });
    return d;
  }
  // анимация ожидания: светящийся «шар» с орбитами, мерцающий статус, скелетон-строки и таймер
  const PHASES = {
    ru: ['Читаю ваш вопрос', 'Ищу в каталоге', 'Подбираю инструменты', 'Формулирую ответ'],
    uk: ['Читаю ваше запитання', 'Шукаю в каталозі', 'Підбираю інструменти', 'Формулюю відповідь'],
    kk: ['Сұрағыңызды оқып жатырмын', 'Каталогтан іздеп жатырмын', 'Құралдарды таңдап жатырмын', 'Жауап жазып жатырмын'],
    en: ['Reading your question', 'Searching the catalog', 'Picking the best tools', 'Writing the answer'],
  };
  const SLOW = {
    ru: 'Модель занята — подключаю запасную, секунду…', uk: 'Модель зайнята — підключаю запасну, секунду…',
    kk: 'Модель бос емес — қосалқысын қосып жатырмын…', en: 'The model is busy — switching to a backup…',
  };
  function langOf(t) {
    if (/[әғқңөұүһ]/i.test(t)) return 'kk';
    if (/[іїєґ]/i.test(t)) return 'uk';
    if (/[а-яё]/i.test(t)) return 'ru';
    return /[a-z]/i.test(t) ? 'en' : 'ru';
  }
  function el(tag, cls, parent) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (parent) parent.appendChild(n);
    return n;
  }
  function typing(question) {
    const lang = langOf(question || ''), phases = PHASES[lang];
    const d = el('div', 'msg ai thinking');
    d.setAttribute('role', 'status');
    d.setAttribute('aria-label', 'ИИ думает');
    const row = el('div', 'think', d), orb = el('div', 'orb', row);
    el('span', 'ring', orb); el('span', 'ring r2', orb); el('span', 'core', orb);
    ['s1', 's2', 's3'].forEach((c, i) => { const s = el('span', 'sat ' + c, orb); s.style.animationDelay = (-i * 0.9) + 's'; });
    const tt = el('div', 'tt', row), txt = el('span', 'shim', tt), sub = el('span', 'sub', tt);
    const sk = el('div', 'sk', d);
    el('b', '', sk); el('b', '', sk); el('b', '', sk);
    txt.textContent = phases[0] + '…';
    log.appendChild(d);
    d.scrollIntoView({ block: 'end', behavior: 'smooth' });

    const t0 = Date.now();
    let i = 0, slow = false;
    const swap = (s) => { txt.classList.add('swap'); setTimeout(() => { txt.textContent = s; txt.classList.remove('swap'); }, 220); };
    const timer = setInterval(() => {
      const sec = (Date.now() - t0) / 1000;
      sub.textContent = sec >= 3 ? sec.toFixed(0) + ' c' : '';
      if (sec > 9 && !slow) { slow = true; swap(SLOW[lang]); }
    }, 500);
    const phase = setInterval(() => {
      if (slow) return;
      i = (i + 1) % phases.length;
      swap(phases[i] + '…');
    }, 2300);
    return { el: d, stop() { clearInterval(timer); clearInterval(phase); d.remove(); } };
  }
  function sync() {
    send.disabled = busy || !q.value.trim();
    q.style.height = 'auto';
    q.style.height = Math.min(q.scrollHeight, 160) + 'px';
  }

  const ERR = {
    403: 'Доступ закрыт. Откройте сайт по личной ссылке из Telegram-бота и примите пользовательское соглашение (/start в боте).',
    429: 'Слишком много вопросов подряд. Подождите немного и спросите снова.',
    503: 'Сервер сейчас занят. Повторите вопрос через несколько секунд.',
  };

  async function ask(text) {
    if (busy) return;
    text = text.trim();
    if (!text) return;
    busy = true;
    if (intro) { intro.hidden = true; }
    add('user', text);
    q.value = '';
    sync();
    form.classList.add('busy');
    const wait = typing(text);
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), 55000);
    let answer = null, links = null, err = null;
    try {
      const r = await fetch('/api/ai', {
        method: 'POST', signal: ctl.signal,
        headers: { 'Content-Type': 'application/json', 'X-Access-Key': K || '' },
        body: JSON.stringify({ q: text, history: history_.slice(-8) }),
      });
      if (r.ok) { const j = await r.json(); answer = j.answer; links = j.links; }
      else err = ERR[r.status] || 'Не удалось получить ответ. Попробуйте ещё раз.';
    } catch (e) {
      err = 'Не удалось получить ответ. Проверьте соединение и попробуйте ещё раз.';
    }
    clearTimeout(to);
    wait.stop();
    form.classList.remove('busy');
    if (answer) {
      history_.push({ role: 'user', text }, { role: 'assistant', text: answer });
      add('ai', answer, links);
    } else {
      add('ai err', err || 'Пустой ответ. Попробуйте переформулировать вопрос.');
    }
    busy = false;
    sync();
    q.focus();
  }

  form.addEventListener('submit', (e) => { e.preventDefault(); ask(q.value); });
  q.addEventListener('input', sync);
  q.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(q.value); }
  });
  $('chips').addEventListener('click', (e) => {
    const b = e.target.closest('.chip');
    if (b) ask(b.textContent);
  });
  sync();
  q.focus();
})();
