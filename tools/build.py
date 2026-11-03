"""Сборка статического сайта Witte-rus из структурированных данных.

Запуск:  python3 tools/build.py
Исходные данные: data/catalog.json, data/products/*.json, data/company.json, data/documents.json
Результат: каталог site/ (HTML-страницы, sitemap.xml, robots.txt, поисковый индекс).
"""
import copy
import datetime
import glob
import html
import json
import os
import re
import shutil
import urllib.parse
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SITE = os.path.join(ROOT, "site")
IMG_DIR = os.path.join(SITE, "assets", "img", "p")

try:
    from PIL import Image
except ImportError:  # размеры изображений необязательны
    Image = None

E = html.escape
WARN = []


def warn(msg):
    WARN.append(msg)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- данные
catalog = load(os.path.join(DATA, "catalog.json"))
company = load(os.path.join(DATA, "company.json"))
documents = {d["id"]: d for d in load(os.path.join(DATA, "documents.json"))["items"]}

raw = []
for path in sorted(glob.glob(os.path.join(DATA, "products", "*.json"))):
    raw.extend(load(path))

products = {}
series_of = {}


def add_product(p):
    if p["id"] in products:
        warn(f"Повтор id товара: {p['id']}")
    products[p["id"]] = p


for entry in raw:
    if "series" not in entry:
        add_product(entry)
        continue
    base = entry["base"]
    ids = [it["id"] for it in entry["items"]]
    for idx, it in enumerate(entry["items"]):
        p = copy.deepcopy(base)
        p["id"] = it["id"]
        p["model"] = it["model"]
        tpl = entry.get("name_tpl", "{name}")
        p["name"] = it.get("name") or tpl.format(model=it["model"], name=it.get("name", it["model"]))
        p["img"] = it.get("img", []) + base.get("extra_img", [])
        p["params"] = [[c, v] for c, v in zip(entry["cols"], it["row"]) if v not in ("", "—")] + base.get("params", [])
        p["facets"] = it.get("facets", {})
        p["missing"] = base.get("missing", []) + it.get("missing", [])
        if it.get("note"):
            p["description"] = base["description"] + " " + it["note"]
        # соседние модели серии — в сопутствующие
        near = [i for i in ids if i != it["id"]]
        near = sorted(near, key=lambda x: abs(ids.index(x) - idx))[:4]
        p["related"] = near + base.get("related_extra", [])
        p["series_table"] = {"title": entry["title"] + " — все исполнения", "cols": entry["cols"],
                             "rows": [x["row"] for x in entry["items"]],
                             "links": [x["id"] for x in entry["items"]], "current": idx}
        p["series"] = entry["series"]
        series_of.setdefault(entry["series"], []).append(p["id"])
        add_product(p)

# дерево каталога
DIRS = catalog["directions"]
NODE = {}  # path -> node
for d in DIRS:
    NODE[d["slug"]] = {"type": "dir", "d": d}
    for c in d["categories"]:
        NODE[f"{d['slug']}/{c['slug']}"] = {"type": "cat", "d": d, "c": c}
        for s in c["subcategories"]:
            NODE[f"{d['slug']}/{c['slug']}/{s['slug']}"] = {"type": "sub", "d": d, "c": c, "s": s}

by_sub = {}
for p in products.values():
    if not p.get("cats"):
        warn(f"У товара {p['id']} нет категорий")
        continue
    for path in p["cats"]:
        if path not in NODE or NODE[path]["type"] != "sub":
            warn(f"{p['id']}: неизвестная подкатегория {path}")
            continue
        by_sub.setdefault(path, []).append(p["id"])


def url_of(path):
    return "/catalog/" + path + "/"


def purl(pid):
    return url_of(products[pid]["cats"][0]) + pid + "/"


def img_url(name):
    return f"/assets/img/p/{name}.jpg"


SIZES = {}


def img_size(name):
    if name in SIZES:
        return SIZES[name]
    path = os.path.join(IMG_DIR, name + ".jpg")
    if not os.path.exists(path):
        warn(f"Нет изображения: {name}")
        SIZES[name] = None
        return None
    if Image:
        with Image.open(path) as im:
            SIZES[name] = im.size
    else:
        SIZES[name] = (800, 600)
    return SIZES[name]


def img_tag(name, alt, cls="", lazy=True, sizes=None):
    sz = img_size(name)
    if not sz:
        return ""
    w, h = sz
    attrs = f' class="{cls}"' if cls else ""
    lz = ' loading="lazy" decoding="async"' if lazy else ""
    return f'<img src="{img_url(name)}" alt="{E(alt)}" width="{w}" height="{h}"{lz}{attrs}>'


for p in products.values():
    p["img"] = [i for i in p.get("img", []) if img_size(i)]
    for r in p.get("related", []):
        if r not in products:
            warn(f"{p['id']}: сопутствующий товар не найден: {r}")
    p["related"] = [r for r in p.get("related", []) if r in products and r != p["id"]]
    for dref in p.get("docs", []):
        if dref["doc"] not in documents:
            warn(f"{p['id']}: неизвестный документ {dref['doc']}")


def sub_products(path):
    return by_sub.get(path, [])


def cat_products(path):
    seen = []
    for k, ids in by_sub.items():
        if k.startswith(path + "/"):
            for i in ids:
                if i not in seen:
                    seen.append(i)
    return seen


# ---------------------------------------------------------------- шаблон
def missing_value(label="будет указано по карточке предприятия"):
    return f'<span class="placeholder">[{E(label)}]</span>'


def company_field(key, label=None):
    v = company.get(key, "")
    if not v:
        return missing_value(label or "будет указано по карточке предприятия")
    if key == "email":
        return f'<a href="mailto:{E(v)}">{E(v)}</a>'
    if key == "phone":
        return f'<a href="tel:{E(re.sub(r"[^0-9+]", "", v))}">{E(v)}</a>'
    return E(v)


ICONS = {
    "search": '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4.3-4.3"/></svg>',
    "menu": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M3 12h18M3 18h18"/></svg>',
    "close": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    "arrow": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
    "chev": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>',
    "doc": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></svg>',
    "download": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
}


def mega_menu():
    cols = []
    for d in DIRS:
        items = []
        for c in d["categories"]:
            subs = "".join(f'<li><a href="{url_of(d["slug"] + "/" + c["slug"] + "/" + s["slug"])}">{E(s["name"])}</a></li>'
                           for s in c["subcategories"])
            items.append(f'<li class="mm-cat"><a href="{url_of(d["slug"] + "/" + c["slug"])}">{E(c["name"])}</a><ul>{subs}</ul></li>')
        cols.append(f'<div class="mm-col"><a class="mm-dir" href="{url_of(d["slug"])}">{E(d["name"])}'
                    f'<span>{E(d.get("full", ""))}</span></a><ul>{"".join(items)}</ul></div>')
    return f'<div class="mega" id="mega" role="region" aria-label="Каталог оборудования"><div class="wrap mega-grid">{"".join(cols)}</div></div>'


