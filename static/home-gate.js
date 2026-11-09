// Главная для гостей (без ?k=): если в этой вкладке уже есть токен, сразу открываем полный каталог.
(function () {
  try {
    var k = sessionStorage.getItem('k');
    if (k && k.indexOf('tma ') === 0) location.replace('/app/#nointro');      // вход из мини-аппа: назад в мини-апп
    else if (k && /^[\w-]{16,64}$/.test(k)) location.replace('/?k=' + encodeURIComponent(k) + location.hash);
  } catch (e) {}
})();
