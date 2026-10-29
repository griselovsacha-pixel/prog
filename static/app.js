const DUR = 320, ROW = 40, LINK = 90, R = 7, GAPL = 14, PANEL_W = 380;
const EASE = d3.easeCubicInOut;
const $ = id => document.getElementById(id);
const wait = ms => new Promise(r => setTimeout(r, ms));

// ---- аналитика: пульс (время на сайте, «онлайн») и действия с инструментами
// Токен приходит в ссылке бота (?k=…). Запоминаем его на вкладке и сразу убираем из адресной строки,
// чтобы он не попал в историю, скриншоты и «скопировать ссылку».
let K = new URLSearchParams(location.search).get('k');
try { if (K) sessionStorage.setItem('k', K); else K = sessionStorage.getItem('k'); } catch (e) {}
if (new URLSearchParams(location.search).has('k')) {
  try { history.replaceState(null, '', location.pathname + location.hash); } catch (e) {}
}
// ссылки наружу открываем только по http(s): «javascript:» и подобное отбрасываем
const safeUrl = u => {
  if (!u) return '';
  try { const x = new URL(u, location.href); return /^https?:$/.test(x.protocol) ? x.href : ''; } catch (e) { return ''; }
};
const SID = String(window.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random().toString(16).slice(2)).replace(/[^\w-]/g, '');
let curTool = '';
function track(ev, tool) {
  if (!K) return;
  try {
    fetch('/api/track', {
      method: 'POST', keepalive: true, headers: {'Content-Type': 'application/json', 'X-Access-Key': K},
      body: JSON.stringify({sid: SID, ev, tool: tool || null}),
    }).catch(() => {});
  } catch (e) {}
}
function startBeat() {
  track('hb');
  setInterval(() => { if (!document.hidden) track('hb'); }, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) track('hb'); });
}

const svg = d3.select('#tree');
const g = svg.append('g');
const linkG = g.append('g');
const nodeG = g.append('g');
const ctx = document.createElement('canvas').getContext('2d');
const layout = d3.tree().nodeSize([ROW, 1]);

let root, uid = 0, first = true, panelOpen = false;
let selNode = null, foundNode = null, foundIds = new Set();   // выбранный в карточке узел и найденный через поиск путь
let RATINGS = {};

const zoom = d3.zoom()
  .scaleExtent([0.15, 3])
  .extent(() => [[0, 0], [svg.node().clientWidth, svg.node().clientHeight]])
  .on('zoom', e => g.attr('transform', e.transform));
svg.call(zoom).on('dblclick.zoom', null);

const measure = t => {
  ctx.font = `500 15px ${getComputedStyle(document.body).fontFamily}`;
  return Math.ceil(ctx.measureText(t).width);
};
const curve = (sx, sy, tx, ty) => {
  const m = (sx + tx) / 2;
  return `M${sx},${sy}C${m},${sy} ${m},${ty} ${tx},${ty}`;
};
const isLeaf = d => !d.children && !d._children;
const open = d => { if (d._children) { d.children = d._children; d._children = null; } };
const closeDeep = d => {
  const kids = d.children || d._children;
  if (kids) kids.forEach(closeDeep);
  if (d.children) { d._children = d.children; d.children = null; }
};
const stagger = d => (d.parent && d.parent.children ? d.parent.children.indexOf(d) * 70 : 0);

function highlight(d) {
  const ids = new Set(d ? d.ancestors().map(n => n.id) : []);
  linkG.selectAll('g.lk').classed('hl', l => ids.has(l.target.id));
}