def mobile_nav():
    dirs = []
    for d in DIRS:
        cats = []
        for c in d["categories"]:
            subs = "".join(f'<li><a href="{url_of(d["slug"] + "/" + c["slug"] + "/" + s["slug"])}">{E(s["name"])}</a></li>'
                           for s in c["subcategories"])
            cats.append(f'<details><summary>{E(c["name"])}</summary><ul><li><a href="{url_of(d["slug"] + "/" + c["slug"])}">Все: {E(c["name"].lower())}</a></li>{subs}</ul></details>')
        dirs.append(f'<details class="mn-dir"><summary>{E(d["name"])}</summary><div><a class="mn-all" href="{url_of(d["slug"])}">Всё направление</a>{"".join(cats)}</div></details>')
    return f'''<div class="mnav" id="mnav" hidden>
  <div class="mnav-head"><span class="logo"><img class="logo-mark" src="/assets/img/brand/mark-blue.svg" alt="" width="64" height="54"><span class="logo-txt">WITTE</span></span><button class="icon-btn" data-close-mnav aria-label="Закрыть меню">{ICONS["close"]}</button></div>
  <form class="mnav-search" action="/search/" role="search"><input type="search" name="q" placeholder="Поиск по каталогу" aria-label="Поиск по каталогу"><button aria-label="Найти">{ICONS["search"]}</button></form>
  <nav aria-label="Мобильное меню">
    <a class="mn-link" href="/">Главная</a>
    <div class="mn-group"><a class="mn-link" href="/catalog/">Каталог оборудования</a>{"".join(dirs)}</div>
    <a class="mn-link" href="/about/">О компании</a>
    <a class="mn-link" href="/documentation/">Документация</a>
    <a class="mn-link" href="/contacts/">Контакты</a>
  </nav>
  <a class="btn btn-accent btn-block" href="/request/">Запросить коммерческое предложение</a>
</div>'''


MEGA = None
MNAV = None
FOOTER = None


def footer():
    dirs = "".join(f'<li><a href="{url_of(d["slug"])}">{E(d["name"])}</a></li>' for d in DIRS)
    return f'''<footer class="footer">
  <div class="wrap footer-grid">
    <div>
      <div class="logo logo-light"><img class="logo-mark" src="/assets/img/brand/mark-white.svg" alt="" width="64" height="54"><span class="logo-text"><span class="logo-txt">WITTE</span><span class="logo-sub">оборудование для ферм</span></span></div>
      <p class="muted-light">Оборудование для свиноводства, птицеводства и КРС: кормление, поение, микроклимат, обогрев, полы и системы содержания.</p>
    </div>
    <div><h3>Каталог</h3><ul>{dirs}<li><a href="/search/">Поиск по каталогу</a></li></ul></div>
    <div><h3>Компания</h3><ul><li><a href="/about/">О компании</a></li><li><a href="/documentation/">Документация</a></li><li><a href="/contacts/">Контакты</a></li><li><a href="/request/">Запрос КП</a></li></ul></div>
    <div><h3>Контакты</h3>
      <ul class="contact-list">
        <li>Телефон: {company_field("phone")}</li>
        <li>E-mail: {company_field("email")}</li>
        <li>Адрес: {company_field("office_address")}</li>
      </ul>
    </div>
  </div>
  <div class="wrap footer-bottom">
    <span>© {datetime.date.today().year} {E(company.get("name_short", "Witte-rus"))}{(" — " + E(company["legal_name_short"]) + ", ИНН " + E(company.get("inn", ""))) if company.get("legal_name_short") else ""}. Сведения о товарах приведены по каталогам изготовителей и не являются публичной офертой.</span>
    <a href="/privacy/">Обработка персональных данных</a>
  </div>
</footer>'''


def breadcrumbs_html(crumbs):
    if not crumbs:
        return ""
    items = []
    for i, (name, href) in enumerate(crumbs):
        if href and i < len(crumbs) - 1:
            items.append(f'<li><a href="{href}">{E(name)}</a></li>')
        else:
            items.append(f'<li aria-current="page">{E(name)}</li>')
    return f'<nav class="crumbs" aria-label="Навигационная цепочка"><ol>{"".join(items)}</ol></nav>'


def breadcrumbs_ld(crumbs):
    base = company.get("site_url", "").rstrip("/")
    lst = [{"@type": "ListItem", "position": i + 1, "name": n, **({"item": base + h} if h else {})}
           for i, (n, h) in enumerate(crumbs)]
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": lst}


ALL_PAGES = []


def write_page(path, title, description, body, crumbs=None, active="", ld=None, og_image=None, noindex=False):
    """path — URL-путь вида /catalog/x/ ; файл site/catalog/x/index.html"""
    global MEGA, MNAV, FOOTER
    if MEGA is None:
        MEGA, MNAV, FOOTER = mega_menu(), mobile_nav(), footer()
    full_title = title if "Witte-rus" in title else f"{title} — Witte-rus"
    base = company.get("site_url", "").rstrip("/")
    lds = []
    if crumbs:
        lds.append(breadcrumbs_ld(crumbs))
    if ld:
        lds.append(ld)
    ld_html = "".join(f'<script type="application/ld+json">{json.dumps(x, ensure_ascii=False)}</script>' for x in lds)
    og_img = f'<meta property="og:image" content="{base}{img_url(og_image)}">' if og_image else ""

    def nav(href, label, key):
        cur = ' aria-current="page"' if active == key else ""
        return f'<a href="{href}"{cur}>{label}</a>'

    page = f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(full_title)}</title>
<meta name="description" content="{E(description)}">
<link rel="canonical" href="{base}{path}">
{'<meta name="robots" content="noindex">' if noindex else ''}
<meta property="og:type" content="website">
<meta property="og:locale" content="ru_RU">
<meta property="og:title" content="{E(full_title)}">
<meta property="og:description" content="{E(description)}">
<meta property="og:url" content="{base}{path}">
{og_img}
<meta name="theme-color" content="#0B49B0">
<link rel="icon" href="/assets/img/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&amp;family=Unbounded:wght@600;800&amp;display=swap">
<link rel="stylesheet" href="/assets/css/site.css">
{ld_html}
</head>
<body>
<a class="skip" href="#main">Перейти к содержанию</a>
<header class="header">
  <div class="wrap header-in">
    <a class="logo" href="/" aria-label="WITTE — на главную"><img class="logo-mark" src="/assets/img/brand/mark-blue.svg" alt="" width="64" height="54"><span class="logo-text"><span class="logo-txt">WITTE</span><span class="logo-sub">оборудование для ферм</span></span></a>
    <nav class="nav" aria-label="Основное меню">
      {nav("/", "Главная", "home")}
      <div class="nav-cat">
        <a href="/catalog/" class="nav-cat-link{' is-active' if active == 'catalog' else ''}" aria-haspopup="true" aria-expanded="false" aria-controls="mega">Каталог оборудования {ICONS["chev"]}</a>
        {MEGA}
      </div>
      {nav("/about/", "О компании", "about")}
      {nav("/documentation/", "Документация", "docs")}
      {nav("/contacts/", "Контакты", "contacts")}
    </nav>
    <div class="header-actions">
      <a class="icon-btn hsearch-btn" href="/search/" aria-label="Поиск по каталогу" title="Поиск по каталогу">{ICONS["search"]}</a>
      <a class="btn btn-accent btn-cta" href="/request/">Запросить КП</a>
      <button class="icon-btn burger" data-open-mnav aria-label="Открыть меню" aria-controls="mnav" aria-expanded="false">{ICONS["menu"]}</button>
    </div>
  </div>
