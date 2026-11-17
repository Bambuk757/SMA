"""
Пульс с камеры телефона и очередь уведомлений для часов (вибромотор ESP32).
Чистая логика без FastAPI, чтобы её можно было проверять отдельно.
"""
import threading
import time
from collections import deque

PULSE_TTL = 90          # сколько секунд пульс с телефона заменяет пульс устройства
BPM_MIN, BPM_MAX = 30, 220
BRIDGE_ALIVE = 6        # мост считается подключённым, если опрашивал очередь не позже, чем N секунд назад


class PhonePulse:
    """Последний пульс с камеры по каждому астронавту."""

    def __init__(self):
        self._data = {}

    def set(self, astronaut, bpm, now=None):
        if not isinstance(bpm, int) or not BPM_MIN <= bpm <= BPM_MAX:
            return False
        self._data[astronaut] = (bpm, (now or time.time()) + PULSE_TTL)
        return True

    def get(self, astronaut, now=None):
        item = self._data.get(astronaut)
        if not item:
            return None
        if (now or time.time()) > item[1]:
            del self._data[astronaut]
            return None
        return item[0]


class Notifier:
    """Очередь уведомлений. Мост забирает новые и отправляет на часы команду NOTIFY (вибрация)."""

    def __init__(self, maxlen=50):
        self._items = deque(maxlen=maxlen)
        self._next = 1
        self._lock = threading.Lock()
        self.last_poll = 0.0

    def push(self, text, level="INFO", astronaut=None):
        with self._lock:
            item = {"id": self._next, "time": time.strftime("%H:%M:%S"), "text": text,
                    "level": level, "astronaut": astronaut}
            self._next += 1
            self._items.append(item)
            return item["id"]

    def last_id(self):
        with self._lock:
            return self._next - 1

    def poll(self, since):
        """since < 0: просто узнать текущий номер (старые уведомления не отдаём)."""
        self.last_poll = time.time()
        with self._lock:
            last = self._next - 1
            if since < 0:
                return last, []
            return last, [i for i in self._items if i["id"] > since]

    def bridge_online(self):
        return time.time() - self.last_poll < BRIDGE_ALIVE
