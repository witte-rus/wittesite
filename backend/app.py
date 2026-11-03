"""Серверная часть сайта WITTE: приём заявок, почта, кабинет менеджера.

Работает на Layero как backend полноэкранной сборки (запросы /api/*), локально —
вместе со статикой: `DEV_STATIC=1 python3 -m uvicorn backend.app:app --port 8080`.

Переменные окружения (на Layero: Project → Environment Variables):
  MANAGER_PASSWORD   пароль входа в кабинет менеджера (обязательно)
  SECRET_KEY         случайная строка для подписи сессий (обязательно, 32+ символа)
  SMTP_USER          ящик-отправитель, напр. witte-rus@mail.ru
  SMTP_PASSWORD      пароль для внешних приложений mail.ru (не основной пароль!)
  SMTP_HOST / SMTP_PORT   по умолчанию smtp.mail.ru / 465
  MAIL_TO            куда слать заявки (по умолчанию witte-rus@mail.ru)
  LAYERO_DATABASE_URL  PostgreSQL (выдаёт Layero при подключении базы); без неё — SQLite (только для локальной проверки)
  GITHUB_TOKEN       токен с правом Contents: write на репозиторий — чтобы кабинет сохранял оборудование
  GITHUB_REPO        по умолчанию witte-rus/wittesite;  GITHUB_BRANCH — main
"""
import base64
import datetime as dt
import hashlib
import hmac
import io
import json
import logging
import os
import re
import smtplib
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log = logging.getLogger("witte")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ENV = os.environ.get
MANAGER_PASSWORD = ENV("MANAGER_PASSWORD", "")
SECRET_KEY = ENV("SECRET_KEY", "")
MAIL_TO = ENV("MAIL_TO", "witte-rus@mail.ru")
SMTP_HOST = ENV("SMTP_HOST", "smtp.mail.ru")
SMTP_PORT = int(ENV("SMTP_PORT", "465"))
SMTP_USER = ENV("SMTP_USER", "")
SMTP_PASSWORD = ENV("SMTP_PASSWORD", "")
DB_URL = ENV("LAYERO_DATABASE_URL") or ENV("DATABASE_URL") or ""
GITHUB_TOKEN = ENV("GITHUB_TOKEN", "")
GITHUB_REPO = ENV("GITHUB_REPO", "witte-rus/wittesite")
GITHUB_BRANCH = ENV("GITHUB_BRANCH", "main")
DEV_STATIC = ENV("DEV_STATIC", "") == "1"
SESSION_TTL = 12 * 3600
COOKIE = "witte_mgr"

REQUEST_FIELDS = ["name", "company", "phone", "email", "region", "direction", "products", "comment", "page"]
STATUSES = {"new": "Новая", "work": "В работе", "offer": "КП отправлено", "done": "Закрыта", "spam": "Спам"}