</header>
{MNAV}
<main id="main">
{breadcrumbs_html(crumbs) if crumbs else ''}
{body}
</main>
{FOOTER}
<div class="lightbox" id="lightbox" hidden><button class="icon-btn lb-close" data-lb-close aria-label="Закрыть">{ICONS["close"]}</button><button class="lb-nav lb-prev" data-lb-prev aria-label="Предыдущее фото">‹</button><img alt=""><button class="lb-nav lb-next" data-lb-next aria-label="Следующее фото">›</button><div class="lb-cap"></div></div>
<script src="/assets/js/site.js" defer></script>
</body>
</html>
'''
    out = os.path.join(SITE, path.strip("/"), "index.html") if path != "/404.html" else os.path.join(SITE, "404.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    if not noindex:
        ALL_PAGES.append(path)


# ---------------------------------------------------------------- компоненты
def key_params(p, n=3):
    out = []
    model = p.get("model", "").replace(" ", "")
    params = [(k, v) for k, v in p.get("params", []) if v.replace(" ", "") not in (model, model.replace("/", ","))]
    for k, v in params[:n]:
        out.append(f"<li><span>{E(k)}</span><b>{E(v)}</b></li>")
    if len(out) < n:
        for k, v in p.get("facets", {}).items():
            if len(out) < n and v and v != "—" and not p.get("series"):
                out.append(f"<li><span>{E(k)}</span><b>{E(v)}</b></li>")
    if len(out) < n and p.get("tables"):
        t = p["tables"][0]
        out.append(f"<li><span>Исполнений</span><b>{len(t['rows'])}</b></li>")
    return f'<ul class="kp">{"".join(out)}</ul>' if out else ""


def product_card(pid, show_path=False):
    p = products[pid]
    img = img_tag(p["img"][0], p["name"]) if p["img"] else '<span class="noimg">Фото готовится</span>'
    facets = json.dumps(p.get("facets", {}), ensure_ascii=False)
    air = p.get("airflow")
    air_attr = f' data-air="{air[0]}-{air[1]}"' if air else ""
    text = " ".join([p["name"], p.get("model", ""), p.get("summary", "")]).lower()
    return f'''<article class="pcard" data-facets='{E(facets)}'{air_attr} data-text="{E(text)}">
  <a class="pcard-img" href="{purl(pid)}" tabindex="-1" aria-hidden="true">{img}</a>
  <div class="pcard-body">
    <div class="pcard-model">{E(p.get("model", ""))}</div>
    <h3 class="pcard-title"><a href="{purl(pid)}">{E(p["name"])}</a></h3>
    <p class="pcard-sum">{E(p.get("summary", ""))}</p>
    {key_params(p)}
  </div>
  <div class="pcard-foot"><a class="link-more" href="{purl(pid)}">Подробнее {ICONS["arrow"]}</a><a class="link-req" href="/request/?product={pid}">Запросить цену</a></div>
</article>'''


def filters_block(pids):
    facets = {}
    for pid in pids:
        for k, v in products[pid].get("facets", {}).items():
            if v and v != "—":
                facets.setdefault(k, set()).add(v)
    selects = []
    for k, vals in facets.items():
        if len(vals) < 2:
            continue
        opts = "".join(f'<option value="{E(v)}">{E(v)}</option>' for v in sorted(vals))
        selects.append(f'<label class="fl"><span>{E(k)}</span><select data-facet="{E(k)}"><option value="">Все</option>{opts}</select></label>')
    if any(products[pid].get("airflow") for pid in pids):
        selects.append('<label class="fl"><span>Нужная производительность, м³/ч</span><input type="number" inputmode="numeric" min="0" step="500" placeholder="например, 30000" data-air-filter></label>')
    search = '<label class="fl fl-grow"><span>Поиск в разделе</span><input type="search" placeholder="Модель, артикул, название" data-text-filter></label>' if len(pids) > 3 else ""
    if not selects and not search:
        return ""
    return f'''<div class="filters" data-filters>{search}{"".join(selects)}<button type="button" class="btn btn-ghost btn-sm" data-reset>Сбросить</button><div class="fl-count" data-count aria-live="polite"></div></div>'''


def grid(pids, with_filters=True):
    cards = "".join(product_card(i) for i in pids)
    fb = filters_block(pids) if with_filters else ""
    empty = '<p class="empty" data-empty hidden>По заданным условиям изделия не найдены. Измените фильтры.</p>'
    return f'<div class="listing">{fb}<div class="pgrid" data-grid>{cards}</div>{empty}</div>'


def tile(href, title, text, image, count=None, cls="tile"):
    img = img_tag(image, title) if image and img_size(image) else ""
    cnt = f'<span class="tile-count">{count} {plural(count, "позиция", "позиции", "позиций")}</span>' if count is not None else ""
    return f'<a class="{cls}" href="{href}"><div class="tile-img">{img}</div><div class="tile-body"><h3>{E(title)}</h3><p>{E(text)}</p>{cnt}</div></a>'


def low(name):
    """Строчные буквы для середины фразы, но аббревиатуры (КРС) не трогаем."""
    return name if name.isupper() else name.lower()


def count_label(path, cat=None):
    if cat is not None and cat.get("on_request"):
        return "подбор по запросу"
    n = len(cat_products(path))
    return f"{n} {plural(n, 'позиция', 'позиции', 'позиций')}"


def plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def table_html(t, link_rows=None, current=None):
    head = "".join(f"<th>{E(c)}</th>" for c in t["cols"])
    rows = []
    for i, r in enumerate(t["rows"]):
        cells = []
        for j, v in enumerate(r):
            v = E(str(v))
            if j == 0 and link_rows and i != current:
                v = f'<a href="{purl(link_rows[i])}">{v}</a>'
            cells.append(f"<td>{v}</td>" if j else f'<th scope="row">{v}</th>')
        cls = ' class="is-current"' if current == i else ""
        rows.append(f"<tr{cls}>{''.join(cells)}</tr>")
    title = f'<h3 class="tbl-title">{E(t["title"])}</h3>' if t.get("title") else ""
    return f'{title}<div class="tbl-wrap" tabindex="0"><table class="tbl"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def doc_link(dref):
    d = documents[dref["doc"]]
    page = ""
    m = re.match(r"(\d+)", dref.get("pages", ""))
    if m and d["file"].endswith(".pdf"):
        page = f"#page={m.group(1)}"
    pages = f', стр. {E(dref["pages"])}' if dref.get("pages") and not dref["pages"].startswith("слайд") else (f', {E(dref["pages"])}' if dref.get("pages") else "")
    ext = d["file"].rsplit(".", 1)[-1].upper()
    return f'<li><a class="doc-link" href="/docs/{d["file"]}{page}" target="_blank" rel="noopener">{ICONS["doc"]}<span><b>{E(d["title"])}</b><small>{ext}, {E(d["lang"])}{pages}</small></span></a></li>'


# ---------------------------------------------------------------- страницы каталога
def build_catalog():
    # /catalog/
    blocks = []
    for d in DIRS:
        cats = "".join(
            f'<li><a href="{url_of(d["slug"] + "/" + c["slug"])}">{E(c["name"])}</a><span>{"по запросу" if c.get("on_request") else len(cat_products(d["slug"] + "/" + c["slug"]))}</span></li>'
            for c in d["categories"])
        blocks.append(f'''<section class="dir-block">
  <a class="dir-block-img" href="{url_of(d["slug"])}">{img_tag(d["image"], d["name"])}</a>
  <div class="dir-block-body"><h2><a href="{url_of(d["slug"])}">{E(d["name"])}</a></h2><p>{E(d["description"])}</p><ul class="cat-list">{cats}</ul>
  <a class="btn btn-primary" href="{url_of(d["slug"])}">Перейти в раздел</a></div>
