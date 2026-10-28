// Проверка умного поиска: node tests/search_smoke.js
const fs = require('fs'), path = require('path');
const S = require('../static/search.js');
const data = JSON.parse(fs.readFileSync(path.join(__dirname, '../data/tools.json'), 'utf8'));
const prune = n => Array.isArray(n.children) ? { ...n, children: n.children.map(prune).filter(c => !Array.isArray(c.children) || c.children.length) } : n;
function hier(d, p) {
  const n = { data: d, parent: p || null, children: null };
  if (d.children && d.children.length) n.children = d.children.map(c => hier(c, n));
  n.ancestors = function () { const a = []; for (let x = this; x; x = x.parent) a.push(x); return a; };
  n.descendants = function () { const o = [this]; (this.children || []).forEach(c => o.push(...c.descendants())); return o; };
  return n;
}
S.build(hier(prune(data)));

// [запрос, что должно быть в блоках-ветках, что должно быть среди инструментов/чипов]
const CASES = [
  ['проверить на валид',        { cat: 'Check Valid' }],
  ['ппроверить на валид',       { cat: 'Check Valid' }],
  ['check valid',               { cat: 'Check Valid' }],
  ['сносерс',                   { cat: 'Snosers', tool: 'Ember Snos' }],
  ['snosers',                   { cat: 'Snosers' }],
  ['шерлок',                    { tool: 'Sherlock' }],
  ['sherlok',                   { tool: 'Sherlock' }],
  ['глаз бога',                 { tool: 'Void' }],
  ['pimeyes',                   { tool: 'PimEyes' }],
  ['pimeys',                    { tool: 'PimEyes' }],
  ['поиск по фото',             { cat: 'Поиск по фото', tool: 'TinEye' }],
  ['фото',                      { tool: 'PimEyes' }],
  ['бомбер',                    { tool: 'Черный Крест' }],
  ['пранк',                     { tool: 'Prank Bot' }],
  ['логгер',                    { cat: 'Loggers', tool: 'Rendy Logger' }],
  ['loggers',                   { cat: 'Loggers' }],
  ['вк',                        { cat: 'Поиск по ВК', tool: '220VK' }],
  ['vk',                        { cat: 'Поиск по ВК' }],
  ['ton кошелек',               { cat: 'Поиск по TON кошельку', tool: 'TONSCAN' }],
  ['крипта',                    { tool: 'TONSCAN' }],
  ['утечки',                    { tool: 'Have I Been Pwned' }],
  ['метаданные',                { tool: 'Exif-Ксия' }],
  ['exif',                      { tool: 'Exif-Ксия' }],
  ['без vpn',                   { tool: 'Sherlock' }],
  ['нужен vpn',                 { tool: 'Leak Osint', only: ['Leak Osint','Search Faces','Yandex Images','smsc.ru'] }],
  ['бесплатно',                 { tool: 'Maigret' }],
  ['бесплатные боты для поиска по нику', { tool: 'Sherlock' }],
  ['работает без vpn',          { tool: 'Sherlock' }],
  ['платно',                    { tool: 'DOZOR' }],
  ['требует регистрацию',       { tool: 'Epieos' }],
  ['api ключ',                  { tool: 'Vector' }],
  ['мини приложение',           { tool: 'Onux' }],
  ['какие инструменты есть для поиска данных', { cat: 'Поиск данных' }],
  ['ghbdtn',                    {}],            // «привет»: ничего нет, но не должно падать
  ['сайт для поиска по нику',   { tool: 'WhatsMyName' }],
  ['дух',                       { tool: 'Духless' }],
  ['dukhless',                  { tool: 'Духless' }],
  ['максонов',                  { tool: 'Максонов search' }],
  ['hlr',                       { tool: 'smsc.ru' }],
  ['ghjdthm yf dfkbl',          { cat: 'Check Valid' }],   // «проверь на валид» в английской раскладке
  ['[frth',                     {}],
  ['почта',                     { tool: 'Have I Been Pwned' }],
  ['email',                     { tool: 'Epieos' }],
  ['qwertyuiop',                { none: true }],
];
let bad = 0;
for (const [q, want] of CASES) {
  const r = S.query(q);
  const cats = r.blocks.map(b => b.e.name), chips = r.blocks.flatMap(b => b.chips.map(c => c.name)), tools = r.tools.map(t => t.name);
  const have = new Set([...chips, ...tools]);
  let ok = true;
  if (want.cat && !cats.includes(want.cat)) ok = false;
  if (want.tool && !have.has(want.tool)) ok = false;
  if (want.none && !r.empty) ok = false;
  if (want.only && [...have].some(n => !want.only.includes(n))) ok = false;
  if (!ok) bad++;
  console.log((ok ? 'OK  ' : 'FAIL') + ' ' + JSON.stringify(q) + ' [tier ' + (r.tier || '-') + (r.partial ? ', partial' : '') + '] ветки: ' + (cats.join(' | ') || '—') + ' ; инструменты: ' + (have.size ? [...have].slice(0, 6).join(', ') : '—'));
}
console.log(bad ? `\n${bad} провалено` : '\nвсё ок');
process.exit(bad ? 1 : 0);
