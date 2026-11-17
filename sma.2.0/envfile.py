"""
Загрузка настроек из файла .env рядом с проектом (KEY=VALUE по одной на строку).
Уже заданные переменные окружения НЕ перезаписываются, поэтому
$env:GEMINI_API_KEY="..." в PowerShell по-прежнему имеет приоритет.
"""
import os
from pathlib import Path


def load_env(path=None):
    p = Path(path) if path else Path(__file__).parent / ".env"
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        val = val.strip().strip('"').strip("'")
        if key and val and not os.environ.get(key):
            os.environ[key] = val
