const DUR = 600, ROW = 50, LINK = 90, R = 7, GAPL = 14, PANEL_W = 380;
const EASE = d3.easeCubicInOut;
const $ = id => document.getElementById(id);
const wait = ms => new Promise(r => setTimeout(r, ms));

const svg = d3.select('#tree');
const g = svg.append('g');
const linkG = g.append('g');
const nodeG = g.append('g');
const ctx = document.createElement('canvas').getContext('2d');
const layout = d3.tree().nodeSize([ROW, 1]);

let root, uid = 0, first = true, panelOpen = false;

const zoom = d3.zoom()
  .scaleExtent([0.3, 2.5])
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
  const H = svg.node().clientHeight, TOP = 120, BOT = 50, PADX = 50;
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
  enter.append('circle').attr('r', R);
  enter.append('text').attr('x', -GAPL).attr('dy', '0.35em').text(d => d.data.name);

  const all = enter.merge(node);
  all.classed('leaf', isLeaf).classed('open', d => !!d.children);
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

function showPanel(d) {
  $('crumb').textContent = d.ancestors().reverse().slice(1, -1).map(n => n.data.name).join(' › ');
  $('ptitle').textContent = d.data.name;
  $('pdesc').textContent = d.data.desc || '';
  $('ptags').replaceChildren(...(d.data.tags || []).map(t =>
    Object.assign(document.createElement('span'), { textContent: t })));
  const setBtn = (id, url) => { const a = $(id); a.hidden = !url; if (url) a.href = url; };
  setBtn('btnBot', d.data.bot);
  setBtn('btnEternal', d.data.eternal);
  nodeG.selectAll('g.node').classed('sel', n => n === d);
  togglePanel(true);
}

function togglePanel(on) {
  panelOpen = on;
  $('panel').classList.toggle('open', on);
  $('panel').setAttribute('aria-hidden', String(!on));
  if (!on) nodeG.selectAll('g.node').classed('sel', false);
  update(root);
}

function deny(msg) {
  const el = $('deny');
  el.textContent = msg || 'Доступ только по персональной ссылке из Telegram-бота.';
  el.hidden = false;
  $('hint').style.display = 'none';
}

function init(data) {
  root = d3.hierarchy(data);
  root.each(d => { d.id = ++uid; d.w = measure(d.data.name); });
  closeDeep(root);
  root.ox = root.oy = 0;
  update(root);                                  // сначала сразу виден только корень
  $('hint').textContent = 'Нажимайте на кружки, чтобы раскрывать ветки';
  setTimeout(() => { open(root); update(root); }, 450);   // и он сам раскрывается
}

$('close').onclick = () => togglePanel(false);
svg.on('click', () => panelOpen && togglePanel(false));
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

// ---- запуск: заставка (название по центру → наверх) → дерево
(async () => {
  const k = new URLSearchParams(location.search).get('k');
  if (k) {
    const q = '?k=' + encodeURIComponent(k);
    $('navAbout').href = '/about' + q;
    $('navCollab').href = '/collab' + q;
  }
  const load = (async () => {
    if (!k) return {};
    try {
      const r = await fetch('/api/tools?k=' + encodeURIComponent(k));
      return r.ok ? { data: await r.json() } : {};
    } catch (e) { return { err: 'Ошибка загрузки: ' + (e && e.message ? e.message : e) }; }
  })();

  await wait(1400);                              // название по центру
  document.body.classList.remove('intro');       // уезжает наверх
  const res = await load;
  await wait(900);
  if (!res.data) return deny(res.err);
  try { sessionStorage.setItem('k', k); } catch (e) {}
  try { init(res.data); } catch (e) { deny('Ошибка: ' + (e && e.message ? e.message : e)); }
})();
