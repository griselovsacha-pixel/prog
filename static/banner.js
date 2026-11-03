// Рекламный баннер (левый нижний угол). Настраивается в админке → «Баннер».
// Токен доступа app.js уже положил в sessionStorage (?k=…); без него баннер не запрашивается.
(function () {
  var K = null;
  try { K = sessionStorage.getItem('k'); } catch (e) {}
  if (!K) return;
  var NS = 'http://www.w3.org/2000/svg';

  function safeUrl(u) {
    try { var x = new URL(u, location.href); return /^https?:$/.test(x.protocol) ? x.href : ''; } catch (e) { return ''; }
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;          // только textContent: HTML из админки в страницу не попадает
    return n;
  }
  function planet() {
    var s = document.createElementNS(NS, 'svg');
    s.setAttribute('viewBox', '0 0 64 64'); s.setAttribute('class', 'bn-ico'); s.setAttribute('aria-hidden', 'true');
    var c = document.createElementNS(NS, 'circle');
    c.setAttribute('cx', '34'); c.setAttribute('cy', '28'); c.setAttribute('r', '15'); c.setAttribute('fill', 'currentColor');
    var r = document.createElementNS(NS, 'path');
    r.setAttribute('d', 'M6 46c-3-6 7-17 24-24 14-6 27-6 29-1M58 20c3 6-7 17-24 24-12 5-22 6-27 3');
    r.setAttribute('fill', 'none'); r.setAttribute('stroke', 'currentColor'); r.setAttribute('stroke-width', '4'); r.setAttribute('stroke-linecap', 'round');
    s.appendChild(c); s.appendChild(r);
    return s;
  }

  fetch('/api/banner', { headers: { 'X-Access-Key': K } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (b) {
      if (!b || !b.on) return;
      var key = 'bn_closed', seen = null;
      try { seen = localStorage.getItem(key); } catch (e) {}
      if (seen === String(b.v)) return;                    // уже закрыт; новая версия баннера покажется снова
      var url = safeUrl(b.url);
      if (!url) return;

      var box = el('aside', 'bn');
      box.setAttribute('aria-label', 'Реклама');
      var x = el('button', 'bn-x', '\u00d7');
      x.type = 'button'; x.setAttribute('aria-label', 'Закрыть');
      x.onclick = function () {
        try { localStorage.setItem(key, String(b.v)); } catch (e) {}
        box.classList.remove('on');
        setTimeout(function () { box.remove(); }, 300);
      };
      var body = el('div', 'bn-body');
      body.appendChild(el('div', 'bn-t', b.title));
      if (b.text) body.appendChild(el('div', 'bn-d', b.text));
      var a = el('a', 'bn-b');
      a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer sponsored';
      a.appendChild(el('span', '', b.button));
      a.appendChild(el('i', '', '\u2197'));
      body.appendChild(a);
      box.appendChild(x);
      box.appendChild(body);
      // своя картинка главнее планеты; принимаем только data:image (png/jpeg/webp), как проверено и на сервере
      if (b.image && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+\/=]+$/.test(b.image)) {
        var im = document.createElement('img');
        im.className = 'bn-ico bn-img'; im.alt = ''; im.width = 54; im.height = 54; im.src = b.image;
        box.appendChild(im);
      } else if (b.icon) box.appendChild(planet());
      document.body.appendChild(box);
      setTimeout(function () { box.classList.add('on'); }, 2500);   // после заставки, не перебивает её
    })
    .catch(function () {});
})();
