"""
SMA - запуск всей системы одной командой:  py start.py

Автоматически:
  1. доустанавливает нужные библиотеки (если их нет);
  2. запускает сервер SMA Core (http://localhost:8000 и https://<IP>:8443 для телефона)
     и ждёт, пока он будет готов; ключ ИИ Gemini берётся из файла .env;
  3. открывает панель управления в браузере и печатает адрес для телефона;
  4. если ESP32 подключён по USB - запускает мост (bridge.py),
     если нет - запускает симулятор экипажа (simulate.py);
  5. следит за процессами: упал - перезапустит; ESP32 воткнули/выдернули -
     сам переключится между мостом и симулятором;
  6. уведомления панели (тревоги, пульс с телефона, кнопка "Тест вибро") мост
     передаёт на ESP32 командой NOTIFY - срабатывает вибромотор.

Параметры:  py start.py --sim   (всегда симулятор)
            py start.py --port 8080 --no-browser
Остановка:  Ctrl+C
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from envfile import load_env

BASE = Path(__file__).parent
PY = sys.executable
load_env()    # GEMINI_API_KEY и др. из .env; дочерние процессы наследуют окружение


def log(*a):
    print(time.strftime("%H:%M:%S"), "[SMA]", *a, flush=True)


def ensure_deps():
    try:
        import fastapi, uvicorn, serial  # noqa: F401
        return
    except ImportError:
        log("Устанавливаю библиотеки из requirements.txt...")
    r = subprocess.run([PY, "-m", "pip", "install", "-r", str(BASE / "requirements.txt")])
    if r.returncode != 0:
        # cryptography нужна только для https; без неё пробуем поставить обязательные пакеты
        core = ["fastapi", "uvicorn", "websockets", "pyserial"]
        r = subprocess.run([PY, "-m", "pip", "install", *core])
        if r.returncode != 0:
            sys.exit("Не удалось установить библиотеки. Проверьте интернет и запустите снова.")


def wait_server(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def spawn(*args):
    return subprocess.Popen([PY, *args], cwd=BASE, stdin=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--sim", action="store_true", help="всегда использовать симулятор")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--https-port", type=int, default=8443)
    ap.add_argument("--no-https", action="store_true", help="не поднимать https для телефона")
    args = ap.parse_args()

    ensure_deps()
    from bridge import find_port

    base = f"http://127.0.0.1:{args.port}"
    server_cmd = ["server.py", "--port", str(args.port), "--https-port", str(args.https_port)]
    if args.no_https:
        server_cmd.append("--no-https")
    server = spawn(*server_cmd)
    if not wait_server(base + "/api/state"):
        server.terminate()
        sys.exit("Сервер не запустился за 30 секунд.")
    log("Сервер запущен:", base)
    try:
        info = json.loads(urllib.request.urlopen(base + "/api/info", timeout=3).read())
        log("Пульс с телефона (телефон в той же Wi-Fi сети):", info["phone_url"])
        if info["https"]:
            log("  телефон покажет предупреждение о сертификате: «Дополнительно» -> «Всё равно перейти»")
    except Exception:
        pass
    from ai import ai_provider
    log("ИИ:", ai_provider() or "офлайн-справочник (задайте GEMINI_API_KEY в файле .env)")
    if not args.no_browser:
        webbrowser.open(base)

    feeder, mode = None, None
    try:
        while True:
            if server.poll() is not None:
                log("Сервер остановился, перезапускаю...")
                server = spawn(*server_cmd)
                wait_server(base + "/api/state")

            want = "sim" if args.sim or not find_port() else "bridge"
            if feeder and (feeder.poll() is not None or want != mode):
                if feeder.poll() is None:
                    feeder.terminate()
                    feeder.wait(timeout=5)
                feeder = None
            if feeder is None:
                mode = want
                if mode == "bridge":
                    log("ESP32 найден - запускаю мост")
                    feeder = spawn("bridge.py", "--url", base + "/api/measurements")
                else:
                    log("ESP32 не найден - запускаю симулятор экипажа")
                    feeder = spawn("simulate.py", base + "/api/measurements")
            time.sleep(3)
    except KeyboardInterrupt:
        log("Остановка...")
    finally:
        for p in (feeder, server):
            if p and p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    main()
