'use strict';
/* Мини-апп Harledy Project = точная копия сайта (те же index.html, style.css, app.js, d3-дерево).
 * Этот файл только входит через Telegram и запускает скрипты сайта:
 *   1) проверяет подпись Telegram, соглашение и подписки на сервере (/api/tma/state);
 *   2) кладёт в sessionStorage 'k' строку «tma <initData>» — app.js, reviews.js, ai.js и banner.js берут её как токен;
 *   3) подключает скрипты сайта в том же порядке, что и обычная страница;
 *   4) в Telegram: ссылки наружу открываются средствами Telegram, кнопка «Назад» закрывает карточку. */
(() => {
  const TG = window.Telegram && Telegram.WebApp;
  const HDR = TG ? 'tma ' + TG.initData : '';
  const SCRIPTS = ['https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js', '/search.js?v=10', '/app.js?v=11', '/reviews.js?v=2', '/banner.js?v=2'];
  let state = null;

  const el = (tag, css, text) => { const n = document.createElement(tag); if (css) n.style.cssText = css; if (text) n.textContent = text; return n; };
  const safeUrl = u => { try { const x = new URL(u, location.href); return /^https?:$/.test(x.protocol) ? x.href : ''; } catch (e) { return ''; } };
  const isTg = u => /^https:\/\/(t|telegram)\.me\//i.test(u);
  function openUrl(u) {
    u = safeUrl(u); if (!u) return;
    if (isTg(u) && TG.openTelegramLink) TG.openTelegramLink(u); else if (TG.openLink) TG.openLink(u); else window.open(u, '_blank', 'noopener');
  }

  async function getState(path) {
    try {
      const r = await fetch(path || '/api/tma/state', { method: path ? 'POST' : 'GET', headers: { 'X-Access-Key': HDR, 'Content-Type': 'application/json' }, body: path ? '{}' : undefined });
      return r.ok ? await r.json() : null;
    } catch (e) { return null; }
  }

  // ---- экран входа в стиле сайта (цвета берём из его CSS-переменных)
  let layer = null;
  function showGate(title, text, buttons, extra) {
    if (!layer) {
      layer = el('div', 'position:fixed;inset:0;z-index:50;display:grid;place-items:center;padding:24px;background:var(--bg);color:var(--fg);text-align:center;overflow:auto');
      document.body.append(layer);
    }
    const box = el('div', 'max-width:380px;width:100%');
    box.append(el('h2', 'color:var(--fg-h);margin:0 0 10px;font-size:22px', title), el('p', 'margin:0 0 18px;line-height:1.5', text));
    (extra || []).forEach(n => box.append(n));
    buttons.forEach(b => {
      const n = el('button', 'display:block;width:100%;margin:8px 0 0;padding:12px 16px;border:0;border-radius:10px;font:inherit;font-weight:600;cursor:pointer;' +
        (b.ghost ? 'background:var(--ghost);color:var(--ghost-t)' : 'background:var(--btn);color:#fff'), b.text);
      n.addEventListener('click', async () => { n.disabled = true; try { await b.fn(); } finally { n.disabled = false; } });
      box.append(n);
    });
    layer.replaceChildren(box);
  }
  function gate() {
    const st = state;
    if (!st.terms) {
      showGate('Добро пожаловать', 'Перед началом ознакомьтесь с пользовательским соглашением. Нажав кнопку ниже, вы подтверждаете, что полностью с ним согласны.', [
        ...(st.site ? [{ text: 'Прочитать соглашение', ghost: true, fn: () => openUrl(st.site + '/terms') }] : []),
        { text: 'Я ознакомился и согласен', fn: async () => { const s = await getState('/api/tma/terms'); if (s) { state = s; proceed(); } else alert('Не получилось, попробуйте ещё раз'); } }]);
    } else {
      const rows = st.subs.map(s => { const r = el('div', 'display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;margin:6px 0;background:var(--panel);border:1px solid var(--line);border-radius:10px;text-align:left');
        r.append(el('span', 'color:var(--fg-h)', s.title));
        if (s.ok) r.append(el('span', 'color:var(--mute)', '✓ готово'));
        else { const b = el('button', 'border:0;border-radius:8px;padding:6px 12px;background:var(--btn);color:#fff;font:inherit;cursor:pointer', 'Подписаться'); b.addEventListener('click', () => openUrl(s.link)); r.append(b); }
        return r; });
      showGate('Нужна подписка', 'Чтобы открыть каталог, подпишитесь на канал и группу проекта.', [
        { text: 'Я подписался — проверить', fn: async () => { const s = await getState(); if (!s) return alert('Не удалось проверить, попробуйте ещё раз'); state = s; if (s.ok) proceed(); else { gate(); alert('Подписка пока не найдена'); } } }], rows);
    }
  }

  // ---- запуск сайта
  const loadScript = src => new Promise((res, rej) => { const s = document.createElement('script'); s.src = src; s.onload = res; s.onerror = () => rej(new Error(src)); document.head.append(s); });

  function patchFetch() {      // сервер перезапустился и забыл сессию: один раз заново подтверждаем вход и повторяем запрос
    const orig = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const r = await orig(input, init);
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const hd = (init && init.headers) || {};
      const k = hd['X-Access-Key'] || (hd.get && hd.get('X-Access-Key')) || '';
      if (r.status === 403 && url.startsWith('/api/') && String(k).startsWith('tma ')) {
        const s = await getState();
        if (s && s.ok) return orig(input, init);
      }
      return r;
    };
  }
  function telegramBits() {
    try { TG.setHeaderColor('#1a1a1a'); TG.setBackgroundColor('#1a1a1a'); TG.disableVerticalSwipes(); } catch (e) {}
    // ссылки на другие сайты и в Telegram открываем средствами Telegram, свои страницы — внутри мини-аппа
    document.addEventListener('click', e => {
      const a = e.target.closest && e.target.closest('a[href]'); if (!a) return;
      const u = safeUrl(a.href); if (!u) return;
      let same = false; try { same = new URL(u).origin === location.origin; } catch (x) {}
      if (same) return;
      e.preventDefault(); openUrl(u);
    }, true);
    // «Назад» в Telegram закрывает отзывы, затем карточку инструмента
    const upd = () => { const on = document.body.classList.contains('rev-open') || document.body.classList.contains('panel-open'); try { on ? TG.BackButton.show() : TG.BackButton.hide(); } catch (x) {} };
    try { TG.BackButton.onClick(() => { const b = document.body.classList; const t = document.getElementById(b.contains('rev-open') ? 'revClose' : 'close'); if (t) t.click(); }); } catch (x) {}
    new MutationObserver(upd).observe(document.body, { attributes: true, attributeFilter: ['class'] });
  }
  async function proceed() {
    if (!state.ok) return gate();
    if (layer) { layer.remove(); layer = null; }
    try { sessionStorage.setItem('k', HDR); } catch (e) {}
    patchFetch(); telegramBits();
    try { for (const s of SCRIPTS) await loadScript(s); }
    catch (e) { showGate('Не удалось загрузить', 'Проверьте интернет и откройте мини-апп ещё раз.', [{ text: 'Повторить', fn: () => location.reload() }]); return; }
    // держим сессию свежей: сервер помнит вход ограниченное время
    const keep = () => getState();
    setInterval(() => { if (!document.hidden) keep(); }, 10 * 60 * 1000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) keep(); });
  }

  (async () => {
    if (!TG || !TG.initData) return showGate('Откройте через Telegram', 'Этот каталог работает как мини-приложение внутри Telegram. Откройте его из бота Harledy Project.', [{ text: 'Открыть сайт', ghost: true, fn: () => { location.href = '/'; } }]);
    try { TG.ready(); TG.expand(); } catch (e) {}
    state = await getState();
    if (!state) return showGate('Не удалось войти', 'Закройте и откройте мини-апп заново.', [{ text: 'Повторить', fn: () => location.reload() }]);
    proceed();
  })();
})();
