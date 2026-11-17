"""
SMA Bridge: автоматический мост ESP32 (USB) -> SMA Core (HTTP).

Что делает сам, без настройки:
  * находит COM-порт ESP32 (CP210x / CH340 / FTDI / Espressif);
  * переподключается, если кабель выдернули или плата перезагрузилась;
  * отправляет каждое измерение на сервер (POST /api/measurements);
  * если сервер недоступен - копит измерения и досылает их позже;
  * передаёт на часы тревогу сервера (ALARM 1 / ALARM 0);
  * забирает уведомления сайта (/api/notify/poll) и шлёт на часы NOTIFY - вибромотор;
  * шлёт PING каждые 2 секунды, чтобы часы считали связь живой.

Запуск:  py bridge.py
Опции:   py bridge.py --port COM5 --url http://127.0.0.1:8000/api/measurements
"""
import argparse
import json
import sys
import time
import urllib.request
from collections import deque

import serial
from serial.tools import list_ports

DEFAULT_URL = "http://127.0.0.1:8000/api/measurements"
BAUD = 115200
PING_EVERY = 2.0
NOTIFY_POLL_EVERY = 1.0
RECONNECT_DELAY = 3.0
MAX_BACKLOG = 5000

ESP_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A}   # CP210x, CH340, FTDI, Espressif
ESP_WORDS = ("cp210", "ch340", "ch910", "ftdi", "esp32", "silicon labs", "usb serial", "usb-serial")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def find_port():
    """Возвращает имя порта ESP32 или None."""
    for p in list_ports.comports():
        text = f"{p.description} {p.manufacturer} {p.hwid}".lower()
        if p.vid in ESP_VIDS or any(w in text for w in ESP_WORDS):
            return p.device
    return None


def post(url, data):
    """Отправка на сервер. Возвращает ответ (dict) или None, если сервер недоступен."""
    req = urllib.request.Request(
        url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return None


def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return None


def send(ser, cmd):
    ser.write((cmd + "\n").encode())
    ser.flush()


class Bridge:
    def __init__(self, url, port=None):
        self.url = url
        self.fixed_port = port
        self.backlog = deque(maxlen=MAX_BACKLOG)
        self.server_ok = None
        self.last_alarm = None
        self.sent = 0
        self.notify_url = url.split("/api/")[0] + "/api/notify/poll"
        self.notify_since = -1           # -1: при старте старые уведомления не вибрируем
        self.next_poll = 0.0

    def set_server(self, ok):
        if ok != self.server_ok:
            log("SERVER:", "подключен" if ok else "недоступен, измерения копятся")
            self.server_ok = ok

    def deliver(self, ser, data):
        """Сначала досылаем накопленное, затем текущее измерение."""
        self.backlog.append(data)
        while self.backlog:
            item = self.backlog[0]
            resp = post(self.url, item)
            if resp is None:
                self.set_server(False)
                return
            self.set_server(True)
            self.backlog.popleft()
            self.sent += 1
            if item is data:
                self.sync_alarm(ser, resp.get("alarm"))
                log(f"#{self.sent} {data.get('astronaut_id')} HR={data.get('heart_rate')} "
                    f"SpO2={data.get('spo2')} T={data.get('temperature')} "
                    f"{data.get('motion')} -> {resp.get('status', '?')}")

    def sync_alarm(self, ser, alarm):
        """Тревога сервера -> часы (светодиод и зуммер)."""
        if alarm is None or alarm == self.last_alarm:
            return
        send(ser, "ALARM 1" if alarm else "ALARM 0")
        self.last_alarm = alarm

    def poll_notifications(self, ser):
        """Новые уведомления сайта -> вибромотор на часах (команда NOTIFY)."""
        if time.time() < self.next_poll:
            return
        d = get_json(f"{self.notify_url}?since={self.notify_since}")
        if d is None:
            self.next_poll = time.time() + 3.0      # сервер недоступен - не долбим его каждую секунду
            return
        self.next_poll = time.time() + NOTIFY_POLL_EVERY
        last = d.get("last", self.notify_since)
        if last < self.notify_since:                # сервер перезапущен, нумерация началась заново
            self.notify_since = -1
            return
        self.notify_since = last
        for item in d.get("items", []):
            log("УВЕДОМЛЕНИЕ -> вибромотор:", item.get("text"))
        if d.get("items"):
            send(ser, "NOTIFY")

    def run_port(self, port):
        with serial.Serial(port, BAUD, timeout=0.2, write_timeout=1) as ser:
            time.sleep(2)                    # ESP32 перезагружается при открытии порта
            ser.reset_input_buffer()
            log("ESP32 ПОДКЛЮЧЕН:", port)
            self.last_alarm = None           # после перезагрузки платы состояние неизвестно
            last_ping = 0.0
            while True:
                self.poll_notifications(ser)
                if time.time() - last_ping >= PING_EVERY:
                    send(ser, "PING")
                    last_ping = time.time()
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue             # обрезанная строка - пропускаем
                    self.deliver(ser, data)
                elif "PONG" not in line:
                    log("[ESP32]", line)

    def run(self):
        log("SMA BRIDGE запущен. Сервер:", self.url)
        waiting_logged = False
        while True:
            port = self.fixed_port or find_port()
            if not port:
                if not waiting_logged:
                    log("ESP32 не найден, жду подключения кабеля...")
                    waiting_logged = True
                time.sleep(RECONNECT_DELAY)
                continue
            waiting_logged = False
            try:
                self.run_port(port)
            except (serial.SerialException, OSError) as e:
                log("СВЯЗЬ С ESP32 ПОТЕРЯНА:", e)
                time.sleep(RECONNECT_DELAY)


def main():
    ap = argparse.ArgumentParser(description="SMA Bridge: ESP32 -> SMA Core")
    ap.add_argument("--port", help="COM-порт (по умолчанию определяется автоматически)")
    ap.add_argument("--url", default=DEFAULT_URL, help="адрес API сервера")
    args = ap.parse_args()
    try:
        Bridge(args.url, args.port).run()
    except KeyboardInterrupt:
        log("SMA BRIDGE остановлен")
        sys.exit(0)


if __name__ == "__main__":
    main()