# ------------------------------------------------------------------ хранилище заявок
class Store:
    """PostgreSQL на Layero, SQLite — для локальной проверки."""

    def __init__(self):
        self.pg = bool(DB_URL)
        self.lock = threading.Lock()
        if self.pg:
            import psycopg  # noqa: F401
        else:
            path = ENV("SQLITE_PATH") or os.path.join(ROOT, "data", "requests", "dev.sqlite")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.sqlite_path = path
        self._init()

    def _conn(self):
        if self.pg:
            import psycopg
            return psycopg.connect(DB_URL, connect_timeout=10)
        c = sqlite3.connect(self.sqlite_path)
        return c

    def _init(self):
        sql = """CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, created TEXT NOT NULL, name TEXT, company TEXT, phone TEXT, email TEXT,
            region TEXT, direction TEXT, products TEXT, comment TEXT, page TEXT,
            status TEXT NOT NULL DEFAULT 'new', note TEXT DEFAULT '', mailed TEXT DEFAULT '')"""
        with self.lock, self._conn() as c:
            c.execute(sql)

    def _ph(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def add(self, rec: Dict):
        cols = ["id", "created"] + REQUEST_FIELDS
        sql = self._ph(f"INSERT INTO requests ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})")
        with self.lock, self._conn() as c:
            c.execute(sql, [rec[k] for k in cols])

    def set_mailed(self, rid: str, value: str):
        with self.lock, self._conn() as c:
            c.execute(self._ph("UPDATE requests SET mailed=? WHERE id=?"), (value, rid))

    def list(self, status: Optional[str], q: Optional[str], limit: int = 500) -> List[Dict]:
        sql, args = "SELECT id, created, name, company, phone, email, region, direction, products, comment, page, status, note, mailed FROM requests", []
        where = []
        if status:
            where.append("status=?")
            args.append(status)
        if q:
            where.append("(LOWER(name) LIKE ? OR LOWER(phone) LIKE ? OR LOWER(email) LIKE ? OR LOWER(company) LIKE ? OR LOWER(products) LIKE ?)")
            args += [f"%{q.lower()}%"] * 5
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created DESC LIMIT " + str(int(limit))
        with self.lock, self._conn() as c:
            cur = c.execute(self._ph(sql), args)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def update(self, rid: str, status: Optional[str], note: Optional[str]) -> bool:
        sets, args = [], []
        if status is not None:
            sets.append("status=?")
            args.append(status)
        if note is not None:
            sets.append("note=?")
            args.append(note)
        if not sets:
            return True
        with self.lock, self._conn() as c:
            cur = c.execute(self._ph(f"UPDATE requests SET {', '.join(sets)} WHERE id=?"), args + [rid])
            return cur.rowcount > 0

    def counts(self) -> Dict[str, int]:
        with self.lock, self._conn() as c:
            cur = c.execute("SELECT status, COUNT(*) FROM requests GROUP BY status")
            return {s: n for s, n in cur.fetchall()}


try:
    store = Store()
    STORE_ERROR = ""
except Exception as e:  # база недоступна — заявки всё равно уходят на почту
    log.exception("База заявок недоступна")
    store, STORE_ERROR = None, str(e)


# ------------------------------------------------------------------ почта
def send_mail(rec: Dict) -> str:
    if not (SMTP_USER and SMTP_PASSWORD):
        log.warning("SMTP не настроен — письмо по заявке %s не отправлено", rec["id"])
        return "smtp-not-configured"
    msg = EmailMessage()
    msg["Subject"] = f"Заявка с сайта № {rec['id']}: {rec['name']}"
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO
    if rec.get("email"):
        msg["Reply-To"] = rec["email"]
    labels = {"name": "Имя", "company": "Компания", "phone": "Телефон", "email": "E-mail", "region": "Регион",
              "direction": "Направление", "products": "Оборудование", "comment": "Комментарий", "page": "Страница"}
    body = [f"Новая заявка с сайта WITTE № {rec['id']} от {rec['created']}", ""]
    body += [f"{labels[k]}: {rec[k]}" for k in REQUEST_FIELDS if rec.get(k)]
    body += ["", "Заявка сохранена в кабинете менеджера: /manager/"]
    msg.set_content("\n".join(body))
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        return "sent"
    except Exception as e:
        log.exception("Не удалось отправить письмо по заявке %s", rec["id"])
        return "error: " + str(e)[:200]


def mail_and_mark(rec: Dict):
    status = send_mail(rec)
    if store:
        try:
            store.set_mailed(rec["id"], status)
        except Exception:
            log.exception("Не удалось сохранить статус письма")


# ------------------------------------------------------------------ сессии менеджера
def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    payload = str(int(time.time()) + SESSION_TTL)
    return payload + "." + _sign(payload)


def check_token(tok: Optional[str]) -> bool:
    if not tok or not SECRET_KEY or "." not in tok:
        return False
    payload, sig = tok.split(".", 1)
    return hmac.compare_digest(sig, _sign(payload)) and payload.isdigit() and int(payload) > time.time()


def require_manager(request: Request):
    if not (MANAGER_PASSWORD and SECRET_KEY):
        raise HTTPException(503, "Кабинет не настроен: задайте MANAGER_PASSWORD и SECRET_KEY")
    if not check_token(request.cookies.get(COOKIE)):
        raise HTTPException(401, "Требуется вход")


def same_origin(request: Request):
    """Защита от подделки запросов: изменяющие запросы — только со своего сайта."""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if origin and host and urllib.parse.urlparse(origin).netloc != host:
        raise HTTPException(403, "Запрос с чужого сайта")


_attempts: Dict[str, List[float]] = {}


def rate_limit(key: str, limit: int, window: int):
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < window]
    if len(hits) >= limit:
        raise HTTPException(429, "Слишком много попыток, попробуйте через минуту")
    hits.append(now)
    _attempts[key] = hits


