"""Публичный слой для поисковиков: главная для гостей, /osint, /osint/category/<slug>, /osint/<slug>,
/sitemap.xml, /robots.txt.

Показываем только тизер инструмента (название, описание, цена, VPN, категория). Ссылки на ботов
и сайты остаются за персональным токеном — воронка через канал не меняется.
Страницы собираются из того же JSON, что и сайт (cat.body()), правки в админке видны сразу.

Скрытые ветки/инструменты: переменная SEO_HIDE (регулярка по категории верхнего уровня, названию и тегам).
"""
import hashlib
import html
import os 
import json
import re

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()
D = {"tools_json": lambda: None, "site": "", "channel": ""}


def configure(**kw):
    """tools_json() -> bytes|None, site (https://домен), channel (ссылка на канал)"""
    D.update(kw)


HIDE = re.compile(os.environ.get("SEO_HIDE", ""), re.I)

_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slugify(s: str) -> str:
    s = "".join(_TR.get(c, c) for c in s.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "tool"


PRICE = {"free": "бесплатно", "freemium": "есть бесплатные запросы", "paid": "платно"}
VPN = {"no": "работает без VPN", "yes": "нужен VPN"}
KIND = {"bot": "Telegram-бот", "site": "сайт", "bot+site": "бот и сайт", "bot+miniapp": "бот с мини-приложением"}

_c = {"h": None, "data": None}


def _uniq(base: str, used: set) -> str:
    slug, i = base, 2
    while slug in used:
        slug, i = f"{base}-{i}", i + 1
    used.add(slug)
    return slug


def build() -> dict | None:
    body = D["tools_json"]()
    if not body:
        return None
    h = hashlib.md5(body).hexdigest()
    if _c["h"] == h:
        return _c["data"]
    tree = json.loads(body)
    tools, links, slugs = [], set(), set()

    def walk(n, path):
        for c in n.get("children") or []:
            if not isinstance(c, dict):
                continue
            if isinstance(c.get("children"), list):
                walk(c, path + [str(c.get("name", ""))])
                continue
            name, link = str(c.get("name", "")).strip(), str(c.get("bot") or "").strip()
            if not name or not link or link in links or not path:
                continue
         
            links.add(link)
            tools.append({"slug": _uniq(slugify(name), slugs), "name": name, "cat": path,
                          "desc": str(c.get("desc") or ""), "type": c.get("type"), "price": c.get("price"),
                          "vpn": c.get("vpn"), "ptext": c.get("price_text"), "tags": c.get("tags") or []})

    walk(tree, [])
    cats, cslugs = {}, set()
    for t in tools:
        k = tuple(t["cat"])
        if k not in cats:
            cats[k] = {"slug": _uniq("cat-" + slugify(" ".join(k)), cslugs), "path": list(k), "tools": []}
        cats[k]["tools"].append(t)
    data = {"tools": tools, "by_slug": {t["slug"]: t for t in tools}, "cats": list(cats.values()),
            "cat_by_slug": {c["slug"]: c for c in cats.values()}}
    _c.update(h=h, data=data)
    return data


def e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def short(s: str, n: int = 155) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def ld(obj) -> str:
    return '<script type="application/ld+json">' + json.dumps(obj, ensure_ascii=False).replace("</", "<\\/") + "</script>"


def plural(n, f):
    a, b = n % 100, n % 10
    return f[2] if 10 < a < 20 else f[0] if b == 1 else f[1] if 1 < b < 5 else f[2]


CSS = """.wrap{max-width:760px;padding:84px 24px 60px;display:block}
.crumbs{font-size:13px;color:var(--mute);margin-bottom:14px}.crumbs a{color:var(--mute)}
.wrap h1{font-size:clamp(26px,4.4vw,36px);margin-bottom:14px}
.wrap h2{font-size:18px;color:var(--fg-h);margin:26px 0 10px}
.wrap p{font-size:16px;line-height:1.7;margin-bottom:12px}
.wrap ul{margin:0 0 12px;padding-left:20px}.wrap li{margin:4px 0;line-height:1.6}
.wrap a{color:var(--accent)}
.bd{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:2px 9px;font-size:12px;margin:0 6px 6px 0}
.wrap a.btn-tg{color:#fff;margin-top:6px}
.navl{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}"""

ICONS = ('<link rel="icon" href="/favicon.svg" type="image/svg+xml">\n'
         '<link rel="icon" href="/favicon.ico" sizes="48x48">\n'
         '<link rel="apple-touch-icon" href="/apple-touch-icon.png">')


def page(title, desc, path, body, extra="", bar=True) -> str:
    url = D["site"] + path
    barh = '<div class="bar"><a class="pill" href="/osint">← Каталог</a></div>' if bar else \
        '<div class="bar"><a class="pill" href="/about">О нас</a><a class="pill" href="/osint">Каталог</a></div>'
    return f"""<!doctype html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(url)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Harledy Project">
<meta property="og:locale" content="ru_RU">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(url)}">
<meta name="theme-color" content="#1a1a1a">
{ICONS}
<link rel="stylesheet" href="/pages.css?v=9">
<style>{CSS}</style>
{extra}
</head>
<body>
{barh}
<main class="wrap">
{body}
</main>
</body>
</html>"""


def cta() -> str:
    ch = e(D["channel"])
    return (f'<h2>Как открыть инструмент</h2><p>Ссылки на ботов и сайты, вечные зеркала, статус «работает ли сейчас» '
            f'и рейтинг доступны в полном каталоге. Доступ выдаёт Telegram-бот после заявки в канал проекта.</p>'
            f'<a class="btn-tg" href="{ch}" target="_blank" rel="noopener">Перейти в канал</a>')


def badges_of(t) -> str:
    b = [KIND.get(t["type"]), t["ptext"] or PRICE.get(t["price"]), VPN.get(t["vpn"])]
    return "".join(f'<span class="bd">{e(x)}</span>' for x in b if x)


# ---------------------------------------------------------------- главная для гостей
def render_home() -> str | None:
    d = build()
    if not d:
        return None
    n, cats = len(d["tools"]), d["cats"]
    top = []
    for c in cats:
        top.append(f'<li><a href="/osint/category/{e(c["slug"])}">{e(" › ".join(c["path"]))}</a> — '
                   f'{len(c["tools"])} {plural(len(c["tools"]), ["инструмент", "инструмента", "инструментов"])}</li>')
    pop = "".join(f'<li><a href="/osint/{e(t["slug"])}">{e(t["name"])}</a> — {e(short(t["desc"], 100))}</li>'
                  for t in d["tools"][:10])
    faq = [("Что такое Harledy Project?",
            "Это каталог OSINT-инструментов: Telegram-боты и сайты для поиска по открытым источникам, собранные по категориям."),
           ("Это бесплатно?", "Каталог бесплатный. У каждого инструмента указано, бесплатный он, платный или с бесплатными запросами."),
           ("Как получить доступ к ссылкам?", "Нужно запустить Telegram-бот проекта и подать заявку в канал. Бот выдаст персональную ссылку на полный каталог.")]
    body = ("<h1>Harledy Project — каталог OSINT-инструментов</h1>"
            f"<p>Сборник из {n} Telegram-ботов и сервисов для работы с открытыми источниками: поиск по нику, фото, "
            "почте и утечкам, метаданные файлов, геолокация по IP, анализ блокчейна. Для каждого инструмента указано, "
            "сколько он стоит и нужен ли VPN, а работоспособность проверяется автоматически раз в сутки.</p>"
            f'<div class="navl"><a class="btn-tg" href="{e(D["channel"])}" target="_blank" rel="noopener">Получить доступ</a></div>'
            "<h2>Разделы каталога</h2><ul>" + "".join(top) + "</ul>"
            "<h2>Популярные инструменты</h2><ul>" + pop + "</ul>"
            '<p><a href="/osint">Весь список инструментов</a> · <a href="/about">О проекте</a> · <a href="/collab">Сотрудничество</a></p>'
            "<h2>Частые вопросы</h2>" + "".join(f"<p><b>{e(q)}</b><br>{e(a)}</p>" for q, a in faq) +
            "<p>Уже есть персональная ссылка? Откройте её из сообщения бота — полный каталог загрузится там.</p>")
    site = D["site"]
    extra = ('<script src="/home-gate.js?v=1"></script>\n' + ld({
        "@context": "https://schema.org", "@type": "WebSite", "name": "Harledy Project", "url": site + "/", "inLanguage": "ru"}) +
        ld({"@context": "https://schema.org", "@type": "Organization", "name": "Harledy Project", "url": site + "/",
            "logo": site + "/favicon-192.png", "sameAs": [D["channel"]]}) +
        ld({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}))
    return page("Harledy Project — каталог OSINT-инструментов: боты и сервисы",
                "Каталог OSINT-инструментов на русском: Telegram-боты и сайты для поиска по нику, фото, почте, "
                "утечкам и метаданным. Цена, VPN и статус работы каждого.", "/", body, extra, bar=False)


# ---------------------------------------------------------------- каталог, категории, инструменты
def render_index() -> str | None:
    d = build()
    if not d:
        return None
    parts = ["<h1>Каталог OSINT-инструментов</h1>",
             f"<p>{len(d['tools'])} проверенных инструментов для открытых источников: поиск по нику, фото, "
             "почте и утечкам, метаданные, IP и блокчейн. Для каждого указано, бесплатный ли он и нужен ли VPN. "
             "Статус «работает сейчас» проверяется автоматически раз в сутки.</p>"]
    for c in d["cats"]:
        parts.append(f'<h2><a href="/osint/category/{e(c["slug"])}">{e(" › ".join(c["path"]))}</a></h2><ul>')
        for t in c["tools"]:
            parts.append(f'<li><a href="/osint/{e(t["slug"])}">{e(t["name"])}</a> — {e(short(t["desc"], 110))}</li>')
        parts.append("</ul>")
    parts.append(cta())
    return page("Каталог OSINT-инструментов: боты и сервисы — Harledy Project",
                "Каталог OSINT-инструментов на русском: поиск по нику, фото, почте, утечкам, метаданным и IP. "
                "Цена, VPN и статус работы каждого сервиса.", "/osint", "\n".join(parts))


def render_category(slug: str) -> str | None:
    d = build()
    c = d and d["cat_by_slug"].get(slug)
    if not c:
        return None
    name, full = c["path"][-1], " › ".join(c["path"])
    items = "".join(f'<li><a href="/osint/{e(t["slug"])}">{e(t["name"])}</a> — {e(short(t["desc"], 160))}<br>'
                    f'{badges_of(t)}</li>' for t in c["tools"])
    body = (f'<div class="crumbs"><a href="/osint">Каталог</a> › {e(full)}</div>'
            f'<h1>{e(name)}: инструменты и боты</h1>'
            f'<p>В разделе «{e(full)}» {len(c["tools"])} {plural(len(c["tools"]), ["инструмент", "инструмента", "инструментов"])}. '
            f'У каждого указаны цена и необходимость VPN.</p><ul>{items}</ul>{cta()}')
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Каталог", "item": D["site"] + "/osint"},
        {"@type": "ListItem", "position": 2, "name": name, "item": D["site"] + "/osint/category/" + slug}]}
    return page(f"{name}: инструменты и боты — Harledy Project",
                short(f"{name}: {', '.join(t['name'] for t in c['tools'])}. Цена, VPN и описание каждого инструмента."),
                "/osint/category/" + slug, body, ld(crumbs))


