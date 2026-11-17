"""
SMA Core - SPACE MEDICAL ASSISTENT
Server: receives measurements from the watch, analyses them, stores history in SQLite,
pushes live updates to the dashboard (WebSocket) and answers voice questions.

Run:  py -m uvicorn main:app --host 0.0.0.0 --port 8000
Open: http://localhost:8000
"""
import asyncio
import os
import re
import socket
import sqlite3
import statistics
import threading
import time
from collections import deque
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ai import (ai_provider, ask_ai, condition_text, conditions_list,
                find_condition)
from phone_notify import Notifier, PhonePulse

app = FastAPI(title="SMA Core")
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "sma.db"

LINK_TIMEOUT = 10          # seconds without data = Connection Lost
DEFAULT_BASELINE = 72      # resting pulse until the personal baseline is learned
SPO2_WARN, SPO2_CRIT = 94, 90
TEMP_HI_WARN, TEMP_HI_CRIT, TEMP_LO_WARN = 37.8, 38.5, 35.5

# Radiation limits (uSv/h) by mission phase: (warning, critical). Conditional values for the demo.
RAD_LIMITS = {
    "launch": (10, 30),
    "orbit": (50, 150),
    "deep_space": (150, 400),
    "landing": (10, 30),
}

PHASE_RU = {"launch": "старт", "orbit": "орбита", "deep_space": "дальний космос", "landing": "посадка"}
RANK = {"NORMAL": 0, "WARNING": 1, "CRITICAL": 2}

crew = {}                    # astronaut id -> current state (in memory)
events = deque(maxlen=30)    # event log for the dashboard
sim_flags = {}               # demo events: key (astronaut id or "*") -> {"flare": until, ...}
phone_pulse = PhonePulse()   # pulse measured by the phone camera (replaces the device pulse for a while)
notifier = Notifier()        # notifications for the watch (vibration motor on the ESP32)


# ---------- Database ----------

db_lock = threading.Lock()


def db(sql, args=(), fetch=False):
    with db_lock, closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute(sql, args)
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows


db("CREATE TABLE IF NOT EXISTS astronauts (id TEXT PRIMARY KEY, name TEXT, device_id TEXT, hr_baseline REAL)")
db("""CREATE TABLE IF NOT EXISTS measurements (
        id INTEGER PRIMARY KEY AUTOINCREMENT, astronaut_id TEXT, ts REAL, heart_rate INTEGER,
        hr_source TEXT, spo2 INTEGER, temperature REAL, motion TEXT, radiation REAL,
        dose_total REAL, phase TEXT, status TEXT)""")
db("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, astronaut_id TEXT, ts REAL, level TEXT, text TEXT)")
db("CREATE INDEX IF NOT EXISTS idx_meas ON measurements (astronaut_id, ts)")


# ---------- Models ----------

class Measurement(BaseModel):
    astronaut_id: str = "AST-001"
    device_id: str = "WATCH-001"
    timestamp: int = 0
    heart_rate: int
    heart_rate_source: str = "sim"       # camera, button or sim
    spo2: int = 98                       # simulated
    temperature: Optional[float] = None  # simulated
    motion: str = "rest"                 # rest, moving, fall
    radiation_usv_h: float = 0.0         # simulated
    dose_total_msv: float = 0.0          # simulated
    mission_phase: str = "orbit"         # launch, orbit, deep_space, landing
    manual_alarm: bool = False           # button on the watch
    buffered: bool = False


class PulseIn(BaseModel):
    bpm: int
    astronaut_id: str = "AST-001"
    source: str = "camera-ppg"
    final: bool = True                   # final result (True) or a live value during measurement
    ts: Optional[str] = None


class NotifyIn(BaseModel):
    text: str = "Уведомление с панели SMA"
    level: str = "INFO"


class SimEvent(BaseModel):
    event: str                # solar_flare, fall, connection_lost, clear
    astronaut: str = "all"