def client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()


# ------------------------------------------------------------------ репозиторий (каталог оборудования)
class LocalRepo:
    """Локальная проверка: пишет файлы в рабочую папку и пересобирает сайт."""
    name = "local"

    def read(self, path: str) -> Optional[bytes]:
        p = os.path.join(ROOT, path)
        return open(p, "rb").read() if os.path.exists(p) else None

    def list_dir(self, path: str) -> List[str]:
        p = os.path.join(ROOT, path)
        return sorted(f for f in os.listdir(p)) if os.path.isdir(p) else []

    def commit(self, files: Dict[str, Optional[bytes]], message: str) -> str:
        for path, data in files.items():
            p = os.path.join(ROOT, path)
            if data is None:
                if os.path.exists(p):
                    os.remove(p)
                continue
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build.py")], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise HTTPException(500, "Сборка сайта завершилась с ошибкой: " + (r.stdout + r.stderr)[-800:])
        return "local"


class GitHubRepo:
    """Публикация: коммит в репозиторий, Layero пересобирает сайт по вебхуку."""
    name = "github"

    def _api(self, method: str, path: str, body=None):
        req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "witte-manager"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise HTTPException(502, f"GitHub ответил {e.code}: {e.read()[:300].decode('utf-8', 'ignore')}")

    def read(self, path: str) -> Optional[bytes]:
        d = self._api("GET", f"/contents/{urllib.parse.quote(path)}?ref={GITHUB_BRANCH}")
        if not d or isinstance(d, list):
            return None
        if d.get("encoding") == "base64" and d.get("content"):
            return base64.b64decode(d["content"])
        blob = self._api("GET", f"/git/blobs/{d['sha']}")
        return base64.b64decode(blob["content"])

    def list_dir(self, path: str) -> List[str]:
        d = self._api("GET", f"/contents/{urllib.parse.quote(path)}?ref={GITHUB_BRANCH}")
        return sorted(x["name"] for x in d) if isinstance(d, list) else []

    def _rebuild(self, files: Dict[str, Optional[bytes]]) -> Dict[str, Optional[bytes]]:
        """Пересобирает страницы в рабочей копии контейнера и добавляет изменённые файлы site/ в коммит.

        На Layero сборщик статики работает без Python, поэтому готовые страницы собирает сервер:
        данные берутся из GitHub (последняя версия), сайт генерируется tools/build.py.
        """
        def put(path: str, data: Optional[bytes]):
            p = os.path.join(ROOT, path)
            if data is None:
                if os.path.exists(p):
                    os.remove(p)
                return
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)

        # 1. актуальные данные из репозитория + новые изменения
        remote_products = [n for n in self.list_dir(PRODUCTS_DIR) if n.endswith(".json")]
        for n in os.listdir(os.path.join(ROOT, PRODUCTS_DIR)):
            if n.endswith(".json") and n not in remote_products:
                put(f"{PRODUCTS_DIR}/{n}", None)
        for path in ["data/catalog.json", "data/documents.json", "data/company.json"] + [f"{PRODUCTS_DIR}/{n}" for n in remote_products]:
            if path not in files:
                raw = self.read(path)
                if raw is not None:
                    put(path, raw)
        for path, data in files.items():
            put(path, data)

        def snapshot() -> Dict[str, str]:
            out = {}
            for base, dirs, names in os.walk(os.path.join(ROOT, "site")):
                rel = os.path.relpath(base, ROOT)
                if rel.startswith(os.path.join("site", "docs")) or rel.startswith(os.path.join("site", "assets", "img")):
                    continue
                for n in names:
                    if n.endswith((".html", ".xml", ".txt", ".json")):
                        p = os.path.join(base, n)
                        out[os.path.relpath(p, ROOT).replace(os.sep, "/")] = hashlib.sha1(open(p, "rb").read()).hexdigest()
            return out

        def build() -> str:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build.py")], capture_output=True, text=True, timeout=300, cwd=ROOT)
            if r.returncode != 0:
                raise HTTPException(500, "Сборка сайта завершилась с ошибкой: " + (r.stdout + r.stderr)[-800:])
            return r.stdout

        before = snapshot()
        out = build()
        # 2. фото, добавленные после последнего деплоя контейнера, докачиваем из репозитория
        missing = re.findall(r"Нет изображения: ([a-z0-9-]+)", out)
        if missing:
            for name in set(missing):
                raw = self.read(f"{IMG_DIR}/{name}.jpg")
                if raw:
                    put(f"{IMG_DIR}/{name}.jpg", raw)
            build()
        after = snapshot()
        result = dict(files)
        for path, h in after.items():
            if before.get(path) != h:
                result[path] = open(os.path.join(ROOT, path), "rb").read()
        for path in before:
            if path not in after:
                result[path] = None
        return result

    def commit(self, files: Dict[str, Optional[bytes]], message: str) -> str:
        files = self._rebuild(files)
        ref = self._api("GET", f"/git/ref/heads/{GITHUB_BRANCH}")
        head = ref["object"]["sha"]
        base_tree = self._api("GET", f"/git/commits/{head}")["tree"]["sha"]
        tree = []
        for path, data in files.items():
            if data is None:
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            if path.endswith((".html", ".json", ".xml", ".txt", ".md")):
                try:  # текстовые файлы — прямо в дереве, одним запросом
                    tree.append({"path": path, "mode": "100644", "type": "blob", "content": data.decode("utf-8")})
                    continue
                except UnicodeDecodeError:
                    pass
            blob = self._api("POST", "/git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"})
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self._api("POST", "/git/trees", {"base_tree": base_tree, "tree": tree})
        commit = self._api("POST", "/git/commits", {"message": message, "tree": new_tree["sha"], "parents": [head]})
        self._api("PATCH", f"/git/refs/heads/{GITHUB_BRANCH}", {"sha": commit["sha"]})
        return commit["sha"][:7]