def render_tool(slug: str) -> str | None:
    d = build()
    t = d and d["by_slug"].get(slug)
    if not t:
        return None
    cat = " › ".join(t["cat"])
    rel = [x for x in d["tools"] if x["cat"] == t["cat"] and x["slug"] != slug][:6]
    rel_html = ""
    if rel:
        rel_html = "<h2>Похожие инструменты</h2><ul>" + "".join(
            f'<li><a href="/osint/{e(x["slug"])}">{e(x["name"])}</a></li>' for x in rel) + "</ul>"
    tags = ", ".join(str(x) for x in t["tags"])
    cslug = d["cats"][[c["path"] for c in d["cats"]].index(t["cat"])]["slug"]
    body = (f'<div class="crumbs"><a href="/osint">Каталог</a> › <a href="/osint/category/{e(cslug)}">{e(cat)}</a></div>'
            f'<h1>{e(t["name"])}</h1><div>{badges_of(t)}</div>'
            f'<p>{e(t["desc"])}</p>'
            f'<p>{e(t["name"])} — {e(KIND.get(t["type"], "инструмент"))} из раздела «{e(t["cat"][-1])}» каталога '
            f'Harledy Project.{(" Теги: " + e(tags) + ".") if tags else ""}</p>'
            f'{cta()}{rel_html}')
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Каталог", "item": D["site"] + "/osint"},
        {"@type": "ListItem", "position": 2, "name": t["name"], "item": D["site"] + "/osint/" + slug}]}
    app = {"@context": "https://schema.org", "@type": "SoftwareApplication", "name": t["name"],
           "description": short(t["desc"], 300), "applicationCategory": "UtilitiesApplication",
           "operatingSystem": "Web, Telegram", "inLanguage": "ru"}
    if t["price"] == "free":
        app["offers"] = {"@type": "Offer", "price": "0", "priceCurrency": "USD"}
    return page(f'{t["name"]} — {cat} | Harledy Project', short(t["desc"]) or t["name"], "/osint/" + slug, body,
                ld(crumbs) + ld(app))


