(function () {
  var t = 'dark';
  try { t = localStorage.getItem('theme') || 'dark'; } catch (e) {}
  document.documentElement.dataset.theme = t;

  document.addEventListener('DOMContentLoaded', function () {
    var k = new URLSearchParams(location.search).get('k');
    try { if (k) sessionStorage.setItem('k', k); else k = sessionStorage.getItem('k'); } catch (e) {}
    var h = location.hostname, base = '';
    if (h.indexOf('about.') === 0) {              // если страница открыта на поддомене about.*
      base = location.protocol + '//' + h.slice(6) + (location.port ? ':' + location.port : '');
    }
    var back = document.getElementById('back');
    if (back) back.href = base + '/' + (k ? '?k=' + encodeURIComponent(k) : '');
  });
})();