class VoiceQuery(BaseModel):
    text: str


# ---------- Demo events ----------

def flag(a, kind):
    now = time.time()
    return max(sim_flags.get(a, {}).get(kind, 0), sim_flags.get("*", {}).get(kind, 0)) > now


# ---------- Analysis (rules and statistics) ----------

def reasons(c):
    """List of (code, text, level): level 1 = warning, 2 = critical."""
    out = []
    hr, base = c["hr"], c["baseline"]
    moving = c["motion"] == "moving"
    where = "при движении" if moving else "в покое"

    if c["motion"] == "fall":
        out.append(("fall", "обнаружено падение", 2))

    # Pulse relative to the personal baseline; the limit is higher during movement
    warn_hi, crit_hi = (2.0, 2.4) if moving else (1.3, 1.6)
    if hr > crit_hi * base:
        out.append(("hr_high", f"критически высокий пульс {where} ({hr} уд/мин, ваша норма около {base:.0f})", 2))
    elif hr > warn_hi * base:
        out.append(("hr_high", f"высокий пульс {where} ({hr} уд/мин, ваша норма около {base:.0f})", 1))
    if hr < 0.55 * base:
        out.append(("hr_low", f"критически низкий пульс ({hr} уд/мин)", 2))
    elif hr < 0.7 * base:
        out.append(("hr_low", f"низкий пульс ({hr} уд/мин)", 1))

    if c["spo2"] < SPO2_CRIT:
        out.append(("spo2_low", f"критически низкий SpO2 ({c['spo2']}%)", 2))
    elif c["spo2"] < SPO2_WARN:
        out.append(("spo2_low", f"низкий SpO2 ({c['spo2']}%)", 1))

    t = c["temperature"]
    if t is not None:
        if t > TEMP_HI_CRIT:
            out.append(("temp_high", f"критически высокая температура ({t:.1f} °C)", 2))
        elif t > TEMP_HI_WARN:
            out.append(("temp_high", f"повышенная температура ({t:.1f} °C)", 1))
        elif t < TEMP_LO_WARN:
            out.append(("temp_low", f"пониженная температура ({t:.1f} °C)", 1))

    warn_r, crit_r = RAD_LIMITS.get(c["phase"], RAD_LIMITS["orbit"])
    phase = PHASE_RU.get(c["phase"], c["phase"])
    if c["radiation"] >= crit_r:
        out.append(("radiation", f"опасный уровень радиации ({c['radiation']:.0f} мкЗв/ч, этап: {phase})", 2))
    elif c["radiation"] >= warn_r:
        out.append(("radiation", f"повышенная радиация ({c['radiation']:.0f} мкЗв/ч, этап: {phase})", 1))

    if c["manual_alarm"] and not out:
        out.append(("alarm", "ручная тревога с часов", 1))
    return out


def is_lost(c):
    return flag(c["astronaut"], "lost") or time.time() - c["received_at"] > LINK_TIMEOUT


def status_of(c):
    if is_lost(c):
        return "CONNECTION_LOST"
    level = max([r[2] for r in reasons(c)], default=0)
    return ("NORMAL", "WARNING", "CRITICAL")[level]


def track(c):
    """Detect status changes, write them to the log and to the alerts table."""
    st = status_of(c)
    prev = c["last_status"]
    if st == prev:
        return st
    c["last_status"] = st
    if st == "CONNECTION_LOST" or (st in ("WARNING", "CRITICAL") and RANK[st] > RANK.get(prev, 0)):
        why = "связь потеряна" if st == "CONNECTION_LOST" else ", ".join(r[1] for r in reasons(c))
        events.appendleft({"time": datetime.now().strftime("%H:%M:%S"),
                           "text": f"{c['astronaut']}: {why}", "level": st})
        notifier.push(f"{c['astronaut']}: {why}", st, c["astronaut"])
        db("INSERT INTO alerts (astronaut_id, ts, level, text) VALUES (?,?,?,?)",
           (c["astronaut"], time.time(), st, why))
    return st