function fit(nodes) {
  const W = svg.node().clientWidth - (panelOpen && innerWidth > 760 ? PANEL_W : 0);
  const H = svg.node().clientHeight, TOP = innerWidth <= 700 ? 176 : 152, BOT = 50, PADX = 50;
  const minX = d3.min(nodes, d => d.px - GAPL - d.w - 12);
  const maxX = d3.max(nodes, d => d.px + R + 12);
  const minY = d3.min(nodes, d => d.py) - 24, maxY = d3.max(nodes, d => d.py) + 24;
  const bw = maxX - minX, bh = maxY - minY;
  const s = Math.max(0.35, Math.min(1, (W - 2 * PADX) / bw, (H - TOP - BOT) / bh));
  const tx = (W - s * bw) / 2 - s * minX;
  const ty = TOP + (H - TOP - BOT - s * bh) / 2 - s * minY;
  const t = d3.zoomIdentity.translate(tx, ty).scale(s);
  if (first) svg.call(zoom.transform, t);
  else svg.transition().duration(DUR).ease(EASE).call(zoom.transform, t);
}

function update(src) {
  layout(root);
  const nodes = root.descendants(), links = root.links();

  const colW = [];
  nodes.forEach(d => { colW[d.depth] = Math.max(colW[d.depth] || 0, d.w); });
  const colX = [0];
  for (let k = 1; k < colW.length; k++) colX[k] = colX[k - 1] + R + LINK + 8 + GAPL + colW[k];
  nodes.forEach(d => { d.py = d.x; d.px = colX[d.depth]; });

  const tr = (sel, delay) => first ? sel : sel.transition().duration(DUR).ease(EASE).delay(delay || 0);
  const at = d => `translate(${d.px},${d.py})`;

  // ---- nodes
  const node = nodeG.selectAll('g.node').data(nodes, d => d.id);
  const entered = new Set();
  const enter = node.enter().append('g').attr('class', 'node')
    .attr('transform', d => (first ? at(d) : `translate(${src.ox},${src.oy})`))
    .attr('opacity', first ? 1 : 0)
    .each(d => entered.add(d.id))
    .on('mouseenter', (e, d) => highlight(d))
    .on('mouseleave', () => highlight(null))
    .on('click', (e, d) => { e.stopPropagation(); onNode(d); });
  enter.append('circle').attr('class', 'ring').attr('r', R + 7);
  enter.append('circle').attr('class', 'dot').attr('r', R);
  enter.append('text').attr('x', -GAPL).attr('dy', '0.35em').text(d => d.data.name);

  const all = enter.merge(node);
  all.classed('leaf', isLeaf).classed('open', d => !!d.children)
    .classed('sel', d => d === selNode).classed('pth', d => foundIds.has(d.id)).classed('tgt', d => d === foundNode);
  tr(all, d => (entered.has(d.id) ? stagger(d) : 0)).attr('transform', at).attr('opacity', 1);

  node.exit().transition().duration(DUR).ease(EASE)
    .attr('transform', `translate(${src.px},${src.py})`).attr('opacity', 0).remove();

  // ---- links (кривая + точка на конце)
  const ex = d => d.source.px + R, ey = d => d.source.py;
  const tx = d => d.target.px - GAPL - d.target.w - 8, ty = d => d.target.py;
  const path = d => curve(ex(d), ey(d), tx(d), ty(d));
  const zx = first ? 0 : src.ox + R, zy = first ? 0 : src.oy;

  const lk = linkG.selectAll('g.lk').data(links, d => d.target.id);
  const lkEnter = lk.enter().append('g').attr('class', 'lk');
  lkEnter.append('path').attr('class', 'link').attr('d', d => (first ? path(d) : curve(zx, zy, zx, zy)));
  lkEnter.append('circle').attr('r', 3)
    .attr('cx', d => (first ? tx(d) : zx)).attr('cy', d => (first ? ty(d) : zy));

  const lkAll = lkEnter.merge(lk);
  lkAll.classed('pth', l => foundIds.has(l.target.id));
  const ldelay = d => (entered.has(d.target.id) ? stagger(d.target) : 0);
  tr(lkAll.select('path'), ldelay).attr('d', path);
  tr(lkAll.select('circle'), ldelay).attr('cx', tx).attr('cy', ty);

  const sx = src.px + R, sy = src.py, gone = lk.exit();
  gone.select('path').transition().duration(DUR).ease(EASE).attr('d', curve(sx, sy, sx, sy));
  gone.select('circle').transition().duration(DUR).ease(EASE).attr('cx', sx).attr('cy', sy);
  gone.transition().duration(DUR).remove();

  fit(nodes);
  nodes.forEach(d => { d.ox = d.px; d.oy = d.py; });
  first = false;
}