repo = GitHubRepo() if GITHUB_TOKEN else LocalRepo()
repo_lock = threading.Lock()

PRODUCTS_DIR = "data/products"
MANAGER_FILE = "data/products/manager.json"
IMG_DIR = "site/assets/img/p"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")


def load_json(path: str, default):
    raw = repo.read(path)
    return json.loads(raw.decode("utf-8")) if raw else default


def all_product_files() -> Dict[str, list]:
    return {f"{PRODUCTS_DIR}/{n}": load_json(f"{PRODUCTS_DIR}/{n}", []) for n in repo.list_dir(PRODUCTS_DIR) if n.endswith(".json")}


def catalog_paths() -> List[str]:
    cat = load_json("data/catalog.json", {"directions": []})
    out = []
    for d in cat["directions"]:
        for c in d["categories"]:
            for s in c["subcategories"]:
                out.append(f"{d['slug']}/{c['slug']}/{s['slug']}")
    return out


def dump(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def clean_text(v, limit=4000) -> str:
    return str(v or "").strip()[:limit]


def clean_list(v, limit=60) -> List[str]:
    return [clean_text(x, 600) for x in (v or []) if clean_text(x, 600)][:limit]


def clean_pairs(v) -> List[List[str]]:
    out = []
    for row in v or []:
        if isinstance(row, (list, tuple)) and len(row) == 2 and clean_text(row[0]) and clean_text(row[1]):
            out.append([clean_text(row[0], 200), clean_text(row[1], 600)])
    return out[:80]


def normalize_product(p: Dict, valid_paths: List[str], valid_ids: List[str]) -> Dict:
    pid = clean_text(p.get("id"), 80).lower()
    if not ID_RE.match(pid):
        raise HTTPException(422, "Код (id) — латиница в нижнем регистре, цифры и дефис, от 2 символов")
    name = clean_text(p.get("name"), 300)
    if not name:
        raise HTTPException(422, "Укажите название")
    cats = [c for c in (p.get("cats") or []) if c in valid_paths]
    if not cats:
        raise HTTPException(422, "Выберите хотя бы один раздел каталога")
    out = {"id": pid, "name": name, "model": clean_text(p.get("model"), 300), "cats": cats,
           "img": [i for i in (p.get("img") or []) if re.match(r"^[a-z0-9-]+$", str(i))][:20],
           "summary": clean_text(p.get("summary"), 400)}
    for k in ["description", "purpose", "variants", "maker"]:
        if clean_text(p.get(k)):
            out[k] = clean_text(p.get(k))
    for k in ["features", "kit", "missing"]:
        if clean_list(p.get(k)):
            out[k] = clean_list(p.get(k))
    if clean_pairs(p.get("params")):
        out["params"] = clean_pairs(p.get("params"))
    tables = []
    for t in p.get("tables") or []:
        cols = clean_list(t.get("cols"), 20)
        rows = [[clean_text(c, 300) for c in r][:len(cols)] for r in (t.get("rows") or []) if any(clean_text(c) for c in r)]
        if cols and rows:
            rows = [r + [""] * (len(cols) - len(r)) for r in rows][:200]
            tables.append({"title": clean_text(t.get("title"), 200), "cols": cols, "rows": rows})
    if tables:
        out["tables"] = tables[:6]
    docs = [{"doc": clean_text(d.get("doc"), 40), "pages": clean_text(d.get("pages"), 40)} for d in (p.get("docs") or []) if clean_text(d.get("doc"))]
    if docs:
        out["docs"] = docs[:10]
    related = [r for r in (p.get("related") or []) if r in valid_ids and r != pid]
    if related:
        out["related"] = related[:12]
    facets = {k: v for k, v in clean_pairs([[k, v] for k, v in (p.get("facets") or {}).items()])}
    if facets:
        out["facets"] = facets
    air = p.get("airflow")
    if isinstance(air, list) and len(air) == 2 and all(str(x).strip().isdigit() for x in air):
        lo, hi = sorted(int(x) for x in air)
        out["airflow"] = [lo, hi]
    return out


def image_to_jpeg(data: bytes) -> bytes:
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    im = im.convert("RGB")
    im.thumbnail((1400, 1400))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85, optimize=True, progressive=True)
    return buf.getvalue()


