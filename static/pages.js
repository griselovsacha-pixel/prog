// Общее для внутренних страниц (О нас, Сотрудничество, AI). Соглашение живёт только на главном домене.
(function () {
  // тема — как выбрана на главной (подключено в <head>, чтобы не мигало)
  try {
    var t = localStorage.getItem('theme');
    if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  } catch (e) {}

  function init() {
    // Ссылка на главную. С поддомена (about., admin.) ведём на основной домен.
    // Метка #nointro: возврат с внутренней страницы не должен снова запускать заставку с лупой
    // (подробности — в intro-gate.js).
    var links = document.querySelectorAll('[data-home]');
    var host = location.hostname.replace(/^(about|admin)\./, '');
    var href = location.protocol + '//' + host + (location.port ? ':' + location.port : '') + '/#nointro';
    for (var i = 0; i < links.length; i++) links[i].href = href;
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