function onNode(d) {
  if (isLeaf(d)) return showPanel(d);
  if (d.children) closeDeep(d); else open(d);
  $('hint').classList.add('gone');
  update(d);
}

// ---- бирки инструмента (тип, цена, VPN, веб-версия, регистрация и т.д.)
const TYPE_LABEL = { bot: 'Бот', site: 'Сайт', 'bot+site': 'Бот + сайт', 'bot+miniapp': 'Бот + мини-приложение' };
const PRICE_LABEL = { free: 'Бесплатно', freemium: 'Есть бесплатные запросы', paid: 'Платно', unknown: 'Данные не заполнены' };
const PRICE_CLASS = { free: 'ok', freemium: 'ok', paid: 'warn', unknown: 'mute' };
const VPN_LABEL = { no: 'Работает без VPN', yes: 'Нужен VPN', unknown: 'Данные не заполнены' };
const VPN_CLASS = { no: 'ok', yes: 'bad', unknown: 'mute' };
function toolBadges(t) {
  const out = [];
  if (TYPE_LABEL[t.type]) out.push(['type', TYPE_LABEL[t.type]]);
  if (t.price) out.push([PRICE_CLASS[t.price] || 'mute', t.price_text || PRICE_LABEL[t.price] || '']);
  if (t.vpn) out.push([VPN_CLASS[t.vpn] || 'mute', t.vpn_text || VPN_LABEL[t.vpn] || '']);
  if (t.web === true) out.push(['info', 'Есть веб-версия']);
  (Array.isArray(t.extra) ? t.extra : []).forEach(x => x && x.t && out.push([x.k === 'reg' ? 'info' : 'neutral', String(x.t)]));
  return out.filter(b => b[1]);
}
// пустые категории (например, будущий раздел без инструментов) на сайте не показываем
function prune(n) {
  if (!n || !Array.isArray(n.children)) return n;
  const kids = n.children.map(prune).filter(c => c && (!Array.isArray(c.children) || c.children.length));
  return { ...n, children: kids };
}

// ---- поиск: раскрыть путь к узлу и подсветить его
const leafCount = n => (n.children && n.children.length ? n.children.reduce((s, c) => s + leafCount(c), 0) : 1);
const openDeep = n => { open(n); (n.children || []).forEach(openDeep); };
function clearFound() {
  foundNode = null; foundIds = new Set();
  nodeG.selectAll('g.node').classed('pth', false).classed('tgt', false);
  linkG.selectAll('g.lk').classed('pth', false);
}
function revealNode(d) {
  if (!d || !root) return;
  const path = d.ancestors().reverse();                          // корень … найденный узел
  const on = new Set(path.map(n => n.id));
  // лишние раскрытые ветки рядом с путём сворачиваем, чтобы путь был виден чётко
  path.forEach(a => (a.children || a._children || []).forEach(c => { if (!on.has(c.id)) closeDeep(c); }));
  path.slice(0, -1).forEach(open);
  if (!isLeaf(d)) {                                               // категория: показываем, что внутри
    closeDeep(d);
    if (leafCount(d.data) <= 14) openDeep(d); else open(d);
  }
  foundIds = on; foundNode = d;
  $('hint').classList.add('gone');
  if (isLeaf(d)) showPanel(d);                                    // открывает карточку и перерисовывает дерево
  else if (panelOpen) togglePanel(false);
  else update(root);
}
window.revealNode = revealNode; window.clearFound = clearFound;

