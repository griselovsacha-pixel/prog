const DUR = 650, GAP = 72, ROW = 58, PAD = 60, PANEL_W = 380;
const EASE = d3.easeCubicInOut;
const canHover = matchMedia('(hover:hover)').matches;
const $ = id => document.getElementById(id);

const svg = d3.select('#tree');
const g = svg.append('g');
const linkG = g.append('g');
const nodeG = g.append('g');
const ctx = document.createElement('canvas').getContext('2d');

let root, uid = 0, panelOpen = false, hoverTimer = null, first = true;
const layout = d3.tree().nodeSize([ROW, 1]);

const measure = t => {
  ctx.font = `700 20px ${getComputedStyle(document.body).fontFamily}`;
  return Math.ceil(ctx.measureText(t).width) + 6;
};

function elbow(sx, sy, tx, ty) {
  const dir = ty >= sy ? 1 : -1;
  const r = Math.min(16, Math.abs(ty - sy) / 2, (tx - sx) / 2);
  const mx = (sx + tx) / 2;
  return `M${sx},${sy}H${mx - r}Q${mx},${sy} ${mx},${sy + dir * r}V${ty - dir * r}Q${mx},${ty} ${mx + r},${ty}H${tx}`;
}

const isLeaf = d => !d.children && !d._children;
const open = d => { if (d._children) { d.children = d._children; d._children = null; } };
const closeDeep = d => {
  if (d.children) { d.children.forEach(closeDeep); d._children = d.children; d.children = null; }
  else if (d._children) d._children.forEach(closeDeep);
};
const stagger = d => (d.parent ? d.parent.children.indexOf(d) * 60 : 0);

function update(src) {
  layout(root);
  const nodes = root.descendants(), links = root.links();
  nodes.forEach(d => { d.py = d.x; d.px = d.parent ? d.parent.px + d.parent.w + GAP : 0; });

  const W = svg.node().clientWidth - (panelOpen && innerWidth > 760 ? PANEL_W : 0);
  const H = svg.node().clientHeight;
  const maxX = d3.max(nodes, d => d.px + d.w);
  const midY = (d3.min(nodes, d => d.py) + d3.max(nodes, d => d.py)) / 2;
  const tx = maxX + PAD * 2 <= W ? (W - maxX) / 2 : W - maxX - PAD;
  const ty = H / 2 - midY;
  (first ? g : g.transition().duration(DUR).ease(EASE)).attr('transform', `translate(${tx},${ty})`);

  // nodes
  const node = nodeG.selectAll('g.node').data(nodes, d => d.id);
  const entered = new Set();
  const enter = node.enter().append('g')
    .attr('class', 'node')
    .style('opacity', 0)
    .attr('transform', `translate(${src.ox},${src.oy})`)
    .each(d => entered.add(d.id))
    .on('mouseenter', (e, d) => {
      if (!canHover || isLeaf(d)) return;
      clearTimeout(hoverTimer);
      hoverTimer = setTimeout(() => expand(d), 90);
    })
    .on('mouseleave', () => clearTimeout(hoverTimer))
    .on('click', (e, d) => {
      e.stopPropagation();
      if (isLeaf(d)) return showPanel(d);
      if (!canHover && d.children) { closeDeep(d); return update(d); }
      expand(d);
    });
  enter.append('line').attr('class', 'ul').attr('x1', 0).attr('x2', d => d.w).attr('y1', 9).attr('y2', 9);
  enter.append('text').text(d => d.data.name);

  enter.merge(node).transition().duration(DUR).ease(EASE)
    .delay(d => (entered.has(d.id) ? stagger(d) : 0))
    .attr('transform', d => `translate(${d.px},${d.py})`)
    .style('opacity', 1);

  node.exit().transition().duration(DUR).ease(EASE)
    .attr('transform', `translate(${src.px},${src.py})`)
    .style('opacity', 0).remove();

  // links
  const sx = d => d.px + d.w, sy = d => d.py + 9;
  const link = linkG.selectAll('path.link').data(links, d => d.target.id);
  const start = elbow(src.ox + src.w, src.oy + 9, src.ox + src.w, src.oy + 9);

  link.enter().append('path').attr('class', 'link').attr('d', start)
    .merge(link).transition().duration(DUR).ease(EASE)
    .delay(d => (entered.has(d.target.id) ? stagger(d.target) : 0))
    .attr('d', d => elbow(sx(d.source), sy(d.source), d.target.px, sy(d.target)));

  link.exit().transition().duration(DUR).ease(EASE)
    .attr('d', elbow(src.px + src.w, src.py + 9, src.px + src.w, src.py + 9)).remove();

  nodes.forEach(d => { d.ox = d.px; d.oy = d.py; });
  first = false;
}

function expand(d) {
  if (d.children) return;
  if (d.parent) d.parent.children.forEach(s => { if (s !== d) closeDeep(s); });
  open(d);
  $('hint').classList.add('gone');
  update(d);
}

function showPanel(d) {
  const path = d.ancestors().reverse().slice(1, -1).map(n => n.data.name).join(' › ');
  $('crumb').textContent = path;
  $('ptitle').textContent = d.data.name;
  $('pdesc').textContent = d.data.desc || '';
  $('ptags').replaceChildren(...(d.data.tags || []).map(t => Object.assign(document.createElement('span'), { textContent: t })));
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
}

function init(data) {
  root = d3.hierarchy(data);
  root.each(d => { d.id = ++uid; d.w = measure(d.data.name); });
  closeDeep(root);
  root.ox = root.oy = 0;
  update(root);
}

$('close').onclick = () => togglePanel(false);
svg.on('click', () => panelOpen && togglePanel(false));
addEventListener('keydown', e => e.key === 'Escape' && panelOpen && togglePanel(false));
addEventListener('resize', () => root && update(root));

(async () => {
  const k = new URLSearchParams(location.search).get('k');
  if (!k) return deny();
  try {
    const r = await fetch('/api/tools?k=' + encodeURIComponent(k));
    if (!r.ok) return deny();
    init(await r.json());
  } catch { deny('Нет связи с сервером. Обновите страницу.'); }
})();
