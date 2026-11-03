"""Локальный запуск сайта вместе с кабинетом менеджера.

Запуск:  python3 tools/dev_server.py [порт]     (по умолчанию 8080)
Нужно:   pip3 install -r backend/requirements.txt

Заявки сохраняются в data/requests/dev.sqlite, письма не отправляются (если не заданы SMTP_USER/SMTP_PASSWORD),
изменения оборудования записываются в файлы проекта и сайт пересобирается (если не задан GITHUB_TOKEN).
Пароль кабинета для локальной проверки — переменная DEV_MANAGER_PASSWORD (по умолчанию «witte-dev»).
"""
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DEV_STATIC"] = "1"
os.environ.setdefault("MANAGER_PASSWORD", os.environ.get("DEV_MANAGER_PASSWORD", "witte-dev"))
os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"Сайт: http://localhost:{port}/   Кабинет: http://localhost:{port}/manager/  (пароль: {os.environ['MANAGER_PASSWORD']})")
    uvicorn.run("backend.app:app", host="127.0.0.1", port=port, log_level="info")