</section>''')
    body = f'''<section class="page-head wrap"><h1>Каталог оборудования</h1><p class="lead">Выберите направление животноводства, затем категорию оборудования и модель. Одна и та же позиция может применяться в нескольких направлениях — карточка изделия общая.</p></section>
<div class="wrap dir-blocks">{"".join(blocks)}</div>'''
    write_page("/catalog/", "Каталог оборудования для животноводства",
               "Каталог оборудования Witte-rus для свиноводства, птицеводства и КРС: кормление, поение, вентиляция, обогрев, полы и станки.",
               body, crumbs=[("Главная", "/"), ("Каталог оборудования", "/catalog/")], active="catalog")

    for d in DIRS:
        dpath = d["slug"]
        tiles = []
        for c in d["categories"]:
            cp = f"{dpath}/{c['slug']}"
            subs = "".join(f'<li><a href="{url_of(cp + "/" + s["slug"])}">{E(s["name"])}</a></li>' for s in c["subcategories"])
            tiles.append(f'''<article class="cat-card">
  <a class="cat-card-img" href="{url_of(cp)}">{img_tag(c["image"], c["name"])}</a>
  <div class="cat-card-body"><h2><a href="{url_of(cp)}">{E(c["name"])}</a></h2><p>{E(c["description"])}</p><ul>{subs}</ul>
  <span class="tile-count">{count_label(cp, c)}</span></div>
</article>''')
        body = f'''<section class="dir-hero"><div class="dir-hero-img">{img_tag(d["image"], d["name"], lazy=False)}</div>
<div class="wrap dir-hero-in"><h1>{E(d.get("full", d["name"]))}</h1><p class="lead">{E(d["description"])}</p></div></section>
<div class="wrap section"><div class="cat-grid">{"".join(tiles)}</div>
<p class="note">Разделы с пометкой «подбор по запросу» комплектуются под проект: оставьте заявку, и мы подготовим предложение.</p></div>'''
        write_page(url_of(dpath), f"{d.get('full', d['name'])}: оборудование", d["description"], body,
                   crumbs=[("Главная", "/"), ("Каталог", "/catalog/"), (d["name"], url_of(dpath))], active="catalog",
                   og_image=d["image"])

        for c in d["categories"]:
            cp = f"{dpath}/{c['slug']}"
            subs = "".join(tile(url_of(cp + "/" + s["slug"]), s["name"], s["description"], s["image"],
                                len(sub_products(cp + "/" + s["slug"])), "tile tile-sm") for s in c["subcategories"])
            if c.get("on_request"):
                points = "".join(f"<li>{E(x)}</li>" for x in c.get("request_points", []))
                others = [x for x in DIRS if x is not d and any(cc["slug"] == c["slug"] for cc in x["categories"])]
                body = f'''<section class="page-head wrap"><span class="eyebrow">{E(d["name"])}</span><h1>{E(c["name"])}</h1><p class="lead">{E(c["description"])}</p></section>
<div class="wrap two-col two-col-form section-tight">
  <div class="req-aside">
    <div class="onreq-badge">Подбор по запросу</div>
    <h2>Что указать в заявке</h2><ul class="ticks">{points}</ul>
    <p class="note">Позиции этого раздела комплектуются под конкретный объект. Технические характеристики и стоимость — в коммерческом предложении.</p>
    {img_tag(c["image"], c["name"], cls="onreq-img") if c.get("image") else ""}
  </div>
  <div><h2>Запрос подбора: {E(c["name"].lower())}</h2>{request_form(products_value=d["name"] + " — " + c["name"].lower(), direction=d.get("full", d["name"]))}</div>
</div>'''
                write_page(url_of(cp), f"{c['name']} — {low(d['name'])}", c["description"], body,
                           crumbs=[("Главная", "/"), ("Каталог", "/catalog/"), (d["name"], url_of(dpath)), (c["name"], url_of(cp))],
                           active="catalog", og_image=c.get("image"))
                continue
            pids = cat_products(cp)
            sections = []
            for s in c["subcategories"]:
                sp = cp + "/" + s["slug"]
                ids = sub_products(sp)
                if not ids:
                    continue
                sections.append(f'<section class="sub-sec"><div class="sub-sec-head"><h2><a href="{url_of(sp)}">{E(s["name"])}</a></h2><a class="link-more" href="{url_of(sp)}">Все {len(ids)} {ICONS["arrow"]}</a></div>{grid(ids[:4] if len(ids) > 6 else ids, with_filters=False)}</section>')
            others = [x for x in DIRS if x is not d and any(cc["slug"] == c["slug"] for cc in x["categories"])]
            other_html = ""
            if others:
                other_html = '<p class="also">Эта категория есть и в других направлениях: ' + ", ".join(
                    f'<a href="{url_of(o["slug"] + "/" + c["slug"])}">{E(o["name"])}</a>' for o in others) + "</p>"
            body = f'''<section class="page-head wrap"><span class="eyebrow">{E(d["name"])}</span><h1>{E(c["name"])}</h1><p class="lead">{E(c["description"])}</p>{other_html}</section>
<div class="wrap"><div class="tiles">{subs}</div></div>
<div class="wrap section">{"".join(sections)}</div>'''
            write_page(url_of(cp), f"{c['name']} — {low(d['name'])}", c["description"], body,
                       crumbs=[("Главная", "/"), ("Каталог", "/catalog/"), (d["name"], url_of(dpath)), (c["name"], url_of(cp))],
                       active="catalog", og_image=c["image"])

            for s in c["subcategories"]:
                sp = cp + "/" + s["slug"]
                ids = sub_products(sp)
                siblings = "".join(
                    f'<a class="chip{" is-active" if ss is s else ""}" href="{url_of(cp + "/" + ss["slug"])}">{E(ss["name"])}</a>'
                    for ss in c["subcategories"])
                ld = {"@context": "https://schema.org", "@type": "ItemList", "name": s["name"],
                      "itemListElement": [{"@type": "ListItem", "position": i + 1, "url": company.get("site_url", "").rstrip("/") + purl(x), "name": products[x]["name"]} for i, x in enumerate(ids)]}
                body = f'''<section class="page-head wrap"><span class="eyebrow">{E(d["name"])} · {E(c["name"])}</span><h1>{E(s["name"])}</h1><p class="lead">{E(s["description"])}</p>