function showPanel(d) {
  $('crumb').textContent = d.ancestors().reverse().slice(1, -1).map(n => n.data.name).join(' › ');
  $('ptitle').textContent = d.data.name;
  $('pdesc').textContent = d.data.desc || '';
  $('pbadges').replaceChildren(...toolBadges(d.data).map(([k, txt]) =>
    Object.assign(document.createElement('span'), { className: 'bd ' + k, textContent: txt })));
  if (d.data.note) $('pbadges').append(Object.assign(document.createElement('small'), { className: 'bnote', textContent: d.data.note }));
  $('pbody').scrollTop = 0;
  $('ptags').replaceChildren(...(d.data.tags || []).map(t =>
    Object.assign(document.createElement('span'), { textContent: t })));
  const isTg = u => /^https?:\/\/t(elegram)?\.me\//i.test(u || '');
  const bot = safeUrl(d.data.bot), eternal = safeUrl(d.data.eternal);
  const bTg = isTg(d.data.bot);
  $('btnBot').hidden = !bot;
  if (bot) { $('btnBot').href = bot; $('btnBot').textContent = (bTg ? 'Перейти в бота' : 'Перейти на сайт') + ' ↗'; }
  const showEternal = eternal && eternal !== bot;                 // не дублируем одну и ту же ссылку
  $('btnEternal').hidden = !showEternal;
  if (showEternal) { $('btnEternal').href = eternal; $('btnEternal').textContent = bTg ? 'Вечная ссылка' : 'Зеркало'; }
  $('pfootNote').hidden = !showEternal;
  if (showEternal) $('pfootNote').textContent = bTg ? 'Вечная ссылка работает, если бот неактивен.' : 'Зеркало работает, если основной адрес недоступен.';
  selNode = d;
  nodeG.selectAll('g.node').classed('sel', n => n === d);
  curTool = d.ancestors().reverse().slice(1).map(n => n.data.name).join(' › ');
  track('select', curTool);
  renderRating(curTool);
  togglePanel(true);
  requestAnimationFrame(updateFade);
  setTimeout(updateFade, 450);
}

function togglePanel(on) {
  panelOpen = on;
  $('panel').classList.toggle('open', on);
  $('panel').setAttribute('aria-hidden', String(!on));
  document.body.classList.toggle('panel-open', on);
  if (!on) { selNode = null; nodeG.selectAll('g.node').classed('sel', false); }
  update(root);
}

function deny(msg) {
  const el = $('deny');
  el.textContent = msg || 'Доступ только по персональной ссылке из Telegram-бота.';
  el.hidden = false;
  $('hint').style.display = 'none';
}

function zoomStep(k) {
  const c = [svg.node().clientWidth / 2, svg.node().clientHeight / 2];
  svg.transition().duration(260).call(zoom.scaleBy, k, c);
}
$('zoomIn').onclick = () => zoomStep(1.4);
$('zoomOut').onclick = () => zoomStep(1 / 1.4);
$('zoomFit').onclick = () => root && update(root);
// двойной клик по пустому месту — приблизить к этой точке (одиночный клик по пустому месту закрывает карточку)
svg.on('dblclick', e => { if (e.target === svg.node()) svg.transition().duration(260).call(zoom.scaleBy, 1.6, d3.pointer(e)); });

function init(data) {
  root = d3.hierarchy(prune(data));
  root.each(d => { d.id = ++uid; d.w = measure(d.data.name); });
  const nTools = root.leaves().length, nCats = root.descendants().length - nTools - 1;
  try { window.HSearch && HSearch.build(root); } catch (e) { console.error('search index', e); }   // индекс строим, пока все ветки раскрыты
  closeDeep(root);
  root.ox = root.oy = 0;
  update(root);                                  // сначала сразу виден только корень
  showStats(nTools, nCats);
  $('hint').textContent = 'Нажимайте на кружки, чтобы раскрывать ветки';
  setTimeout(() => { open(root); update(root); try { window.HSearch && HSearch.show(); } catch (e) {} }, 450);   // и он сам раскрывается
}