def _html(h):
    return Response(h, media_type="text/html; charset=utf-8") if h else Response("not found", status_code=404, media_type="text/plain")


@router.get("/osint")
async def osint_index():
    return _html(render_index())


@router.get("/osint/category/{slug}")
async def osint_category(slug: str):
    return _html(render_category(slug) if re.fullmatch(r"[a-z0-9-]{1,120}", slug) else None)


@router.get("/osint/{slug}")
async def osint_tool(slug: str):
    return _html(render_tool(slug) if re.fullmatch(r"[a-z0-9-]{1,80}", slug) else None)


@router.get("/sitemap.xml")
async def sitemap():
    d = build() or {"tools": [], "cats": []}
    urls = ["/", "/osint", "/about", "/collab", "/terms"] + [f"/osint/category/{c['slug']}" for c in d["cats"]] \
        + [f"/osint/{t['slug']}" for t in d["tools"]]
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + "".join(f"<url><loc>{e(D['site'] + u)}</loc></url>\n" for u in urls) + "</urlset>")
    return Response(xml, media_type="application/xml")


@router.get("/robots.txt")
async def robots():
    return Response(f"User-agent: *\nDisallow: /admin\nDisallow: /api/\nDisallow: /app/\nAllow: /\n\nSitemap: {D['site']}/sitemap.xml\n",
                    media_type="text/plain")