<div class="chips">{siblings}</div></section>
<div class="wrap section-tight">{grid(ids) if ids else '<p class="empty">Изделия будут добавлены.</p>'}</div>'''
                write_page(url_of(sp), f"{s['name']} — {c['name'].lower()}, {low(d['name'])}",
                           f"{s['name']}: {s['description']} Модели, характеристики и документация.", body,
                           crumbs=[("Главная", "/"), ("Каталог", "/catalog/"), (d["name"], url_of(dpath)), (c["name"], url_of(cp)), (s["name"], url_of(sp))],
                           active="catalog", ld=ld, og_image=s["image"])


def build_products():
    for pid, p in products.items():
        path = p["cats"][0]
        n = NODE[path]
        d, c, s = n["d"], n["c"], n["s"]
        crumbs = [("Главная", "/"), ("Каталог", "/catalog/"), (d["name"], url_of(d["slug"])),
                  (c["name"], url_of(d["slug"] + "/" + c["slug"])), (s["name"], url_of(path)), (p["name"], purl(pid))]
        # галерея
        if p["img"]:
            main = img_tag(p["img"][0], p["name"], cls="gal-main-img", lazy=False)
            thumbs = "".join(
                f'<button class="gal-thumb{" is-active" if i == 0 else ""}" data-src="{img_url(im)}" data-idx="{i}" aria-label="Фото {i + 1}">{img_tag(im, p["name"] + " — фото " + str(i + 1))}</button>'
                for i, im in enumerate(p["img"]))
            gallery = f'''<div class="gallery" data-gallery data-images='{E(json.dumps([img_url(i) for i in p["img"]]))}'>
  <button class="gal-main" data-lb-open aria-label="Увеличить фото">{main}</button>
  {f'<div class="gal-thumbs">{thumbs}</div>' if len(p["img"]) > 1 else ''}
</div>'''
        else:
            gallery = '<div class="gallery"><div class="gal-empty">Фотография изделия готовится</div></div>'
        params = p.get("params", [])
        params_html = ""
        if params:
            rows = "".join(f'<tr><th scope="row">{E(k)}</th><td>{E(v)}</td></tr>' for k, v in params)
            params_html = f'<div class="tbl-wrap"><table class="tbl tbl-kv"><tbody>{rows}</tbody></table></div>'
        tables = "".join(table_html(t) for t in p.get("tables", []))
        series_tbl = ""
        if p.get("series_table"):
            st = p["series_table"]
            series_tbl = f'<section class="psec" id="series"><h2>Другие исполнения серии</h2>{table_html(st, st["links"], st["current"])}</section>'
        feats = "".join(f"<li>{E(x)}</li>" for x in p.get("features", []))
        kit = "".join(f"<li>{E(x)}</li>" for x in p.get("kit", []))
        docs = "".join(doc_link(x) for x in p.get("docs", []))
        apps = []
        for cpath in p["cats"]:
            nn = NODE[cpath]
            apps.append(f'<li><a href="{url_of(cpath)}">{E(nn["d"]["name"])} → {E(nn["c"]["name"])} → {E(nn["s"]["name"])}</a></li>')
        related = "".join(product_card(r) for r in p.get("related", [])[:4])
        source = ", ".join(f'{documents[x["doc"]]["title"]}' + (f' ({x["pages"]})' if x.get("pages") else "") for x in p.get("docs", []))
        maker = f'<p class="maker">Изготовитель: {E(p["maker"])}</p>' if p.get("maker") else ""
        incomplete = '<p class="note">Часть характеристик уточняется. Полные технические данные и стоимость — в коммерческом предложении.</p>' if p.get("missing") else ""
        toc = []
        if p.get("description") or p.get("purpose"):
            toc.append(("about", "Описание"))
        if params or tables:
            toc.append(("specs", "Характеристики"))
        if p.get("series_table"):
            toc.append(("series", "Исполнения"))
        if kit:
            toc.append(("kit", "Комплектация"))
        if docs:
            toc.append(("docs", "Документация"))
        if related:
            toc.append(("related", "Сопутствующие"))
        toc_html = "".join(f'<a href="#{a}">{t}</a>' for a, t in toc)
        variants = f'<p><b>Исполнения:</b> {E(p["variants"])}</p>' if p.get("variants") else ""
        body = f'''<div class="wrap product">
  <div class="product-top">
    {gallery}
    <div class="product-info">
      <div class="pcard-model">{E(p.get("model", ""))}</div>
      <h1>{E(p["name"])}</h1>
      <p class="lead">{E(p.get("summary", ""))}</p>
      {maker}
      {key_params(p, 5)}
      <div class="product-cta">
        <a class="btn btn-accent" href="/request/?product={pid}">Запросить цену</a>
        {f'<a class="btn btn-ghost" href="#docs">{ICONS["doc"]} Документация</a>' if docs else ''}
      </div>
      <div class="applies"><h2>Применение</h2><ul>{"".join(apps)}</ul></div>
    </div>
  </div>
  <nav class="ptoc" aria-label="Разделы карточки">{toc_html}</nav>
  <section class="psec" id="about"><h2>Описание</h2>
    {f'<p>{E(p["description"])}</p>' if p.get("description") else f'<p>{E(p.get("summary", ""))}</p>'}
    {f'<p><b>Назначение:</b> {E(p["purpose"])}</p>' if p.get("purpose") else ''}
    {variants}
    {f'<h3>Особенности</h3><ul class="ticks">{feats}</ul>' if feats else ''}
  </section>
  {f'<section class="psec" id="specs"><h2>Технические характеристики</h2>{params_html}{tables}{incomplete}</section>' if (params_html or tables) else f'<section class="psec" id="specs"><h2>Технические характеристики</h2>{incomplete or "<p class=note>Характеристики уточняются.</p>"}</section>'}
  {series_tbl}
  {f'<section class="psec" id="kit"><h2>Комплектация и состав</h2><ul class="ticks">{kit}</ul></section>' if kit else ''}
  {f'<section class="psec" id="docs"><h2>Документация</h2><ul class="doc-list">{docs}</ul><p class="src">Источник данных: {E(source)}.</p></section>' if docs else ''}
  {f'<section class="psec" id="related"><h2>Сопутствующие изделия</h2><div class="pgrid">{related}</div></section>' if related else ''}
  <section class="psec cta-band"><div><h2>Нужен расчёт под ваш объект?</h2><p>Укажите количество голов, размеры помещений и состав оборудования — подготовим коммерческое предложение.</p></div><a class="btn btn-accent" href="/request/?product={pid}">Запросить коммерческое предложение</a></section>