$('btnBot').addEventListener('click', () => track('bot', curTool));
$('btnBot').addEventListener('auxclick', () => track('bot', curTool));
$('btnEternal').addEventListener('click', () => track('eternal', curTool));
$('btnEternal').addEventListener('auxclick', () => track('eternal', curTool));
$('close').onclick = () => togglePanel(false);
svg.on('click', () => { if (panelOpen) togglePanel(false); else clearFound(); });
addEventListener('keydown', e => e.key === 'Escape' && panelOpen && togglePanel(false));
addEventListener('resize', () => root && update(root));

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  $('theme').textContent = t === 'dark' ? 'Light Mode' : 'Dark Mode';
  try { localStorage.setItem('theme', t); } catch (e) {}
}
let saved = 'dark';
try { saved = localStorage.getItem('theme') || 'dark'; } catch (e) {}
setTheme(saved);
$('theme').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');


// ---- подсказка «можно листать» у длинного описания
function updateFade() {
  const el = $('pbody');
  el.classList.toggle('fade', el.scrollHeight - el.scrollTop - el.clientHeight > 8);
}
$('pbody').addEventListener('scroll', updateFade, { passive: true });

// ---- счётчик внизу слева
const plural = (n, f) => { const a = n % 100, b = n % 10; return a > 10 && a < 20 ? f[2] : b === 1 ? f[0] : b > 1 && b < 5 ? f[1] : f[2]; };
function showStats(tools, cats) {
  const el = $('stats'); el.hidden = false;
  const lt = plural(tools, ['инструмент', 'инструмента', 'инструментов']), lc = plural(cats, ['категория', 'категории', 'категорий']);
  const t0 = performance.now();
  const tick = now => {
    const p = Math.min(1, (now - t0) / 900), e = 1 - Math.pow(1 - p, 3);
    el.innerHTML = `<b>${Math.round(tools * e)}</b> ${lt} · <b>${Math.round(cats * e)}</b> ${lc}`;
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}


// ---- звёзды рейтинга: средняя (дробная заливка через clip-path) и своя оценка (кликабельная)
function starsAvgSvg(avg) {
  const pct = Math.max(0, Math.min(5, avg)) / 5 * 100;
  return `<span class="stars-avg"><span class="row bg">★★★★★</span><span class="row fg" style="width:${pct}%">★★★★★</span></span>`;
}
function renderRating(tool) {
  const box = $('prating'), r = RATINGS[tool] || { avg: 0, count: 0, mine: 0 };
  box.innerHTML = `
    <div class="ravg">${starsAvgSvg(r.avg)}<b>${r.count ? r.avg.toFixed(1).replace('.', ',') : '—'}</b><span>/5</span>
      <span class="rcount">${r.count ? `· ${r.count} ${plural(r.count, ['оценка', 'оценки', 'оценок'])}` : '· пока нет оценок'}</span></div>
    <div class="ryours">
      <span>Ваша оценка:</span>
      <span class="rstars" role="radiogroup" aria-label="Оценить инструмент">
        ${[5, 4, 3, 2, 1].map(n => `<button type="button" class="rb${n <= (r.mine || 0) ? ' on' : ''}" data-n="${n}" aria-label="${n} из 5">★</button>`).join('')}
      </span>
    </div>`;
  box.querySelectorAll('.rb').forEach(btn => btn.onclick = () => sendRating(tool, +btn.dataset.n));
}
async function sendRating(tool, stars) {
  if (!K) return;
  RATINGS[tool] = { ...(RATINGS[tool] || { avg: 0, count: 0 }), mine: stars };   // мгновенный отклик
  renderRating(tool);
  track('rate', tool);
  try {
    const res = await fetch('/api/rate', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Access-Key': K },
      body: JSON.stringify({ tool, stars }),
    });
    if (res.ok) { const d = await res.json(); RATINGS[tool] = { avg: d.avg, count: d.count, mine: d.mine }; if (curTool === tool) renderRating(tool); }
  } catch (e) {}
}

