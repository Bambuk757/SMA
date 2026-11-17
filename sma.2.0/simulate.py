"""
Crew simulator: sends fake measurements to SMA Core (one per second for each astronaut),
so the dashboard can be tested without the ESP32 board.

Run:  py simulate.py
      py simulate.py http://127.0.0.1:8000/api/measurements

Commands (type in this window), N is the astronaut number 1-4:
  alert N       manual alarm          safe N        reset alarm and overrides
  move N        toggle movement       fall N        fall (5 s)
  hr N 130      set pulse             spo2 N 90     set SpO2
  temp N 38.9   set temperature       rad N 160     set radiation (uSv/h)
  phase launch|orbit|deep_space|landing   mission phase for everyone
  off N / on N  stop / resume sending (connection lost)
  help          show this list
"""
import json
import random
import sys
import threading
import time
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/api/measurements"

PHASE_BASE = {"launch": 2.0, "orbit": 20.0, "deep_space": 60.0, "landing": 3.0}
phase = "orbit"

crew = {n: {"hr": hr, "spo2": sp} for n, hr, sp in [(1, 72, 98), (2, 78, 97), (3, 68, 99), (4, 80, 97)]}
for n, c in crew.items():
    c.update(alert=False, moving=False, fall_until=0, online=True, dose=0.0,
             hr_o=None, spo2_o=None, temp_o=None, rad_o=None)


def post(data):
    req = urllib.request.Request(
        URL, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception as e:
        print("Server error:", e)


def sender():
    while True:
        now = time.time()
        for n, c in crew.items():
            if not c["online"]:
                continue
            fall = now < c["fall_until"]
            moving = c["moving"] and not fall
            hr = c["hr_o"] or (c["hr"] + (20 if moving else 0) + random.randint(-3, 3))
            spo2 = c["spo2_o"] or (c["spo2"] + random.randint(-1, 1))
            temp = c["temp_o"] or round(36.6 + random.randint(-2, 2) / 10, 1)
            rad = c["rad_o"] or round(PHASE_BASE[phase] * (1 + random.randint(-10, 10) / 100), 1)
            c["dose"] += rad / 3600 / 1000
            post({
                "astronaut_id": f"AST-00{n}",
                "device_id": f"WATCH-00{n}",
                "timestamp": int(now * 1000),
                "heart_rate": hr,
                "heart_rate_source": "sim",
                "spo2": spo2,
                "temperature": temp,
                "motion": "fall" if fall else ("moving" if moving else "rest"),
                "radiation_usv_h": rad,
                "dose_total_msv": round(c["dose"], 5),
                "mission_phase": phase,
                "manual_alarm": c["alert"] or fall,
                "buffered": False,
            })
        time.sleep(1)


def handle(cmd):
    global phase
    p = cmd.split()
    if not p:
        return
    if p[0] == "help":
        print(__doc__)
        return
    if p[0] == "phase":
        if len(p) > 1 and p[1] in PHASE_BASE:
            phase = p[1]
            print("OK")
        else:
            print("Phases:", ", ".join(PHASE_BASE))
        return
    try:
        c = crew[int(p[1])]
        if p[0] == "alert":
            c["alert"] = True
        elif p[0] == "safe":
            c.update(alert=False, moving=False, fall_until=0, hr_o=None, spo2_o=None, temp_o=None, rad_o=None)
        elif p[0] == "move":
            c["moving"] = not c["moving"]
        elif p[0] == "fall":
            c["fall_until"] = time.time() + 5
        elif p[0] == "hr":
            c["hr_o"] = int(p[2])
        elif p[0] == "spo2":
            c["spo2_o"] = int(p[2])
        elif p[0] == "temp":
            c["temp_o"] = float(p[2])
        elif p[0] == "rad":
            c["rad_o"] = float(p[2])
        elif p[0] == "off":
            c["online"] = False
        elif p[0] == "on":
            c["online"] = True
        else:
            print("Unknown command. Type help")
            return
        print("OK")
    except (IndexError, ValueError, KeyError):
        print("Wrong command. Type help")


if __name__ == "__main__":
    print("Sending to", URL)
    print("Type help for commands")
    threading.Thread(target=sender, daemon=True).start()
    if not sys.stdin or not sys.stdin.isatty():      # запуск из start.py: без консоли, просто работаем
        while True:
            time.sleep(3600)
    while True:
        try:
            handle(input("> ").strip().lower())
        except (EOFError, KeyboardInterrupt):
            break
