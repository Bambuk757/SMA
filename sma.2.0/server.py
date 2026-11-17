"""
Запуск SMA Core сразу на двух портах в ОДНОМ процессе (общие данные):
  http://localhost:8000      - панель на компьютере
  https://<IP>:8443/pulse    - страница для телефона (камера работает только по https)

Запуск:  py server.py [--port 8000] [--https-port 8443] [--no-https]
"""
import argparse
import asyncio
import os

import uvicorn


async def safe_serve(server, name):
    try:
        await server.serve()
    except SystemExit:
        print(f"[SMA] {name}: не удалось запустить (порт занят?)", flush=True)


async def run(servers):
    tasks = [asyncio.create_task(safe_serve(s, n)) for n, s in servers]
    await tasks[0]                       # главный (HTTP) сервер; он ловит Ctrl+C
    for _, s in servers[1:]:
        s.should_exit = True
    await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--https-port", type=int, default=8443)
    ap.add_argument("--no-https", action="store_true")
    args = ap.parse_args()

    os.environ["SMA_HTTP_PORT"] = str(args.port)
    cert = None
    if not args.no_https:
        from certs import ensure_cert
        from main import lan_ip
        cert = ensure_cert(lan_ip())
        if cert:
            os.environ["SMA_HTTPS_PORT"] = str(args.https_port)
        else:
            print("[SMA] Сертификат не создан: страница для телефона будет работать только по http "
                  "(камера на телефоне по http не включится).", flush=True)

    from main import app
    servers = [("HTTP", uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=args.port, log_level="warning")))]
    if cert:
        s = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=args.https_port, log_level="warning",
                                          ssl_certfile=cert[0], ssl_keyfile=cert[1]))
        s.install_signal_handlers = lambda: None      # сигналы обрабатывает первый сервер
        servers.append(("HTTPS", s))

    try:
        asyncio.run(run(servers))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