STATUS_RU = {
    "NORMAL": "норма",
    "WARNING": "предупреждение",
    "CRITICAL": "критическое состояние",
    "CONNECTION_LOST": "связь потеряна",
}

MOTION_RU = {
    "rest": "покой",
    "moving": "движение",
    "fall": "падение",
    "no_sensor": "датчик движения не найден",
}

RECOMMENDATIONS = {
    "hr_high": "Высокий пульс: дать отдых, снизить нагрузку, проверить температуру в отсеке и водный баланс, повторить измерение через 5 минут. Если пульс не снижается, связаться с врачом на Земле.",
    "hr_low": "Низкий пульс: проверить самочувствие, избегать резких движений, сообщить врачу миссии.",
    "spo2_low": "Низкий SpO2: проверить подачу кислорода и герметичность скафандра, дышать медленно и глубоко, повторить измерение. Если значение не растёт, сообщить врачу миссии.",
    "fall": "Падение: осмотреть астронавта на травмы, при боли не двигать, связаться с врачом.",
    "alarm": "Сигнал тревоги: связаться с астронавтом, уточнить самочувствие и при необходимости вызвать врача.",
    "radiation": "Повышенная радиация: укрыться в зоне с лучшей защитой, ограничить время вне укрытия, зафиксировать накопленную дозу и сообщить врачу и центру управления.",
    "temp_high": "Повышенная температура: снизить нагрузку, пить воду, проверить систему охлаждения, повторить измерение. При росте сообщить врачу.",
    "temp_low": "Пониженная температура: проверить одежду и климат в отсеке, согреться, повторить измерение, сообщить врачу.",
}


# ---------- Receiving data ----------

def new_crew(a, device_id):
    row = db("SELECT hr_baseline FROM astronauts WHERE id=?", (a,), fetch=True)
    if row:
        base = row[0][0] or DEFAULT_BASELINE
        db("UPDATE astronauts SET device_id=? WHERE id=?", (device_id, a))
    else:
        base = DEFAULT_BASELINE
        db("INSERT INTO astronauts (id, name, device_id, hr_baseline) VALUES (?,?,?,?)",
           (a, a, device_id, base))
    c = {
        "astronaut": a, "device_id": device_id, "baseline": base,
        "hr": 0, "hr_source": "sim", "spo2": 98, "temperature": None, "motion": "rest",
        "radiation": 0.0, "dose": 0.0, "phase": "orbit", "manual_alarm": False,
        "history": deque(maxlen=60), "rest_hr": deque(maxlen=120), "n": 0,
        "dose_extra": 0.0, "received_at": 0.0, "last_status": None,
    }
    crew[a] = c
    return c