# ------------------------------------------------------------------ приложение
app = FastAPI(title="WITTE backend", docs_url=None, redoc_url=None, openapi_url=None)
api = APIRouter()


@api.get("/health")
def health():
    return {"ok": True}


@api.post("/request")
async def create_request(request: Request, bg: BackgroundTasks):
    rate_limit("req:" + client_ip(request), 6, 600)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Некорректные данные")
    if not isinstance(data, dict):
        raise HTTPException(400, "Некорректные данные")
    if data.get("website"):  # ловушка для ботов
        return {"id": "0"}
    rec = {k: clean_text(data.get(k), 2000) for k in REQUEST_FIELDS}
    if not rec["name"] or len(re.sub(r"\D", "", rec["phone"])) < 10 or not data.get("consent"):
        raise HTTPException(422, "Заполните имя, телефон и подтвердите согласие.")
    rec["id"] = dt.datetime.now().strftime("%y%m%d") + "-" + uuid.uuid4().hex[:5].upper()
    rec["created"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=5))).strftime("%Y-%m-%d %H:%M")
    saved = False
    if store:
        try:
            store.add(rec)
            saved = True
        except Exception:
            log.exception("Не удалось сохранить заявку в базу")
    if not saved and not (SMTP_USER and SMTP_PASSWORD):
        raise HTTPException(503, "Приём заявок временно недоступен. Напишите нам на " + MAIL_TO)
    if saved:
        bg.add_task(mail_and_mark, rec)
    else:
        status = send_mail(rec)
        if status != "sent":
            raise HTTPException(503, "Не удалось отправить заявку. Напишите нам на " + MAIL_TO)
    log.info("Заявка %s: %s %s", rec["id"], rec["name"], rec["phone"])
    return {"id": rec["id"]}