</div>'''
        base = company.get("site_url", "").rstrip("/")
        ld = {"@context": "https://schema.org", "@type": "Product", "name": p["name"], "model": p.get("model", ""),
              "description": p.get("description") or p.get("summary", ""), "category": f'{d["name"]} / {c["name"]} / {s["name"]}',
              "image": [base + img_url(i) for i in p["img"]], "url": base + purl(pid)}
        desc = f'{p["name"]} ({p.get("model", "")}): {p.get("summary", "")} Характеристики, фото, документация, запрос цены.'
        model = p.get("model", "")
        title = p["name"] if (model in p["name"] or len(model) > 24 or model in ("—", "")) else f'{p["name"]} {model}'
        write_page(purl(pid), title, desc, body,
                   crumbs=crumbs, active="catalog", ld=ld, og_image=p["img"][0] if p["img"] else None)


# ---------------------------------------------------------------- прочие страницы
def request_form(prefill=True, compact=False, products_value="", direction=""):
    dirs = "".join(f'<option{" selected" if d.get("full", d["name"]) == direction else ""}>{E(d.get("full", d["name"]))}</option>' for d in DIRS)
    return f'''<form class="rform{' rform-compact' if compact else ''}" data-request-form novalidate>
  <div class="rform-grid">
    <label><span>Имя и фамилия *</span><input name="name" required autocomplete="name"></label>
    <label><span>Компания</span><input name="company" autocomplete="organization"></label>
    <label><span>Телефон *</span><input name="phone" type="tel" required autocomplete="tel" inputmode="tel"></label>
    <label><span>E-mail</span><input name="email" type="email" autocomplete="email"></label>
    <label><span>Регион поставки</span><input name="region"></label>
    <label><span>Направление</span><select name="direction"><option value="">Не выбрано</option>{dirs}</select></label>
    <label class="span2"><span>Оборудование</span><input name="products" data-products-field placeholder="Например: самокормушки YLC-100, 120 шт." value="{E(products_value)}"></label>
    <label class="span2"><span>Комментарий: поголовье, размеры помещений, сроки</span><textarea name="comment" rows="4"></textarea></label>
  </div>
  <label class="check"><input type="checkbox" name="consent" required> <span>Согласен на обработку персональных данных для подготовки коммерческого предложения (<a href="/privacy/">подробнее</a>)</span></label>
  <input type="text" name="website" class="hp" tabindex="-1" autocomplete="off" aria-hidden="true">
  <div class="rform-actions"><button class="btn btn-accent" type="submit">Отправить запрос</button><p class="rform-msg" data-msg role="status" aria-live="polite"></p></div>
</form>'''


def build_home():
    d_tiles = "".join(
        f'''<a class="dir-card" href="{url_of(d["slug"])}"><div class="dir-card-img">{img_tag(d["image"], d["name"])}</div>
<div class="dir-card-body"><h3>{E(d["name"])}</h3><p>{E(d["short"])}</p><ul>{"".join("<li>" + E(c["name"]) + "</li>" for c in d["categories"])}</ul><span class="link-more">Перейти {ICONS["arrow"]}</span></div></a>'''
        for d in DIRS)
    pig = DIRS[0]
    cat_tiles = "".join(tile(url_of(pig["slug"] + "/" + c["slug"]), c["name"], c["description"], c["image"],
                             len(cat_products(pig["slug"] + "/" + c["slug"]))) for c in pig["categories"])
    featured = ["dry-wet-feeders", "nipple-fx-006", "fan-hyfb", "cooling-pad", "plastic-floors-lwd6060",
                "farrowing-crate", "feed-silo-gy", "pvc-panels-lw50035zz"]
    feat = "".join(product_card(i) for i in featured if i in products)
    n_models = len(products)
    n_subs = sum(len(c["subcategories"]) for d in DIRS for c in d["categories"])
    cases = "".join(f'<button class="ph" data-lb-single="{img_url(n)}" data-cap="Объект с оборудованием из каталога изготовителя">{img_tag(n, "Свинокомплекс с пластиковыми полами и ПВХ-ограждениями")}</button>'
                    for n in ["case-1", "case-2", "case-3", "case-5", "case-6", "case-7"])
    plants = "".join(f'<button class="ph" data-lb-single="{img_url(n)}" data-cap="Производственная площадка изготовителя полимерных изделий">{img_tag(n, "Производство полимерных изделий для животноводства")}</button>'
                     for n in ["plant-1", "plant-2", "plant-3", "plant-4"])
    docs = "".join(
        f'<li><a class="doc-link" href="/docs/{d["file"]}" target="_blank" rel="noopener">{ICONS["doc"]}<span><b>{E(d["title"])}</b><small>{d["file"].rsplit(".", 1)[-1].upper()}, {E(d["lang"])}, {d["pages"]} стр.</small></span></a></li>'
        for d in list(documents.values())[:6])
    facts = [
        ("Полный цикл свинокомплекса", "От бункера хранения корма и линии раздачи до поилок, вентиляции, щелевых полов и станков опороса — в одном каталоге."),
        ("Нержавеющая сталь в контакте с кормом и водой", "Ниппельные поилки, поилки-чаши, кормушки и поддоны самокормушек — из стали SUS304 / AISI 304."),
        ("Материалы для агрессивной среды", "Стеклопластиковые вентиляторы и опоры полов, ПВХ- и PP-панели, керамические нагреватели — для помещений с аммиаком и высокой влажностью."),
        ("Данные по каждой модели", "Карточки содержат таблицы характеристик и ссылки на страницы исходных каталогов изготовителей."),
    ]
    facts_html = "".join(f'<div class="fact"><span class="fact-n">0{i + 1}</span><h3>{E(t)}</h3><p>{E(x)}</p></div>' for i, (t, x) in enumerate(facts))
    body = f'''<section class="hero">
  <div class="hero-img">{img_tag("case-4", "Свинокомплекс: станки с ПВХ-ограждениями и щелевыми полами", lazy=False)}</div>
  <div class="wrap hero-in">
    <span class="eyebrow eyebrow-light">Witte-rus · оборудование для животноводства</span>
    <h1>Оборудование для свиноводства, птицеводства и КРС</h1>
    <p class="lead">Кормление, поение, микроклимат, обогрев, щелевые полы и системы содержания животных. Каталог с техническими характеристиками и документацией по каждой модели.</p>
    <div class="hero-cta"><a class="btn btn-accent" href="/catalog/">Каталог оборудования</a><a class="btn btn-light" href="/request/">Запросить коммерческое предложение</a></div>
    <dl class="hero-stats"><div><dt>{n_models}</dt><dd>моделей и серий в каталоге</dd></div><div><dt>{n_subs}</dt><dd>разделов оборудования</dd></div><div><dt>3</dt><dd>направления животноводства</dd></div></dl>
  </div>