// ---- заставка: лупа проезжает по названию и «проявляет» его, затем уезжает наверх
const INTRO_SCALE = 1.6;
function playIntro() {
  const text = $('brandText'), splash = $('splash');
  if (!text.animate) { splash.remove(); return Promise.resolve(); }   // Web Animations нет — пропускаем
  const lens = $('lens'), pop = $('pop'), inner = $('lensIn');
  const r = text.getBoundingClientRect(), cs = getComputedStyle(text);
  const L = Math.round(Math.min(160, Math.max(100, r.height * 2.2))), Rr = L / 2, MAG = 1.55;
  const cy = r.top + r.height / 2, x0 = r.left - Rr * 0.7, x1 = r.right + Rr * 0.7;
  const D = 1700, DELAY = 400, ease = 'cubic-bezier(.65,.05,.35,1)';
  const opt = { duration: D, delay: DELAY, easing: ease, fill: 'both' };

  lens.style.width = lens.style.height = L + 'px';
  inner.textContent = text.textContent;
  Object.assign(inner.style, {
    font: `800 ${parseFloat(cs.fontSize) * INTRO_SCALE * MAG}px ${cs.fontFamily}`,
    lineHeight: r.height * MAG + 'px', top: (Rr - r.height * MAG / 2) + 'px',
  });
  const at = x => `translate(${x - Rr}px, ${cy - Rr}px)`;                    // лупа
  const ix = x => `translateX(${Rr - (x - r.left) * MAG}px)`;                // увеличенный текст внутри неё
  const f = x => (1 - (x - r.left) / r.width) * 100 + '%';                   // граница «проявления»
  lens.style.transform = at(x0);
  inner.style.transform = ix(x0);
  const sweep = lens.animate([{ transform: at(x0) }, { transform: at(x1) }], opt);
  inner.animate([{ transform: ix(x0) }, { transform: ix(x1) }], opt);
  text.animate([{ clipPath: `inset(-40% ${f(x0)} -40% 0)` }, { clipPath: `inset(-40% ${f(x1)} -40% 0)` }], opt);
  pop.animate([{ transform: 'scale(.3)', opacity: 0 }, { transform: 'scale(1)', opacity: 1 }],
    { duration: 260, delay: 80, easing: 'cubic-bezier(.3,1.5,.5,1)', fill: 'both' });
  pop.animate([{ transform: 'scale(1)', opacity: 1 }, { transform: 'scale(1.3)', opacity: 0 }],
    { duration: 240, delay: DELAY + D - 40, easing: 'ease-in', fill: 'both' });
  setTimeout(() => splash.remove(), DELAY + D + 900);
  return sweep.finished.catch(() => {}).then(() => {
    document.body.classList.add('reveal');          // значок рисуется, под названием появляется подпись
    document.querySelector('.lupa').classList.add('draw');
    return wait(1100);                              // пауза, чтобы заставку успели рассмотреть
  });
}

// ---- запуск: заставка (название по центру → наверх) → дерево
(async () => {
  const k = K;
  const load = (async () => {
    if (!k) return {};
    try {
      const r = await fetch('/api/tools', { headers: { 'X-Access-Key': k } });
      if (r.status === 429) return { err: 'Слишком много запросов. Подождите минуту и обновите страницу.' };
      if (r.status === 403) {
        const j = await r.json().catch(() => ({}));
        if (j && j.error === 'terms') return { err: 'Чтобы открыть каталог, примите пользовательское соглашение в Telegram-боте: отправьте /start и нажмите «Я ознакомился и со всем согласен».' };
      }
      return r.ok ? { data: await r.json() } : {};
    } catch (e) { return { err: 'Ошибка загрузки: ' + (e && e.message ? e.message : e) }; }
  })();

  try { await playIntro(); } catch (e) { $('splash') && $('splash').remove(); }   // лупа проявляет название
  document.body.classList.remove('intro');       // название уезжает наверх
  document.querySelector('.lupa').classList.add('draw');
  const res = await load;
  await wait(450);
  if (!res.data) return deny(res.err);
  try { init(res.data); startBeat(); } catch (e) { deny('Ошибка: ' + (e && e.message ? e.message : e)); }
  if (K) fetch('/api/ratings', { headers: { 'X-Access-Key': K } })
    .then(r => (r.ok ? r.json() : {})).then(d => { RATINGS = d || {}; }).catch(() => {});
})();