@api.post("/manager/login")
async def login(request: Request, response: Response):
    same_origin(request)
    rate_limit("login:" + client_ip(request), 5, 60)
    if not (MANAGER_PASSWORD and SECRET_KEY):
        raise HTTPException(503, "Кабинет не настроен: задайте MANAGER_PASSWORD и SECRET_KEY")
    data = await request.json()
    if not hmac.compare_digest(str(data.get("password", "")).encode(), MANAGER_PASSWORD.encode()):
        raise HTTPException(401, "Неверный пароль")
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, make_token(), max_age=SESSION_TTL, httponly=True, secure=secure, samesite="strict", path="/")
    return {"ok": True}


@api.post("/manager/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@api.get("/manager/status")
def status(request: Request):
    require_manager(request)
    return {"db": ("postgres" if store and store.pg else "sqlite") if store else "нет: " + STORE_ERROR,
            "db_persistent": bool(store and store.pg), "smtp": bool(SMTP_USER and SMTP_PASSWORD), "mail_to": MAIL_TO,
            "repo": repo.name, "github_repo": GITHUB_REPO if GITHUB_TOKEN else "", "statuses": STATUSES}


@api.get("/manager/requests")
def list_requests(request: Request, status: Optional[str] = None, q: Optional[str] = None):
    require_manager(request)
    if not store:
        raise HTTPException(503, "База заявок не подключена — заявки приходят только на почту " + MAIL_TO)
    return {"items": store.list(status if status in STATUSES else None, (q or "").strip()[:100] or None), "counts": store.counts()}


@api.patch("/manager/requests/{rid}")
async def update_request(rid: str, request: Request):
    require_manager(request)
    same_origin(request)
    if not store:
        raise HTTPException(503, "База заявок не подключена")
    data = await request.json()
    st = data.get("status")
    if st is not None and st not in STATUSES:
        raise HTTPException(422, "Неизвестный статус")
    note = clean_text(data["note"], 2000) if "note" in data else None
    if not store.update(rid, st, note):
        raise HTTPException(404, "Заявка не найдена")
    return {"ok": True}


@api.get("/manager/requests.csv")
def export_csv(request: Request):
    require_manager(request)
    if not store:
        raise HTTPException(503, "База заявок не подключена")
    import csv
    buf = io.StringIO()
    cols = ["id", "created", "status", "name", "company", "phone", "email", "region", "direction", "products", "comment", "note", "page"]
    w = csv.writer(buf, delimiter=";")
    w.writerow(cols)
    for r in store.list(None, None, 10000):
        w.writerow([STATUSES.get(r.get(c), r.get(c)) if c == "status" else r.get(c, "") for c in cols])
    return PlainTextResponse("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=zayavki.csv"})


@api.get("/manager/products")
def list_products(request: Request):
    require_manager(request)
    files = all_product_files()
    items = []
    for path, entries in files.items():
        for e in entries:
            if "series" in e:
                for it in e["items"]:
                    items.append({"id": it["id"], "name": it.get("name") or e.get("name_tpl", "{model}").format(model=it["model"], name=it.get("name", "")),
                                  "model": it["model"], "file": path, "editable": False, "series": e["title"]})
            else:
                items.append({"id": e["id"], "name": e["name"], "model": e.get("model", ""), "file": path, "editable": True, "product": e})
    return {"items": items, "catalog": load_json("data/catalog.json", {}), "documents": load_json("data/documents.json", {}).get("items", [])}