</section>
<section class="section wrap"><div class="sec-head"><h2>Направления</h2><p>Сначала выберите отрасль — внутри вы найдёте категории оборудования и конкретные модели.</p></div><div class="dir-cards">{d_tiles}</div></section>
<section class="section section-alt"><div class="wrap"><div class="sec-head"><h2>Оборудование для свиноводства</h2><a class="link-more" href="{url_of(pig["slug"])}">Весь раздел {ICONS["arrow"]}</a></div><div class="tiles">{cat_tiles}</div></div></section>
<section class="section wrap"><div class="sec-head"><h2>Ключевые позиции каталога</h2><a class="link-more" href="/catalog/">Весь каталог {ICONS["arrow"]}</a></div><div class="pgrid">{feat}</div></section>
<section class="section section-dark"><div class="wrap"><div class="sec-head"><h2>Особенности оборудования</h2><p>По данным технических каталогов, на основе которых сформирован ассортимент.</p></div><div class="facts">{facts_html}</div></div></section>
<section class="section wrap"><div class="sec-head"><h2>Оборудование на объектах</h2><p>Фотографии объектов и производственных площадок из каталога изготовителя полимерных изделий.</p></div><div class="photos">{cases}</div><div class="photos photos-4">{plants}</div></section>
<section class="section section-alt"><div class="wrap two-col"><div><h2>Документация</h2><p>Каталоги изготовителей с таблицами характеристик. Ссылки на нужные страницы есть в каждой карточке изделия.</p><a class="btn btn-primary" href="/documentation/">Все документы</a></div><ul class="doc-list">{docs}</ul></div></section>
<section class="section wrap" id="request"><div class="two-col two-col-form"><div><h2>Запрос коммерческого предложения</h2><p>Опишите задачу: направление, поголовье, размеры помещений, перечень оборудования. Мы подготовим подбор и расчёт.</p>
<ul class="contact-list"><li>Телефон: {company_field("phone")}</li><li>E-mail: {company_field("email")}</li><li>Адрес: {company_field("office_address")}</li></ul></div>{request_form()}</div></section>'''
    write_page("/", "Witte-rus — оборудование для свиноводства, птицеводства и КРС",
               "Оборудование для животноводства: кормление, поение, вентиляция, обогрев, щелевые полы, станки и ограждения для свиноводства, птицеводства и КРС. Каталог с характеристиками и документацией.",
               body, active="home", og_image="case-4",
               ld={"@context": "https://schema.org", "@type": "Organization", "name": company.get("name_short"),
                   "legalName": company.get("legal_name"), "taxID": company.get("inn"), "email": company.get("email"),
                   "address": {"@type": "PostalAddress", "streetAddress": company.get("legal_address"), "addressLocality": company.get("city"), "addressCountry": "RU"},
                   "url": company.get("site_url")})


def map_block():
    addr = company.get("office_address", "")
    if not addr:
        return '<div class="map-ph">Карта проезда будет добавлена после уточнения адреса</div>'
    q = urllib.parse.quote(addr)
    return (f'<div class="map-ph map-link"><div><b>{E(addr)}</b><br>'
            f'<a class="btn btn-ghost btn-sm" href="https://yandex.ru/maps/?text={q}" target="_blank" rel="noopener">Открыть на Яндекс Картах</a></div></div>')


def build_static_pages():
    counts = "".join(f'<li><a href="{url_of(d["slug"])}">{E(d.get("full", d["name"]))}</a> — {len(cat_products(d["slug"]))} {plural(len(cat_products(d["slug"])), "позиция", "позиции", "позиций")}</li>' for d in DIRS)
    rows = [("Полное наименование", "legal_name"), ("Сокращённое наименование", "legal_name_short"), ("ИНН", "inn"), ("КПП", "kpp"), ("ОГРН", "ogrn"),
            ("Юридический адрес", "legal_address"), ("Почтовый адрес", "office_address"), ("Руководитель", "director"),
            ("E-mail", "email"), ("Телефон", "phone"), ("Банк", "bank_name"), ("Расчётный счёт", "bank_account"),
            ("Корреспондентский счёт", "bank_corr_account"), ("БИК", "bank_bik")]
    req = "".join(f'<tr><th scope="row">{k}</th><td>{company_field(v)}</td></tr>' for k, v in rows)
    body = f'''<section class="page-head wrap"><h1>О компании</h1><p class="lead">Witte-rus ({E(company.get("legal_name_short", ""))}, {E(company.get("city", ""))}) — поставщик оборудования для животноводства. Каталог компании охватывает свиноводство, птицеводство и КРС.</p></section>
<div class="wrap two-col section-tight">
  <div>
    <h2>Направления каталога</h2>
    <ul class="ticks">{counts}</ul>
    <h2>Что входит в ассортимент</h2>
    <p>Системы хранения и раздачи корма, самокормушки и дозаторы, ниппельное и чашечное поение, вытяжная и приточная вентиляция, испарительное охлаждение, контроллеры микроклимата, теплогенераторы и калориферы, чугунные и пластиковые щелевые полы, стеклопластиковые опоры, станки для свиноматок, ограждения из ПВХ- и PP-панелей.</p>
    <div class="placeholder-box"><h3>История и преимущества компании</h3><p>Раздел будет заполнен по официальным материалам предприятия: год основания, география поставок, реализованные проекты, сервис и гарантийные обязательства.</p></div>
  </div>
  <div>
    <h2>Реквизиты</h2>
    <div class="tbl-wrap"><table class="tbl tbl-kv"><tbody>{req}</tbody></table></div>
    <p class="note">Данные приведены по карточке предприятия.</p>
  </div>
</div>
<section class="section section-alt"><div class="wrap"><div class="sec-head"><h2>Оборудование на объектах</h2><p>Фотографии из каталога изготовителя полимерных изделий.</p></div><div class="photos">{"".join(f'<button class="ph" data-lb-single="{img_url(n)}">{img_tag(n, "Объект с оборудованием")}</button>' for n in ["case-1", "case-3", "case-5", "case-6", "case-7", "case-8"])}</div></div></section>'''
    write_page("/about/", "О компании Witte-rus", "Witte-rus — поставщик оборудования для свиноводства, птицеводства и КРС.", body,
               crumbs=[("Главная", "/"), ("О компании", "/about/")], active="about")

    # документация
    doc_rows = []
    for d in documents.values():
        fpath = os.path.join(SITE, "docs", d["file"])
        size = f"{os.path.getsize(fpath) / 1048576:.1f} МБ".replace(".", ",") if os.path.exists(fpath) else "—"
        if not os.path.exists(fpath):
            warn(f"Нет файла документа: {d['file']}")
        used = [x["slug"] for x in DIRS if any(c.startswith(x["slug"] + "/") for pid in products for c in products[pid]["cats"] if any(r["doc"] == d["id"] for r in products[pid].get("docs", [])))]
        used_names = ", ".join(NODE[u]["d"]["name"] for u in used)
        doc_rows.append(f'<tr><th scope="row"><a href="/docs/{d["file"]}" target="_blank" rel="noopener">{E(d["title"])}</a></th><td>{E(d["topics"])}</td><td>{E(used_names)}</td><td>{d["file"].rsplit(".", 1)[-1].upper()}, {E(d["lang"])}</td><td>{d["pages"]}</td><td>{size}</td><td><a class="btn btn-ghost btn-sm" href="/docs/{d["file"]}" download>{ICONS["download"]} Скачать</a></td></tr>')
    body = f'''<section class="page-head wrap"><h1>Документация</h1><p class="lead">Технические каталоги изготовителей, по которым составлены карточки изделий. Ссылки на конкретные страницы есть в карточке каждой модели.</p></section>