@app.post("/api/measurements")
@app.post("/api/data")
def receive(m: Measurement):
    now = time.time()
    a = m.astronaut_id

    if flag(a, "lost"):                      # demo: the device "disappears"
        return {"ok": True, "alarm": False, "dropped": True}

    c = crew.get(a) or new_crew(a, m.device_id)

    rad = m.radiation_usv_h
    if flag(a, "flare"):                     # demo: solar flare
        rad *= 8
        dt = min(now - c["received_at"], 5) if c["received_at"] else 1
        c["dose_extra"] += (rad - m.radiation_usv_h) / 3600 / 1000 * dt

    hr, hr_source = m.heart_rate, m.heart_rate_source
    cam = phone_pulse.get(a)
    if cam is not None:                      # fresh pulse from the phone camera wins over the device value
        hr, hr_source = cam, "camera"

    c.update(
        device_id=m.device_id,
        hr=hr,
        hr_source=hr_source,
        spo2=m.spo2,
        temperature=m.temperature,
        motion="fall" if flag(a, "fall") else m.motion.lower(),
        radiation=rad,
        dose=m.dose_total_msv + c["dose_extra"],
        phase=m.mission_phase,
        manual_alarm=m.manual_alarm,
        received_at=now,
    )
    c["history"].append(hr)
    c["n"] += 1

    # Personal pulse baseline: rolling median of resting values
    if c["motion"] == "rest" and not m.manual_alarm and 50 <= hr <= 100:
        c["rest_hr"].append(hr)
        if len(c["rest_hr"]) >= 30:
            c["baseline"] = round(statistics.median(c["rest_hr"]), 1)
            if c["n"] % 30 == 0:
                db("UPDATE astronauts SET hr_baseline=? WHERE id=?", (c["baseline"], a))

    st = track(c)
    db("""INSERT INTO measurements (astronaut_id, ts, heart_rate, hr_source, spo2, temperature,
          motion, radiation, dose_total, phase, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
       (a, now, c["hr"], c["hr_source"], c["spo2"], c["temperature"], c["motion"],
        c["radiation"], c["dose"], c["phase"], st))
    return {"ok": True, "alarm": st in ("WARNING", "CRITICAL"), "status": st}


# ---------- Pulse from the phone camera ----------

@app.post("/api/pulse")
def receive_pulse(p: PulseIn):
    a = p.astronaut_id.strip() or "AST-001"
    now = time.time()
    if not phone_pulse.set(a, p.bpm, now):
        return {"ok": False, "error": "пульс вне диапазона 30-220"}

    c = crew.get(a)
    if c is None or is_lost(c):
        # устройства нет на связи: пульс с телефона сам становится измерением
        receive(Measurement(astronaut_id=a, device_id="PHONE-CAM", heart_rate=p.bpm,
                            heart_rate_source="camera"))
    else:
        c["hr"], c["hr_source"] = p.bpm, "camera"
        c["history"].append(p.bpm)

    if p.final:
        text = f"{a}: пульс с камеры телефона {p.bpm} уд/мин"
        events.appendleft({"time": datetime.now().strftime("%H:%M:%S"), "text": text, "level": "INFO"})
        notifier.push(text, "INFO", a)
    return {"ok": True, "bpm": p.bpm, "astronaut": a}


@app.get("/api/ai/analyze")
def ai_analyze(astronaut: str = ""):
    c = crew.get(astronaut) or (crew[sorted(crew)[0]] if crew else None)
    if c is None:
        return {"answer": "Данных от устройств пока нет.", "ai": ai_name()}
    q = (f"Оцени текущие показатели астронавта {c['astronaut']} (пульс {c['hr']}, источник пульса: {c['hr_source']}). "
         "Скажи коротко: всё ли в норме, на что обратить внимание и что делать.")
    text = ask_ai(q, crew_context())
    return {"answer": text or recommendations([c]), "ai": ai_name() if text else "offline"}


# ---------- Notifications for the watch (vibration) ----------

@app.get("/api/notify/poll")
def notify_poll(since: int = -1):
    last, items = notifier.poll(since)
    return {"last": last, "items": items}


@app.post("/api/notify")
def notify_send(n: NotifyIn):
    events.appendleft({"time": datetime.now().strftime("%H:%M:%S"), "text": f"На часы: {n.text}", "level": "INFO"})
    return {"ok": True, "id": notifier.push(n.text, n.level)}


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))      # пакеты не отправляются, нужен только выбор интерфейса
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


@app.get("/api/info")
def info():
    ip = lan_ip()
    http_port = os.environ.get("SMA_HTTP_PORT", "8000")
    https_port = os.environ.get("SMA_HTTPS_PORT")
    phone = f"https://{ip}:{https_port}/pulse" if https_port else f"http://{ip}:{http_port}/pulse"
    return {"phone_url": phone, "https": bool(https_port), "watch_online": notifier.bridge_online(),
            "ai": ai_name()}


# ---------- State for the dashboard ----------

_ai_cache = (0.0, "offline")


def ai_name():
    global _ai_cache
    if time.time() - _ai_cache[0] > 15:
        _ai_cache = (time.time(), ai_provider() or "offline")
    return _ai_cache[1]


def snapshot():
    out = []
    for a in sorted(crew):
        c = crew[a]
        st = track(c)
        lost = st == "CONNECTION_LOST"
        out.append({
            "astronaut": a,
            "device_id": c["device_id"],
            "hr": c["hr"],
            "hr_source": c["hr_source"],
            "spo2": c["spo2"],
            "temperature": c["temperature"],
            "motion": c["motion"],
            "radiation": round(c["radiation"], 1),
            "dose": round(c["dose"], 5),
            "phase": c["phase"],
            "baseline": round(c["baseline"]),
            "status": st,
            "reasons": ["нет данных от устройства"] if lost else [r[1] for r in reasons(c)],
            "age": round(time.time() - c["received_at"], 1),
            "history": list(c["history"]),
        })
    return {"crew": out, "events": list(events), "ai": ai_name()}


@app.get("/api/state")
def state():
    return snapshot()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(snapshot())
            await asyncio.sleep(1)
    except Exception:
        pass


@app.get("/api/astronauts")
def astronauts():
    rows = db("SELECT id, name, device_id, hr_baseline FROM astronauts ORDER BY id", fetch=True)
    out = []
    for a, name, device, base in rows:
        c = crew.get(a)
        out.append({
            "id": a, "name": name, "device_id": device, "hr_baseline": base,
            "status": status_of(c) if c else "CONNECTION_LOST",
        })
    return {"astronauts": out}


@app.get("/api/astronauts/{astronaut_id}/history")
def history(astronaut_id: str, minutes: int = 10, limit: int = 300):
    since = time.time() - max(1, minutes) * 60
    rows = db("""SELECT ts, heart_rate, hr_source, spo2, temperature, motion, radiation,
                 dose_total, phase, status FROM measurements
                 WHERE astronaut_id=? AND ts>=? ORDER BY ts DESC LIMIT ?""",
              (astronaut_id, since, min(max(1, limit), 2000)), fetch=True)
    keys = ["ts", "heart_rate", "hr_source", "spo2", "temperature", "motion",
            "radiation", "dose_total", "phase", "status"]
    return {"astronaut": astronaut_id, "items": [dict(zip(keys, r)) for r in reversed(rows)]}


@app.get("/api/conditions")
def conditions():
    return {"conditions": conditions_list()}


@app.get("/api/report")
def report(minutes: int = 30):
    since = time.time() - max(1, minutes) * 60
    stats = db("""SELECT astronaut_id, COUNT(*), AVG(heart_rate), MIN(heart_rate), MAX(heart_rate),
                  MIN(spo2), MAX(temperature), MAX(radiation), MAX(dose_total)
                  FROM measurements WHERE ts>=? GROUP BY astronaut_id ORDER BY astronaut_id""",
               (since,), fetch=True)
    alert_rows = db("SELECT astronaut_id, level, COUNT(*) FROM alerts WHERE ts>=? GROUP BY astronaut_id, level",
                    (since,), fetch=True)
    alerts_by = {}
    for a, lvl, n in alert_rows:
        alerts_by.setdefault(a, {})[lvl] = n

    lines = [f"Отчёт для медицинской команды за последние {minutes} мин, {datetime.now():%H:%M}. "
             "Пульс, SpO2, температура и радиация сейчас тестовые (симуляция)."]
    items = []
    for a, n, avg, mn, mx, spo2_min, tmax, radmax, dose in stats:
        c = crew.get(a)
        st = status_of(c) if c else "CONNECTION_LOST"
        al = alerts_by.get(a, {})
        items.append({"astronaut": a, "status": st, "samples": n, "hr_avg": round(avg, 1),
                      "hr_min": mn, "hr_max": mx, "spo2_min": spo2_min, "temp_max": tmax,
                      "radiation_max": radmax, "dose_total": dose, "alerts": al})
        temp = f"{tmax:.1f} °C" if tmax is not None else "нет данных"
        lines.append(
            f"{a}: статус {STATUS_RU[st]}. Пульс в среднем {avg:.0f} (от {mn} до {mx}), "
            f"SpO2 минимум {spo2_min}%, температура максимум {temp}, "
            f"радиация максимум {radmax:.0f} мкЗв/ч, доза {dose:.4f} мЗв. "
            f"Предупреждений: {al.get('WARNING', 0)}, критических: {al.get('CRITICAL', 0)}, "
            f"потерь связи: {al.get('CONNECTION_LOST', 0)}.")
    if not items:
        lines.append("Данных за этот период нет.")
    return {"minutes": minutes, "items": items, "text": "\n".join(lines)}


@app.post("/api/sim/event")
def sim_event(e: SimEvent):
    key = "*" if e.astronaut.lower() == "all" else e.astronaut
    now = time.time()
    names = {"solar_flare": "солнечная вспышка", "fall": "падение", "connection_lost": "потеря связи"}
    if e.event == "clear":
        sim_flags.clear()
        events.appendleft({"time": datetime.now().strftime("%H:%M:%S"),
                           "text": "Демо: события сброшены", "level": "INFO"})
        return {"ok": True}
    if e.event not in names:
        return {"ok": False, "error": "unknown event"}
    f = sim_flags.setdefault(key, {})
    if e.event == "solar_flare":
        f["flare"] = now + 60
    elif e.event == "fall":
        f["fall"] = now + 6
    else:
        f["lost"] = now + 25
    events.appendleft({"time": datetime.now().strftime("%H:%M:%S"),
                       "text": f"Демо: {names[e.event]} ({'все' if key == '*' else key})", "level": "INFO"})
    return {"ok": True}


# ---------- Voice questions ----------

WORD_NUMBERS = {"перв": 1, "втор": 2, "трет": 3, "четв": 4}


def pick(q):
    nums = set(int(n) for n in re.findall(r"(?:ast|аст\w*)[\s\-]*0*(\d+)", q))
    for stem, n in WORD_NUMBERS.items():
        if re.search(stem + r"\w*\s+(?:ast|аст)", q) or re.search(r"(?:ast|аст\w*)\s+" + stem, q):
            nums.add(n)
    ids = [f"AST-{n:03d}" for n in sorted(nums)]
    return [i for i in ids if i in crew]


def line(c):
    return (f"{c['astronaut']}: пульс {c['hr']}, SpO2 {c['spo2']}%, "
            f"{STATUS_RU[status_of(c)]}")


def recommendations(sel):
    problems = [c for c in sel if status_of(c) in ("WARNING", "CRITICAL")]
    if not problems:
        return "Все показатели в норме, рекомендаций нет. Поддерживайте режим сна, питьё и физическую нагрузку."
    parts = []
    for c in problems:
        for code in dict.fromkeys(r[0] for r in reasons(c)):
            parts.append(f"{c['astronaut']}. {RECOMMENDATIONS[code]}")
    return " ".join(parts) + " Это учебный прототип, а не медицинское заключение."


def crew_context():
    lines = []
    for a in sorted(crew):
        c = crew[a]
        why = ", ".join(r[1] for r in reasons(c)) or "нет"
        temp = f"{c['temperature']:.1f}" if c["temperature"] is not None else "нет данных"
        lines.append(
            f"{a}: пульс {c['hr']} (личная норма около {c['baseline']:.0f}, источник {c['hr_source']}), "
            f"SpO2 {c['spo2']}%, температура {temp}, движение {c['motion']}, "
            f"радиация {c['radiation']:.0f} мкЗв/ч, доза {c['dose']:.4f} мЗв, этап миссии {c['phase']}, "
            f"статус {status_of(c)}, причины: {why}")
    return "\n".join(lines)


def answer(q):
    # 1. Question about a condition: knowledge base + AI.
    # A plain request for a measurement ("какая температура") is answered from live data instead.
    data_words = ("радиац", "доза", "температур", "пульс", "кислород", "сердц", "дыхани")
    action_words = ("что делать", "как лечить", "лечен", "помощ", "помоги", "симптом", "при ", "первая")
    cond = find_condition(q)
    if cond and (any(w in q for w in action_words) or not any(w in q for w in data_words)):
        kb = condition_text(cond)
        return ask_ai(q, crew_context(), kb) or kb

    if not crew:
        return "Данных от устройств пока нет. Проверьте, что часы включены и отправляют данные."

    ids = pick(q)
    sel = [crew[i] for i in ids] if ids else [crew[i] for i in sorted(crew)]

    def has(*stems):
        return any(s in q for s in stems)

    if has("рекоменд", "что делать", "совет", "лечен", "помощ", "помоги"):
        return recommendations(sel)

    if has("отчет", "отчёт", "сводк"):
        warned = [c for c in sel if status_of(c) in ("WARNING", "CRITICAL")]
        head = f"Сводка: астронавтов {len(sel)}, с предупреждением {len(warned)}. "
        return head + ". ".join(line(c) for c in sel) + "."

    if has("радиац", "доза", "дозу", "облуч", "вспышк"):
        return ". ".join(f"{c['astronaut']}: радиация {c['radiation']:.0f} мкЗв/ч, накоплено {c['dose']:.4f} мЗв"
                         for c in sel) + ". Значения тестовые."

    if has("температур"):
        return ". ".join(
            f"{c['astronaut']}: температура " + (f"{c['temperature']:.1f}" if c["temperature"] is not None else "нет данных")
            for c in sel) + ". Значения тестовые."

    if has("падени", "упал", "движен"):
        return ". ".join(f"{c['astronaut']}: {MOTION_RU.get(c['motion'], c['motion'])}" for c in sel) + "."

    if has("пульс", "сердц"):
        return ". ".join(f"Пульс {c['astronaut']}: {c['hr']} ударов в минуту, ваша норма около {c['baseline']:.0f}"
                         for c in sel) + "."

    if has("кислород", "spo", "спо", "сатурац", "дыхани"):
        return ". ".join(f"SpO2 {c['astronaut']}: {c['spo2']} процентов" for c in sel) + ". Норма от 94."

    if has("тревог", "предупрежд", "опасн", "проблем", "warning"):
        warned = [c for c in sel if status_of(c) in ("WARNING", "CRITICAL")]
        if not warned:
            return "Предупреждений нет. Все показатели в норме."
        return " ".join(f"{c['astronaut']}: " + ", ".join(r[1] for r in reasons(c)) + "." for c in warned)

    if has("экипаж", "статус", "состоян", "как дела", "в порядке"):
        return ". ".join(line(c) for c in sel) + "."

    # 2. Free question: AI (if available)
    ai_text = ask_ai(q, crew_context())
    if ai_text:
        return ai_text

    return ("Я могу рассказать про пульс, кислород, температуру, радиацию, движение, статус экипажа и тревоги, "
            "дать сводку или рекомендации. Спросите также, что делать при болезни или травме, "
            "например: что делать при гипоксии. Можно назвать астронавта, например: пульс астронавта 2.")


@app.get("/api/ask")
def ask(q: str = ""):
    return {"answer": answer(q.lower())}


@app.post("/api/voice/query")
def voice_query(v: VoiceQuery):
    return {"answer": answer(v.text.lower())}


# ---------- Dashboard ----------

@app.get("/")
@app.get("/pulse")
def index():
    return FileResponse(BASE_DIR / "index.html")
