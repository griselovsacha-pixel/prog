// Решает, показывать ли заставку с лупой. Подключается в <head> главной синхронно,
// чтобы класс успел встать до первой отрисовки (без мигания).
//
// Заставка играет при каждом заходе на основной сайт: по ссылке из бота, из закладок, с других сайтов,
// при обновлении страницы. Не играет, если пользователь вернулся на главную с внутренних страниц и
// поддоменов (about., /collab, /ai, /terms…): такие ссылки ведут на «/#nointro».
// Метка в адресе нужна потому, что сервер шлёт Referrer-Policy: no-referrer, а у поддомена
// свой sessionStorage — другого надёжного способа узнать, откуда пришёл человек, нет.
(function () {
  var d = document.documentElement, skip = false;
  if (/^#nointro$/.test(location.hash)) {
    skip = true;
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}   // метку из адреса убираем
  }
  try {
    // кнопка «Назад» в браузере с внутренней страницы: заставку в этой вкладке уже показывали
    var nav = performance.getEntriesByType('navigation')[0];
    if (nav && nav.type === 'back_forward' && sessionStorage.getItem('introSeen') === '1') skip = true;
  } catch (e) {}
  if (skip) d.classList.add('nointro');
})();