<div class="wrap section-tight"><div class="tbl-wrap"><table class="tbl tbl-docs"><thead><tr><th>Документ</th><th>Содержание</th><th>Направление</th><th>Формат, язык</th><th>Стр.</th><th>Размер</th><th></th></tr></thead><tbody>{"".join(doc_rows)}</tbody></table></div>
<div class="placeholder-box"><h3>Сертификаты и декларации</h3><p>Сертификаты соответствия, декларации ЕАЭС, паспорта и инструкции на русском языке будут размещены после их предоставления.</p></div></div>'''
    write_page("/documentation/", "Документация и каталоги", "Технические каталоги оборудования для животноводства: кормление, поение, вентиляция, обогрев, полы, станки.",
               body, crumbs=[("Главная", "/"), ("Документация", "/documentation/")], active="docs")

    body = f'''<section class="page-head wrap"><h1>Контакты</h1><p class="lead">{E(company.get("legal_name_short", ""))}, {E(company.get("city", ""))}. Напишите нам на e-mail или оставьте заявку через форму.</p></section>
<div class="wrap two-col two-col-form section-tight">
  <div>
    <div class="tbl-wrap"><table class="tbl tbl-kv"><tbody>
      <tr><th scope="row">Телефон</th><td>{company_field("phone")}</td></tr>
      <tr><th scope="row">E-mail</th><td>{company_field("email")}</td></tr>
      <tr><th scope="row">Адрес</th><td>{company_field("office_address")}</td></tr>
      <tr><th scope="row">Режим работы</th><td>{company_field("work_hours")}</td></tr>
      <tr><th scope="row">Юридическое лицо</th><td>{company_field("legal_name")}</td></tr>
      <tr><th scope="row">ИНН / КПП</th><td>{company_field("inn")} / {company_field("kpp")}</td></tr>
      <tr><th scope="row">ОГРН</th><td>{company_field("ogrn")}</td></tr>
    </tbody></table></div>
    {map_block()}
  </div>
  <div><h2>Написать нам</h2>{request_form()}</div>
</div>'''
    write_page("/contacts/", "Контакты", "Контакты Witte-rus: телефон, e-mail, адрес и форма обратной связи.", body,
               crumbs=[("Главная", "/"), ("Контакты", "/contacts/")], active="contacts")

    body = f'''<section class="page-head wrap"><h1>Запрос коммерческого предложения</h1><p class="lead">Заполните форму — укажите оборудование, количество и параметры объекта. Поля со звёздочкой обязательны.</p></section>
<div class="wrap two-col two-col-form section-tight"><div class="req-aside"><h2>Что указать в запросе</h2><ul class="ticks"><li>Направление и тип объекта (репродуктор, доращивание, откорм, птичник, коровник)</li><li>Поголовье и количество станков / секций</li><li>Размеры помещений и тип вентиляции</li><li>Перечень моделей из каталога, если уже выбраны</li><li>Регион и желаемые сроки поставки</li></ul><div data-request-product></div></div>{request_form()}</div>'''
    write_page("/request/", "Запрос коммерческого предложения", "Запросите коммерческое предложение на оборудование для свиноводства, птицеводства и КРС.",
               body, crumbs=[("Главная", "/"), ("Запрос КП", "/request/")])

    body = f'''<section class="page-head wrap"><h1>Обработка персональных данных</h1>
<p class="lead">Данные из форм на сайте используются только для ответа на запрос и подготовки коммерческого предложения.</p>
<h2>Оператор персональных данных</h2>
<div class="tbl-wrap"><table class="tbl tbl-kv"><tbody>
<tr><th scope="row">Наименование</th><td>{company_field("legal_name")}</td></tr>
<tr><th scope="row">ИНН / ОГРН</th><td>{company_field("inn")} / {company_field("ogrn")}</td></tr>
<tr><th scope="row">Адрес</th><td>{company_field("legal_address")}</td></tr>
<tr><th scope="row">E-mail для обращений</th><td>{company_field("email")}</td></tr>
</tbody></table></div>
<p class="note">Полный текст политики обработки персональных данных будет опубликован отдельно.</p></section>'''
    write_page("/privacy/", "Обработка персональных данных", "Сведения об обработке персональных данных на сайте Witte-rus.", body,
               crumbs=[("Главная", "/"), ("Обработка персональных данных", "/privacy/")])

    body = '''<section class="page-head wrap"><h1>Поиск по каталогу</h1>
<form class="search-big" action="/search/" role="search"><input type="search" name="q" placeholder="Модель, артикул или название, например: YLC-100, вентилятор, 600×600" aria-label="Поисковый запрос" data-search-input autofocus><button class="btn btn-accent">Найти</button></form>
<p class="muted" data-search-status aria-live="polite"></p></section>
<div class="wrap section-tight"><div class="pgrid" data-search-results></div></div>'''
    write_page("/search/", "Поиск по каталогу", "Поиск оборудования по названию, модели и артикулу.", body,
               crumbs=[("Главная", "/"), ("Поиск", "/search/")], noindex=True)

    body = '''<section class="page-head wrap center"><h1>Страница не найдена</h1><p class="lead">Возможно, ссылка устарела. Воспользуйтесь каталогом или поиском.</p>
<div class="hero-cta center"><a class="btn btn-primary" href="/catalog/">Каталог оборудования</a><a class="btn btn-ghost" href="/search/">Поиск</a></div></section>'''
    write_page("/404.html", "Страница не найдена", "Страница не найдена.", body, noindex=True)


def build_search_index():
    idx = []
    for pid, p in products.items():
        n = NODE[p["cats"][0]]
        idx.append({"id": pid, "n": p["name"], "m": p.get("model", ""), "s": p.get("summary", ""), "u": purl(pid),
                    "i": img_url(p["img"][0]) if p["img"] else "",
                    "p": f'{n["d"]["name"]} · {n["c"]["name"]} · {n["s"]["name"]}',
                    "t": " ".join([p["name"], p.get("model", ""), p.get("summary", ""), " ".join(str(r[0]) for t in p.get("tables", []) for r in t["rows"]),
                                   " ".join(str(v) for _, v in p.get("params", [])), " ".join(p.get("facets", {}).values())])})
    with open(os.path.join(SITE, "assets", "search-index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))


def build_seo_files():
    base = company.get("site_url", "").rstrip("/")
    today = datetime.date.today().isoformat()
    urls = "".join(f"<url><loc>{base}{u}</loc><lastmod>{today}</lastmod></url>" for u in sorted(set(ALL_PAGES)))
    with open(os.path.join(SITE, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n')
    with open(os.path.join(SITE, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(f"User-agent: *\nAllow: /\nDisallow: /search/\nDisallow: /api/\n\nSitemap: {base}/sitemap.xml\n")


def clean_generated():
    for name in ["catalog", "about", "documentation", "contacts", "request", "privacy", "search"]:
        shutil.rmtree(os.path.join(SITE, name), ignore_errors=True)


def main():
    clean_generated()
    build_home()
    build_catalog()
    build_products()
    build_static_pages()
    build_search_index()
    build_seo_files()
    print(f"Товаров: {len(products)}; страниц: {len(ALL_PAGES)}")
    for w in WARN:
        print("ВНИМАНИЕ:", w)
    return 1 if any("неизвестн" in w or "не найден" in w for w in WARN) else 0


if __name__ == "__main__":
    sys.exit(main())
