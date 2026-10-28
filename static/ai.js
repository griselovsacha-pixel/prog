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
  function render(el, text) {
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
  }

  // ---------- UI ----------
  function add(cls, text) {
    const d = document.createElement('div');
    d.className = 'msg ' + cls;
    if (text !== undefined) { if (cls.indexOf('ai') >= 0) render(d, text); else d.textContent = text; }
    log.appendChild(d);
    d.scrollIntoView({ block: 'end', behavior: 'smooth' });
    return d;
  }
  function typing() {
    const d = document.createElement('div');
    d.className = 'msg ai';
    d.setAttribute('role', 'status');
    d.setAttribute('aria-label', 'ИИ думает');
    d.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>';
    log.appendChild(d);
    d.scrollIntoView({ block: 'end', behavior: 'smooth' });
    return d;
  }
  function sync() {
    send.disabled = busy || !q.value.trim();
    q.style.height = 'auto';
    q.style.height = Math.min(q.scrollHeight, 160) + 'px';
  }

  const ERR = {
    403: 'Доступ закрыт. Откройте сайт по личной ссылке из Telegram-бота и вернитесь на эту страницу.',
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
    const wait = typing();
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), 55000);
    let answer = null, err = null;
    try {
      const r = await fetch('/api/ai', {
        method: 'POST', signal: ctl.signal,
        headers: { 'Content-Type': 'application/json', 'X-Access-Key': K || '' },
        body: JSON.stringify({ q: text, history: history_.slice(-8) }),
      });
      if (r.ok) { const j = await r.json(); answer = j.answer; }
      else err = ERR[r.status] || 'Не удалось получить ответ. Попробуйте ещё раз.';
    } catch (e) {
      err = 'Не удалось получить ответ. Проверьте соединение и попробуйте ещё раз.';
    }
    clearTimeout(to);
    wait.remove();
    if (answer) {
      history_.push({ role: 'user', text }, { role: 'assistant', text: answer });
      add('ai', answer);
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
