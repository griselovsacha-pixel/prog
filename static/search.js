/* Harledy Project — умный поиск по каталогу.
   Движок (без DOM) + выпадающий список. Работает на русском и английском:
   транслитерация (шерлок ↔ sherlock), опечатки, неверная раскладка (ghbdtn → привет),
   синонимы (сносерс → snosers, «проверить на валид» → ветка Check Valid), поиск по категориям.
   Ожидает от app.js: window.revealNode(node) и (необязательно) window.toolBadges(data). */
(function (G) {
  'use strict';

  /* ---------------------------------------------------------------- нормализация */
  const RU = { а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ж: 'zh', з: 'z', и: 'i', й: 'i', к: 'k', л: 'l', м: 'm',
    н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'sh',
    ъ: '', ы: 'i', ь: '', э: 'e', ю: 'iu', я: 'ia' };
  // окончания, которые отбрасываем у русских слов (самое длинное совпадение; остаток ≥ 3 букв)
  const RU_SUF = ('иями ями ами ого его ому ему ыми ими ией ать ять ить еть уть ают яют ует уют ает ых их ов ев ей ий ый ой ' +
    'ая яя ое ее ие ые ом ем ах ях ию ью ия ья ам ям ую юю ть ся у ю а я о е ы и ь й').split(' ')
    .sort((a, b) => b.length - a.length);
  const stemRu = w => {
    for (const s of RU_SUF) if (w.length - s.length >= 3 && w.endsWith(s)) return w.slice(0, -s.length);
    return w;
  };
  const reduceLat = s => s
    .replace(/shch|sch/g, 'sh').replace(/ph/g, 'f').replace(/ck/g, 'k').replace(/kh/g, 'h')
    .replace(/x/g, 'ks').replace(/q/g, 'k').replace(/w/g, 'v')
    .replace(/c(?!h)(?=[eiy])/g, 's').replace(/c(?!h)/g, 'k')
    .replace(/y/g, 'i').replace(/(.)\1+/g, '$1');
  // «фонетический ключ»: русское и английское написание одного слова дают один и тот же ключ
  function key(w) {
    w = String(w).toLowerCase().replace(/ё/g, 'е');
    let s = '';
    if (/[а-я]/.test(w)) { w = stemRu(w); for (const ch of w) s += RU[ch] !== undefined ? RU[ch] : ch; } else s = w;
    if (s.length > 3 && s.endsWith('s') && !s.endsWith('ss')) s = s.slice(0, -1);   // snosers → snoser
    return reduceLat(s);
  }
  const tokens = s => String(s == null ? '' : s).toLowerCase().replace(/ё/g, 'е').split(/[^a-zа-я0-9]+/).filter(Boolean);

  // раскладка клавиатуры: ghbdtn ↔ привет
  const L2R = { q: 'й', w: 'ц', e: 'у', r: 'к', t: 'е', y: 'н', u: 'г', i: 'ш', o: 'щ', p: 'з', '[': 'х', ']': 'ъ', a: 'ф',
    s: 'ы', d: 'в', f: 'а', g: 'п', h: 'р', j: 'о', k: 'л', l: 'д', ';': 'ж', "'": 'э', z: 'я', x: 'ч', c: 'с', v: 'м',
    b: 'и', n: 'т', m: 'ь', ',': 'б', '.': 'ю', '`': 'ё' };
  const R2L = {}; for (const k in L2R) R2L[L2R[k]] = k;
  function swapRaw(raw) {
    const s = String(raw).toLowerCase(), lat = /[a-z]/.test(s), cyr = /[а-яё]/.test(s);
    if (lat === cyr) return '';
    const map = lat ? L2R : R2L;
    let out = ''; for (const ch of s) out += map[ch] !== undefined ? map[ch] : ch;
    return out;
  }

  // расстояние Дамерау — Левенштейна (с ранним выходом)
  function osa(a, b, max) {
    const n = a.length, m = b.length;
    if (Math.abs(n - m) > max) return max + 1;
    let p2 = null, p1 = Array.from({ length: m + 1 }, (_, j) => j);
    for (let i = 1; i <= n; i++) {
      const cur = [i]; let rowMin = i;
      for (let j = 1; j <= m; j++) {
        const c = a.charCodeAt(i - 1) === b.charCodeAt(j - 1) ? 0 : 1;
        let v = Math.min(p1[j] + 1, cur[j - 1] + 1, p1[j - 1] + c);
        if (p2 && i > 1 && j > 1 && a.charCodeAt(i - 1) === b.charCodeAt(j - 2) && a.charCodeAt(i - 2) === b.charCodeAt(j - 1)) v = Math.min(v, p2[j - 2] + 1);
        cur[j] = v; if (v < rowMin) rowMin = v;
      }
      if (rowMin > max) return max + 1;
      p2 = p1; p1 = cur;
    }
    return p1[m];
  }

  // качество совпадения слова запроса q со словом из каталога w (оба — ключи), 0…1
  function Q(q, w, last, fz) {
    if (q === w) return 1;
    if (w.startsWith(q)) return (q.length >= 2 || last) ? 0.88 : 0;            // автодополнение
    const lq = q.length, lw = w.length;
    if (lq >= 3 && lw >= 3 && Math.max(lq, lw) >= 4) {                          // другая форма слова
      let cp = 0; const m = Math.min(lq, lw);
      while (cp < m && q.charCodeAt(cp) === w.charCodeAt(cp)) cp++;
      if (cp >= 3 && cp >= m - 1 && Math.abs(lq - lw) <= 3) return 0.72;
    }
    if (lq >= 4 && w.includes(q)) return 0.5;
    if (fz && lq >= 4) { const md = lq >= 7 ? 2 : 1; if (Math.abs(lq - lw) <= md && osa(q, w, md) <= md) return 0.55; }   // опечатки
    return 0;
  }

  /* ---------------------------------------------------------------- стоп-слова и синонимы */
  const STOP = new Set(('и в во на по к с со у о об от до из за для про при над под как какой какая какие какое что это мне мой моя мои ' +
    'нам нас вам ваш наш где есть ли бы же то да нет или либо если чтобы чтоб можно нужно нужен нужна нужны хочу хотел ищу искать найти ' +
    'работает работают работать найди найдите покажи покажите дай дайте подскажи посоветуй подберите подбери помоги инструмент инструменты инструментов инструменту ' +
    'the a an of for to in on at and or is are be how what which can i me my find show give want need looking look please with from by do does tool tools').split(' '));

  const SYN = [
    'снос сносер сносеры снести снесть жалоба жалобы репорт донос snos snoser report ban бан забанить',
    'бомбер бомберы бомбить бомбинг bomber bombers спам spam смс sms',
    'пранк пранки prank розыгрыш разыграть шутка звонок звонки call calls',
    'логгер логер logger грабер grabber трекер tracker',
    'ip айпи ип геолокация геолокации геопозиция geo geolocation location координаты карта карты map maps',
    'бесплатно бесплатный бесплатные бесплатных халява халявный free даром gratis freemium',
    'платно платный платные платных paid premium премиум подписка subscription',
    'регистрация регистрацию register registration signup зарегистрироваться',
    'фото фотка фотки фотография фотографии картинка картинки изображение изображения лицо лица face photo image picture',
    'номер номера телефон телефона мобильный мобильник phone number mobile hlr хлр',
    'почта почты почту email mail емейл имейл имеил мыло',
    'ник никнейм username nickname nick юзернейм юзер',
    'вк vk вконтакте vkontakte',
    'крипта крипто криптовалюта crypto cryptocurrency blockchain блокчейн ton тон кошелек кошелька wallet aml',
    'утечка утечки слив сливы leak leaks leaked breach pwned база базы бд database dump дамп',
    'метаданные метаданных exif эксиф metadata мета',
    'проверка проверить проверь проверять check checker чекер валид валидность валидный valid validate validation верифицировать verify',
    'osint осинт разведка пробив пробить деанон lookup',
    'авто машина машины автомобиль car vin госномер',
    'фио имя фамилия name fullname человек людей люди person people',
    'паспорт passport инн снилс огрн документ документы document documents docs',
    'телеграм telegram тг tg телега',
    'бот боты bot bots',
    'сайт сайты site sites website веб web браузер browser',
    'мини приложение miniapp webapp',
    'api апи ключ токен token',
    'анализ analysis analyze аналитика статистика stats statistics',
    'россия рф russia',
    'безопасность security защита privacy приватность анонимность anonymity',
  ].map(s => ({ keys: [...new Set(s.split(/\s+/).map(key))] }));

  /* ---------------------------------------------------------------- индекс */
  let INDEX = [], ROOT_CATS = [];
  const uniq = a => [...new Set(a)];
  function keySet(texts) {
    const out = new Set();
    for (const t of texts) {
      const tk = tokens(t);
      tk.forEach(x => out.add(key(x)));
      if (tk.length > 1) out.add(key(tk.join('')));             // «have i been pwned» → haveibeenpwned
    }
    return out;
  }
  const F = (f, w, texts) => ({ f, w, k: keySet(texts) });

  // текст бирок для поиска («бесплатно», «без vpn», «веб-версия» …)
  function badgeText(t) {
    const m = [];
    const type = { bot: 'бот bot', site: 'сайт site веб web', 'bot+site': 'бот bot сайт site веб web', 'bot+miniapp': 'бот bot мини приложение miniapp' };
    m.push(type[t.type] || '');
    const price = { free: 'бесплатно free', freemium: 'бесплатные запросы бесплатно free', paid: 'платно paid', unknown: '' };
    m.push(price[t.price] || '', t.price_text || '');
    const vpn = { no: 'без vpn работает без vpn не нужен vpn', yes: 'нужен vpn требует vpn', unknown: '' };
    m.push(vpn[t.vpn] || '', t.vpn_text || '');
    if (t.web === true) m.push('веб версия web есть веб версия');
    (Array.isArray(t.extra) ? t.extra : []).forEach(x => x && m.push(x.t || ''));
    if (t.note) m.push(t.note);
    return m.join(' ');
  }

  function build(rootNode) {
    INDEX = []; ROOT_CATS = [];
    let order = 0;
    const all = rootNode.descendants().filter(d => d.parent);          // без самого корня «Harledy Project»
    const isCatNode = d => !!(d.children && d.children.length);
    const e0 = new Map();
    for (const d of all) {
      const cat = isCatNode(d), data = d.data;
      const anc = d.ancestors().reverse().slice(1, -1);                // от верхней категории до родителя
      const e = { d, data, isCat: cat, name: data.name, order: order++, crumb: anc.map(n => n.data.name), nameNorm: tokens(data.name).join(' '), leaves: [], fields: [] };
      e0.set(d, e); INDEX.push(e);
      if (cat && !d.parent.parent) ROOT_CATS.push(e);
    }
    for (const e of INDEX) {
      const d = e.d, data = e.data, ancNames = e.crumb;
      if (!e.isCat) {
        e.fields = [F('name', 3.2, [data.name]), F('aka', 3.0, data.aka || []), F('tag', 2.2, data.tags || []),
          F('anc', 1.8, ancNames), F('badge', 1.2, [badgeText(data)]), F('desc', 0.9, [data.desc || ''])];
      } else {
        const desc = d.descendants().filter(x => x !== d);
        const leaves = desc.filter(x => !isCatNode(x)).map(x => e0.get(x));
        const subs = desc.filter(isCatNode).map(x => x.data.name);
        e.leaves = leaves;
        e.fields = [F('name', 3.0, [data.name]), F('sub', 2.0, subs), F('leaf', 1.3, leaves.map(l => l.name)),
          F('tag', 1.1, leaves.flatMap(l => (l.data.tags || []).concat(l.data.aka || []))), F('anc', 1.6, ancNames)];
      }
    }
    return INDEX.length;
  }

  /* ---------------------------------------------------------------- запрос */
  function buildUnits(raw, tier) {
    const toks = tokens(raw);
    const sw = tier >= 3 ? tokens(swapRaw(raw)) : [];
    const stopAt = i => STOP.has(toks[i]) || (sw.length === toks.length && STOP.has(sw[i]));
    let idx = toks.map((_, i) => i).filter(i => !stopAt(i));
    if (!idx.length) idx = toks.map((_, i) => i);
    return idx.slice(0, 8).map(i => {
      const t = toks[i], last = i === toks.length - 1, k = key(t);
      const alts = new Map();
      const add = (kk, wt, l, fz, nd) => { const o = alts.get(kk); if (!o || o.wt < wt) alts.set(kk, { k: kk, wt, last: l, fz, nd: !!nd, m: new Map() }); };
      add(k, 1, last, tier >= 2);
      for (const g of SYN) {                                                       // синонимы
        if (g.keys.some(m => Q(k, m, last, tier >= 2) >= (tier >= 2 ? 0.55 : 0.85))) g.keys.forEach(m => m !== k && add(m, 0.75, false, false));
      }
      if (sw.length) (sw.length === toks.length ? [sw[i]] : sw).forEach(s => s && add(key(s), 0.85, last, true, true));   // раскладка
      return { t, k, alts: [...alts.values()] };
    });
  }

  const wkTag = '~', wkPlain = '=';
  function scoreEntry(e, units) {
    let total = 0, matched = 0, nameHit = false;
    for (const u of units) {
      let best = 0, bf = '';
      for (const a of u.alts) {
        for (const f of e.fields) {
          if (a.nd && (f.f === 'desc' || f.f === 'badge')) continue;
          const ub = f.w * a.wt; if (ub <= best) continue;
          let q = 0;
          const fz = a.fz && f.f !== 'desc' && f.f !== 'badge', mk = fz ? wkTag : wkPlain;
          for (const wk of f.k) {
            let v = a.m.get(mk + wk);
            if (v === undefined) { v = Q(a.k, wk, a.last, fz); a.m.set(mk + wk, v); }
            if (v > q) { q = v; if (q === 1) break; }
          }
          const s = ub * q; if (s > best) { best = s; bf = f.f; }
        }
      }
      if (best > 0) { matched++; total += best; if (bf === 'name') nameHit = true; }
    }
    if (matched === units.length && units.length) {
      const qn = units.map(u => u.t).join(' ');
      if (e.nameNorm === qn) total += 8; else if (e.nameNorm.startsWith(qn)) total += 3;
    }
    return { e, total, matched, nameHit };
  }

  const byName = list => { const s = new Set(); return list.filter(l => !s.has(l.name) && s.add(l.name)); };

  const FREE = new Set('бесплатно бесплатный бесплатные бесплатных бесплатная бесплатное халява халявный даром free'.split(' '));
  const PAID = new Set('платно платный платные платных платная платное paid premium премиум'.split(' '));
  const NEG = new Set('без without no not'.split(' ')), NEED = new Set('нужен нужно нужна нужны требует требуется need needs require requires with'.split(' '));
  const isVpn = t => t === 'vpn' || t === 'впн';
  function parseIntent(toks) {
    const rest = [], f = {};
    for (let i = 0; i < toks.length; i++) {
      const t = toks[i], n = toks[i + 1];
      if (n && isVpn(n) && NEG.has(t)) { f.vpn = 'no'; i++; }
      else if (n && isVpn(n) && NEED.has(t)) { f.vpn = 'yes'; i++; }
      else if (FREE.has(t)) f.price = 'free';
      else if (PAID.has(t)) f.price = 'paid';
      else rest.push(t);
    }
    return { rest, filt: Object.keys(f).length ? f : null };
  }
  const passes = (e, f) => !f || ((!f.vpn || e.data.vpn === f.vpn) &&
    (!f.price || (f.price === 'free' ? (e.data.price === 'free' || e.data.price === 'freemium') : e.data.price === 'paid')));
  const filtLabel = f => [f.vpn === 'no' ? 'без VPN' : f.vpn === 'yes' ? 'нужен VPN' : '', f.price === 'free' ? 'бесплатные' : f.price === 'paid' ? 'платные' : ''].filter(Boolean).join(' · ');

  function compose(units, scored, partial, filt) {
    const sortFn = (a, b) => b.matched - a.matched || b.total - a.total || a.e.order - b.e.order;
    const toolHits = scored.filter(s => !s.e.isCat).sort(sortFn);
    const hitSet = new Set(toolHits.map(s => s.e)), score = new Map(toolHits.map(s => [s.e, s]));
    const blocks = [];
    for (const c of scored.filter(s => s.e.isCat && s.nameHit).sort(sortFn)) {
      if (blocks.length >= 3) break;
      const matchedLeaves = byName(c.e.leaves.filter(l => hitSet.has(l)));
      const names = new Set(matchedLeaves.map(l => l.name));
      if (filt && !names.size) continue;                                           // под фильтр ничего не подходит
      if (names.size && blocks.some(b => [...names].every(n => b.names.has(n)))) continue;   // это же содержимое уже показано выше
      matchedLeaves.sort((a, b) => score.get(b).total - score.get(a).total || a.order - b.order);
      const rest = filt ? [] : byName(c.e.leaves).filter(l => !names.has(l.name));
      const all = matchedLeaves.concat(rest);
      blocks.push({ e: c.e, names, chips: all.slice(0, 8), total: all.length, more: Math.max(0, all.length - 8) });
    }
    const shown = new Set(blocks.flatMap(b => b.chips.map(c => c.name)));
    const top = toolHits.length ? toolHits[0].total : 0;
    const cand = toolHits.filter(s => s.total >= top * 0.5 && !shown.has(s.e.name));   // слабые случайные совпадения в описании отсекаем
    return { blocks, tools: cand.slice(0, 8).map(s => s.e), moreTools: Math.max(0, cand.length - 8), partial, empty: !blocks.length && !cand.length };
  }

  function query(raw) {
    const { rest, filt } = parseIntent(tokens(raw));
    const label = filt ? filtLabel(filt) : '';
    if (!rest.length) {
      if (!filt) return { idle: true, blocks: [], tools: [], hl: [] };
      // только фильтр («без vpn», «бесплатно»): показываем подходящие инструменты, сначала полностью бесплатные
      const rank = e => (e.data.price === 'free' ? 2 : e.data.price === 'freemium' ? 1 : 0);
      const list = INDEX.filter(e => !e.isCat && passes(e, filt)).sort((a, b) => (filt.price === 'free' ? rank(b) - rank(a) : 0) || a.order - b.order);
      return { blocks: [], tools: list.slice(0, 12), moreTools: Math.max(0, list.length - 12), total: list.length, filter: label, empty: !list.length, hl: [] };
    }
    const text = rest.join(' ');
    const keep = s => (s.e.isCat ? (!filt || s.e.leaves.some(l => passes(l, filt))) : passes(s.e, filt));
    for (const tier of [1, 2, 3]) {
      const units = buildUnits(text, tier), need = units.length;
      const scored = INDEX.map(e => scoreEntry(e, units)).filter(s => s.matched === need && keep(s));
      if (scored.length) return Object.assign(compose(units, scored, false, filt), { tier, filter: label, hl: hlKeys(units) });
    }
    if (rest.length > 1) {                                                          // «похожее»: совпала хотя бы часть слов
      const units = buildUnits(text, 3);
      const scored = INDEX.map(e => scoreEntry(e, units)).filter(s => s.matched > 0 && keep(s));
      if (scored.length) return Object.assign(compose(units, scored, true, filt), { tier: 4, filter: label, hl: hlKeys(units) });
    }
    return { empty: true, blocks: [], tools: [], hl: [], filter: label };
  }
  const hlKeys = units => uniq(units.flatMap(u => u.alts.filter(a => a.wt >= 0.85).map(a => a.k)));

  const API = { build, query, key, tokens, rootCats: () => ROOT_CATS.map(e => e), Q, swapRaw };
  G.HSearch = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
  if (typeof document === 'undefined') return;

  /* ---------------------------------------------------------------- интерфейс */
  const $ = id => document.getElementById(id);
  const plural = (n, f) => { const a = n % 100, b = n % 10; return a > 10 && a < 20 ? f[2] : b === 1 ? f[0] : b > 1 && b < 5 ? f[1] : f[2]; };
  const el = (tag, cls, text) => { const x = document.createElement(tag); if (cls) x.className = cls; if (text != null) x.textContent = text; return x; };
  const ICON = {
    folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
    dot: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="5" fill="currentColor"/></svg>',
  };
  const icon = name => { const s = el('span', 'sic'); s.innerHTML = ICON[name]; return s; };      // статичная иконка, без данных каталога

  let box, input, clear, res, items = [], active = -1, lastHl = [], opened = false;

  function highlight(node, text, hl) {
    if (!hl || !hl.length) { node.textContent = text; return; }
    const re = /[A-Za-zА-Яа-яЁё0-9]+/g; let last = 0, m;
    while ((m = re.exec(text))) {
      const wk = key(m[0]);
      if (!hl.some(q => Q(q, wk, true, false) >= 0.72)) continue;
      if (m.index > last) node.append(text.slice(last, m.index));
      node.append(el('mark', '', m[0])); last = m.index + m[0].length;
    }
    node.append(text.slice(last));
  }

  function addItem(node, entry) {
    const i = items.length; items.push({ node, entry });
    node.addEventListener('click', () => choose(entry));
    node.addEventListener('mouseenter', () => setActive(i, false));
  }
  function setActive(i, scroll) {
    if (items[active]) items[active].node.classList.remove('on');
    active = i;
    if (items[i]) { items[i].node.classList.add('on'); if (scroll) items[i].node.scrollIntoView({ block: 'nearest' }); }
  }
  function openList(on) {
    opened = on; res.hidden = !on; input.setAttribute('aria-expanded', String(on));
  }
  function choose(entry) {
    openList(false); input.blur();
    if (typeof G.revealNode === 'function') G.revealNode(entry.d);
  }

  function badgesRow(data) {
    const row = el('span', 'sb');
    if (typeof G.toolBadges === 'function') {
      G.toolBadges(data).filter(b => b[0] !== 'type' && b[0] !== 'neutral').slice(0, 3)
        .forEach(([k, t]) => row.append(Object.assign(el('span', 'bd ' + k), { textContent: t })));
    }
    return row;
  }

  function chipsOf(list, hl, cls) {
    const wrap = el('div', 'chips');
    list.forEach(e => {
      const c = el('button', 'chip' + (cls ? ' ' + cls : '')); c.type = 'button';
      highlight(c, e.name, hl); addItem(c, e); wrap.append(c);
    });
    return wrap;
  }
  const label = t => el('div', 'sgrp', t);
  const note = t => el('div', 'snote', t);

  function renderIdle() {
    items = []; active = -1; res.replaceChildren();
    res.append(label('Разделы каталога'));
    res.append(chipsOf(ROOT_CATS, null, 'cat'));
    res.append(note('Пишите по-русски или по-английски: «снос», «проверить на валид», «фото», «sherlock», «без vpn».'));
  }

  function render() {
    const raw = input.value; clear.hidden = !raw;
    if (!raw.trim()) { renderIdle(); return; }
    const r = query(raw);
    items = []; active = -1; res.replaceChildren(); lastHl = r.hl || [];
    if (r.empty) {
      res.append(note('Ничего не найдено по запросу «' + raw.trim() + '». Попробуйте другое слово — например, «фото», «ВК», «снос», «бесплатно».'));
      res.append(label('Разделы каталога')); res.append(chipsOf(ROOT_CATS, null, 'cat'));
      return;
    }
    if (r.partial) res.append(note('Точных совпадений нет. Возможно, подойдёт:'));
    if (r.filter) res.append(label('Фильтр: ' + r.filter + (r.total ? ' · ' + r.total : '')));
    if (r.blocks.length) {
      if (r.tools.length) res.append(label('Ветки каталога'));
      r.blocks.forEach(b => {
        const wrap = el('div', 'sblock');
        const head = el('button', 'si cat'); head.type = 'button';
        const nm = el('span', 'sn'); highlight(nm, b.e.name, r.hl);
        const cnt = b.total + ' ' + plural(b.total, ['инструмент', 'инструмента', 'инструментов']);
        const meta = el('span', 'sc', (b.e.crumb.length ? b.e.crumb.join(' › ') + ' · ' : '') + cnt);
        head.append(icon('folder'), nm, meta); addItem(head, b.e); wrap.append(head);
        const chips = chipsOf(b.chips, r.hl);
        if (b.more) chips.append(el('span', 'more', '+' + b.more + ' ещё — откройте ветку'));
        wrap.append(chips); res.append(wrap);
      });
    }
    if (r.tools.length) {
      if (r.blocks.length) res.append(label('Инструменты'));
      r.tools.forEach(e => {
        const row = el('button', 'si tool'); row.type = 'button';
        const nm = el('span', 'sn'); highlight(nm, e.name, r.hl);
        row.append(icon('dot'), nm, el('span', 'sc', e.crumb.join(' › ')), badgesRow(e.data));
        addItem(row, e); res.append(row);
      });
      if (r.moreTools) res.append(note('…и ещё ' + r.moreTools + '. Добавьте слово к запросу, чтобы сузить выдачу.'));
    }
  }

  function show() {                      // вызывает app.js, когда дерево готово
    box = $('search'); if (!box) return;
    box.hidden = false;
  }
  API.show = show;

  function initUI() {
    box = $('search'); input = $('q'); clear = $('qclear'); res = $('sres');
    if (!box || !input) return;
    input.addEventListener('input', () => { render(); openList(true); });
    input.addEventListener('focus', () => { render(); openList(true); });
    input.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!opened) { render(); openList(true); }
        if (!items.length) return;
        const n = items.length;
        setActive(e.key === 'ArrowDown' ? (active + 1) % n : (active - 1 + n) % n, true);
      } else if (e.key === 'Enter') {
        const it = items[active >= 0 ? active : 0];
        if (it) { e.preventDefault(); choose(it.entry); }
      } else if (e.key === 'Escape') {
        if (opened) openList(false);
        else if (input.value) { input.value = ''; clear.hidden = true; if (G.clearFound) G.clearFound(); }
      }
    });
    clear.addEventListener('click', () => {
      input.value = ''; clear.hidden = true; if (G.clearFound) G.clearFound(); input.focus();
    });
    document.addEventListener('pointerdown', e => { if (opened && !box.contains(e.target)) openList(false); });
    document.addEventListener('keydown', e => {
      if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !/^(input|textarea|select)$/i.test(e.target.tagName || '')) { e.preventDefault(); input.focus(); }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initUI); else initUI();
})(typeof window !== 'undefined' ? window : globalThis);