@api.post("/manager/products")
async def save_product(request: Request):
    require_manager(request)
    same_origin(request)
    data = await request.json()
    original_id = clean_text(data.get("original_id"), 80) or None
    images = data.get("new_images") or []
    if len(images) > 12:
        raise HTTPException(422, "Не больше 12 новых фото за раз")
    with repo_lock:
        files = all_product_files()
        ids, where = [], {}
        for path, entries in files.items():
            for i, e in enumerate(entries):
                for pid in ([x["id"] for x in e["items"]] if "series" in e else [e["id"]]):
                    ids.append(pid)
                    where[pid] = (path, i, "series" in e)
        prod = normalize_product(data.get("product") or {}, catalog_paths(), ids)
        pid = prod["id"]
        if original_id and original_id not in where:
            raise HTTPException(404, "Изделие не найдено — обновите страницу")
        if original_id and where[original_id][2]:
            raise HTTPException(422, "Модели серий редактируются в файлах данных")
        if pid in where and pid != original_id:
            raise HTTPException(409, "Изделие с таким кодом уже есть")
        changes: Dict[str, Optional[bytes]] = {}
        existing = set(repo.list_dir(IMG_DIR))
        n = 1
        for img in images:
            raw = img.get("data", "")
            if "," in raw:
                raw = raw.split(",", 1)[1]
            try:
                blob = base64.b64decode(raw, validate=True)
            except Exception:
                raise HTTPException(422, "Не удалось прочитать фото")
            if len(blob) > 15 * 1024 * 1024:
                raise HTTPException(422, "Фото больше 15 МБ")
            try:
                jpg = image_to_jpeg(blob)
            except Exception:
                raise HTTPException(422, f"Файл «{clean_text(img.get('name'), 80)}» не является изображением")
            while f"{pid}-{n}.jpg" in existing:
                n += 1
            name = f"{pid}-{n}"
            existing.add(name + ".jpg")
            changes[f"{IMG_DIR}/{name}.jpg"] = jpg
            prod["img"].append(name)
        if original_id:
            path, idx, _ = where[original_id]
            files[path][idx] = prod
        else:
            path = MANAGER_FILE
            files.setdefault(path, [])
            files[path].append(prod)
        changes[path] = dump(files[path])
        if original_id and original_id != pid:  # смена кода: ссылки «сопутствующие» на старый код
            for fpath, entries in files.items():
                touched = False
                for e in entries:
                    if "related" in e and original_id in e["related"]:
                        e["related"] = [pid if r == original_id else r for r in e["related"]]
                        touched = True
                if touched:
                    changes[fpath] = dump(entries)
        ref = repo.commit(changes, f"Кабинет менеджера: {'изменено' if original_id else 'добавлено'} «{prod['name']}» ({pid})")
    return {"ok": True, "id": pid, "commit": ref, "mode": repo.name}


@api.delete("/manager/products/{pid}")
def delete_product(pid: str, request: Request):
    require_manager(request)
    same_origin(request)
    with repo_lock:
        files = all_product_files()
        for path, entries in files.items():
            for i, e in enumerate(entries):
                if "series" not in e and e["id"] == pid:
                    del entries[i]
                    changes = {path: dump(entries)}
                    for fpath, other in files.items():
                        touched = False
                        for o in other:
                            if "related" in o and pid in o["related"]:
                                o["related"] = [r for r in o["related"] if r != pid]
                                touched = True
                        if touched:
                            changes[fpath] = dump(other)
                    ref = repo.commit(changes, f"Кабинет менеджера: удалено изделие {pid}")
                    return {"ok": True, "commit": ref, "mode": repo.name}
    raise HTTPException(404, "Изделие не найдено или входит в серию")


# Layero может передавать запросы с префиксом /api или без него — принимаем оба варианта.
app.include_router(api, prefix="/api")
if not DEV_STATIC:
    app.include_router(api)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


if DEV_STATIC:  # локальная проверка: отдаём собранный сайт
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=os.path.join(ROOT, "site"), html=True), name="site")
