#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
volcano_http.py – Mini-HTTP-Server für S&B Volcano Hybrid (BLE → HTTP)
StayConnected + /discover + --verbose + --devmode (Dev-Tools & Notify-Sniffer)
Kompatibel mit bleak 1.1.x (Linux)
"""

import threading
import requests
import asyncio
import argparse
import traceback
import time
import json
import os
import subprocess
import shutil
from pathlib import Path
from aiohttp import web
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from datetime import datetime

from numpy.core.defchararray import title

# === Konfigurierbare Parameter (manuell anpassbar) ===
# Port, auf dem der HTTP-Server lauscht und über den interne Auto-/on-Calls laufen.
AUTO_ON_PORT = 8181

# Standard-Zieltemperatur beim ersten Start, bis eine andere per HTTP gesetzt wird.
TO_KILL_NOTIFICATION = 1
LAST_MESSAGE = ''
WATCH_RUNNING = False
TEMP_AVAILABLE = ['160','170','180','190','200','210','220','230']
TEMP_INDEX = 0
DEFAULT_TEMP = TEMP_AVAILABLE[TEMP_INDEX]
FAN_STATE = 'Pumpen AUS'
TIMER_SET = False

from bleak.backends.scanner import AdvertisementData
from typing import Optional, Dict, List, Tuple

# ==== Einfaches Error-Log im /tmp ====
LOG_PATH = "/tmp/volcano_http_error.log"

NOTIFY_PATH = shutil.which("notify-send")              # z.B. /usr/local/bin/notify-send (Wrapper)
ns_real = NOTIFY_PATH + ".real"                        # -> /usr/local/bin/notify-send.real
if os.path.exists(ns_real):
    NOTIFY_PATH = ns_real

def log_error(msg: str, exc: Optional[BaseException] = None) -> None:
    """
    Sehr einfache Fehler-Logroutine.
    - Nur für Fehler gedacht (kein normales Logging).
    - Schreibt nach /tmp/volcano_http_error.log
    - Keine Rotation, kein Schnickschnack.
    """
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
            if exc is not None:
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        # Logging soll nie selbst einen Crash verursachen
        pass

# ==== Bekannte UUIDs ====
SERVICE_UUID      = "10110000-5354-4f52-5a26-4249434b454c"
CHAR_CURRENT_TEMP = "10110001-5354-4f52-5a26-4249434b454c"
CHAR_TARGET_TEMP  = "10110003-5354-4f52-5a26-4249434b454c"
CHAR_HEAT_ON      = "1011000f-5354-4f52-5a26-4249434b454c"
CHAR_HEAT_OFF     = "10110010-5354-4f52-5a26-4249434b454c"
CHAR_FAN_ON       = "10110013-5354-4f52-5a26-4249434b454c"
CHAR_FAN_OFF      = "10110014-5354-4f52-5a26-4249434b454c"
ALT_CHAR_FAN_ON   = "10100013-5354-4f52-5a26-4249434b454c"
ALT_CHAR_FAN_OFF  = "10100014-5354-4f52-5a26-4249434b454c"

# Kandidaten für Settings (aus Dump herausgefiltert)
SETTINGS_CANDIDATES = [
    "10110005-5354-4f52-5a26-4249434b454c",  # u8 -> LED?
    "1011000d-5354-4f52-5a26-4249434b454c",  # u16 -> Auto-Shutoff?
    "10110011-5354-4f52-5a26-4249434b454c",  # bool -> Vibration/Display?
    "10110012-5354-4f52-5a26-4249434b454c",  # bool -> Vibration/Display?
    "10110004-5354-4f52-5a26-4249434b454c",  # evtl. Mirror/bitfield
    "101300ff-5354-4f52-5a26-4249434b454c",  # möglicher blob
]


def _u16le_to_c(v: bytes) -> Optional[float]:
    return int.from_bytes(v[:2], "little") / 10.0 if len(v) >= 2 else None


def _hex(b: bytes, maxlen: int = 64) -> str:
    if b is None:
        return "None"
    h = b.hex()
    return h if len(h) <= maxlen else (h[:maxlen] + "…")


def _looks_like_volcano(name: Optional[str], uuids: Optional[List[str]]) -> bool:
    n = (name or "").lower()
    if any(x in n for x in ("volcano", "storz", "bickel", "s&b")):
        return True
    for u in uuids or []:
        if u and SERVICE_UUID in u.lower():
            return True
    return False


# ==== BLE-Kern ====
class VolcanoBLE:
    def __init__(
        self,
        mac: Optional[str],
        scan_seconds: int = 6,
        keepalive: bool = True,
        keepalive_interval: int = 5,
        preconnect: bool = True,
        devmode: bool = False,
    ):
        self.mac = mac
        self.scan_seconds = scan_seconds
        self.keepalive = keepalive
        self.keepalive_interval = keepalive_interval
        self.preconnect = preconnect
        self.devmode = devmode

        self.last_target_temp = DEFAULT_TEMP

        self.client: Optional[BleakClient] = None
        self._lock = asyncio.Lock()
        self._maintain_task: Optional[asyncio.Task] = None
        self._notify_started = False

        # Merke zuletzt erfolgreiche Adresse (für schnellere Reconnects)
        self._last_addr: Optional[str] = None
        # Einfache Retry-Policy für Read/Write nach Disconnect
        self._op_retry_once = True

    def _on_disconnect(self, _client) -> None:
        """Callback von bleak bei unerwartetem Disconnect.

        Wichtig: Callback ist sync -> hier nur Status zurücksetzen, kein await.
        """
        global WATCH_RUNNING
        print('\n' + ts() + 'BLE    : ' + 'getrennt                             ❌\n')
        try:
            if self.devmode:
                print("[DEV] Disconnected-Callback: Verbindung verloren, markiere Client als None.")
        finally:
            WATCH_RUNNING = False
            self.client = None
            self._notify_started = False

    async def _reset_client(self):
        """Erzwingt sauberen Reset der aktuellen Client-Instanz."""
        if self.client:
            try:
                await self.client.disconnect()
            except Exception as e:
                log_error("Fehler beim Disconnect im Reset", e)
            finally:
                self.client = None
        self._notify_started = False

    def ts(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S ")

    def watch(self):
        global WATCH_RUNNING
        global DEFAULT_TEMP
        global TEMP_INDEX
        global TEMP_AVAILABLE
        if WATCH_RUNNING:
            return
        WATCH_RUNNING = True
        ok = False
        url = f"http://127.0.0.1:8181/"
        i = 0
        while not ok:
            try:
                aw = requests.get(url + 'status',timeout=20)
                data = aw.json()
                soll = str(int(data['target']))
                ist = str(int(data['current']))
                TEMP_INDEX = TEMP_AVAILABLE.index(soll) if soll in TEMP_AVAILABLE else 0
                DEFAULT_TEMP = TEMP_AVAILABLE[TEMP_INDEX]
                print(ts() + 'BLE    : ' + 'verbunden                            ✅\n')
                aw = requests.get(url + 'on?temp=' + str(int(data['target'])),timeout=20)
                data = aw.json()
                if(data['ok'] and data['action'] == 'on'):
                        ok = True
                else:
                    time.sleep(1)
            except:
                time.sleep(5)
        FAN_STATE = 'Pumpen AUS'
        WATCH_RUNNING = False

    async def _scan_pick_best(self, seconds: Optional[int] = None) -> Optional[str]:
        seconds = seconds or self.scan_seconds
        found: Dict[str, Tuple[BLEDevice, int, List[str], str]] = {}

        def cb(d: BLEDevice, a: AdvertisementData):
            rssi = a.rssi if (a and a.rssi is not None) else -999
            name = d.name or (a.local_name if a else None) or ""
            uuids = (a.service_uuids or []) if a else []
            prev = found.get(d.address)
            if prev is None or rssi > prev[1]:
                found[d.address] = (d, rssi, uuids, name)

        if self.devmode:
            print(f"[DEV] Starte Scan ({seconds}s)…")
        s = BleakScanner(cb)
        await s.start()
        await asyncio.sleep(seconds)
        await s.stop()

        volcanoes = [
            (addr, tup)
            for addr, tup in found.items()
            if _looks_like_volcano(tup[3], tup[2])
        ]
        if not volcanoes:
            if self.devmode:
                print("[DEV] Scan: Kein Volcano erkannt.")
            return None
        volcanoes.sort(key=lambda kv: kv[1][1], reverse=True)
        best_addr = volcanoes[0][0]
        print(ts() + 'BLE    : Auto-Scan')
        print(ts() + 'BLE    : Gefunden ' + best_addr)
        return best_addr

    async def _dump_services(self, c: BleakClient):
        try:
            svcs = getattr(c, "services", None)
            if not svcs:
                print("[DEV] Services nicht verfügbar (bleak stellt keine Liste bereit).")
                return
            print("[DEV] Services/Characteristics:")
            for svc in svcs:
                print(
                    f"[DEV] Service: {svc.uuid}  ({getattr(svc, 'description', 'Unknown')})"
                )
                for ch in svc.characteristics:
                    props = ",".join(sorted(ch.properties))
                    print(f"[DEV]   Char: {ch.uuid}  props=[{props}]")
        except Exception as e:
            msg = f"[DEV] Service-Dump Fehler: {e}"
            print(msg)
            traceback.print_exc()
            log_error(msg, e)

    async def _start_notify_sniffer(self, c: BleakClient):
        if self._notify_started:
            return
        svcs = getattr(c, "services", None)
        if not svcs:
            return

        async def mk_cb(u):
            def _cb(_, data: bytearray):
                print(f"[DEV] NOTIFY {u} → {data.hex()}")

            return _cb

        for svc in svcs:
            for ch in svc.characteristics:
                if "notify" in ch.properties:
                    try:
                        await c.start_notify(ch.uuid, await mk_cb(ch.uuid))
                        if self.devmode:
                            print(f"[DEV] Notify abonniert: {ch.uuid}")
                    except Exception as e:
                        if self.devmode:
                            print(
                                f"[DEV] Notify-Abo fehlgeschlagen für {ch.uuid}: {e}"
                            )
                        log_error(
                            f"Notify-Abo fehlgeschlagen für {ch.uuid}", e
                        )
        self._notify_started = True

    async def _connect_once(self) -> BleakClient:
        addr = self.mac or self._last_addr or await self._scan_pick_best()
        if not addr:
            e = RuntimeError("Kein Volcano gefunden (Scan leer).")
            log_error("Connect fehlgeschlagen: kein Volcano gefunden", e)
            raise e
        if self.client:
            try:
                if self.devmode:
                    print("[DEV] Vorherige Verbindung trennen…")
                await self.client.disconnect()
            except Exception as e:
                log_error("Fehler beim Trennen einer alten Verbindung", e)
            self.client = None
        if self.devmode:
            print(f"[DEV] Verbinde mit {addr} …")
        c = BleakClient(addr, disconnected_callback=self._on_disconnect)
        await c.connect()
        if not getattr(c, "is_connected", False):
            e = RuntimeError("BLE-Verbindung fehlgeschlagen.")
            log_error("BLE-Verbindung fehlgeschlagen (is_connected=False)", e)
            raise e
        self.client = c
        self._last_addr = addr

        #Auto-Start über internen HTTP-Call (nutzt die zuletzt gesetzte Zieltemperatur)
        ################################################################################################
        # Watch (auto-heat) nach (Re)Connect starten
        global WATCH_RUNNING
        if not WATCH_RUNNING:
            threading.Thread(target=self.watch, daemon=True).start()

        if self.devmode:
            await self._dump_services(c)
            await self._start_notify_sniffer(c)
        return c

    async def ensure_connected(self) -> BleakClient:
        if self.client and getattr(self.client, "is_connected", False):
            return self.client
        return await self._connect_once()

    # --- Primitive ops (mit DEV-Logs) ---
    async def _read(self, uuid: str) -> bytes:
        async with self._lock:
            c = await self.ensure_connected()
            if self.devmode:
                print(f"[DEV] READ  {uuid} …")
            try:
                data = await c.read_gatt_char(uuid)
            except Exception as e:
                # Typisches Szenario: Gerät war aus -> is_connected bleibt "true", aber READ knallt.
                log_error(f"READ {uuid} fehlgeschlagen – versuche Reconnect", e)
                if self.devmode:
                    print(f"[DEV] READ {uuid} fehlgeschlagen: {e} (Reconnect & Retry)")
                await self._reset_client()
                c = await self.ensure_connected()
                data = await c.read_gatt_char(uuid)
            if self.devmode:
                print(f"[DEV] READ  {uuid} → { _hex(data) }")
            return data


    async def _write_safe(self, uuid: str, data: bytes):
        async with self._lock:
            c = await self.ensure_connected()
            if self.devmode:
                print(f"[DEV] WRITE {uuid} ← { _hex(data) } (response=True)")
            try:
                try:
                    await c.write_gatt_char(uuid, data, response=True)
                    if self.devmode:
                        print(f"[DEV] WRITE {uuid} ✓ (response=True)")
                    return
                except Exception as e:
                    if self.devmode:
                        print(f"[DEV] WRITE {uuid} response=True fehlgeschlagen: {e} → ohne Response")
                    log_error(f"WRITE {uuid} response=True fehlgeschlagen – Versuch mit response=False", e)
                    await c.write_gatt_char(uuid, data, response=False)
                    if self.devmode:
                        print(f"[DEV] WRITE {uuid} ✓ (response=False)")
                    return
            except Exception as e:
                # Wenn das Gerät zwischendurch aus war, hilft oft nur: Client verwerfen, neu verbinden, nochmal schreiben.
                log_error(f"WRITE {uuid} endgültig fehlgeschlagen – Reconnect & Retry", e)
                if self.devmode:
                    print(f"[DEV] WRITE {uuid} endgültig fehlgeschlagen: {e} (Reconnect & Retry)")
                await self._reset_client()
                c = await self.ensure_connected()
                # Noch ein Versuch – diesmal ohne response-Logik-Spielchen.
                await c.write_gatt_char(uuid, data, response=False)
                if self.devmode:
                    print(f"[DEV] WRITE {uuid} ✓ (nach Reconnect, response=False)")


    # --- High-level bekannte Funktionen ---
    async def current_temp(self) -> Optional[float]:
        return _u16le_to_c(await self._read(CHAR_CURRENT_TEMP))

    async def target_temp(self) -> Optional[float]:
        return _u16le_to_c(await self._read(CHAR_TARGET_TEMP))

    async def set_temp(self, t: float):
        v = max(0, min(2600, int(round(t * 10))))
        await self._write_safe(CHAR_TARGET_TEMP, v.to_bytes(2, "little"))

    async def heat_on(self):
        await self._write_safe(CHAR_HEAT_ON, b"\x01")

    async def heat_off(self):
        await self._write_safe(CHAR_HEAT_OFF, b"\x01")

    async def fan_on(self):
        for uuid in (CHAR_FAN_ON, ALT_CHAR_FAN_ON):
            try:
                await self._write_safe(uuid, b"\x01")
                return
            except Exception as e:
                log_error(f"fan_on: Fehler beim Schreiben {uuid}", e)
                pass
        raise RuntimeError("fan_on: keine passende Characteristic erreichbar")

    async def fan_off(self):
        for uuid in (CHAR_FAN_OFF, ALT_CHAR_FAN_OFF):
            try:
                await self._write_safe(uuid, b"\x01")
                return
            except Exception as e:
                log_error(f"fan_off: Fehler beim Schreiben {uuid}", e)
                pass
        raise RuntimeError("fan_off: keine passende Characteristic erreichbar")

    # --- Keep-Alive ---
    async def _maintain_loop(self):
        while True:
            try:
                await self.ensure_connected()
                try:
                    await self._read(CHAR_CURRENT_TEMP)
                except Exception:
                    # Lese-Fehler auf Temperatur sind nicht kritisch
                    pass
            except Exception as e:
                if self.devmode:
                    print(f"[DEV] Keep-Alive Problem: {e}")
                log_error("Keep-Alive Problem", e)
            await asyncio.sleep(self.keepalive_interval)

    async def startup(self):
        # Robuster Preconnect mit mehreren Versuchen
        if self.preconnect:
            max_attempts = 3
            delay_between = 5
            for attempt in range(1, max_attempts + 1):
                try:
                    if self.devmode:
                        print(
                            f"[DEV] Preconnect-Versuch {attempt}/{max_attempts} …"
                        )
                    await self.ensure_connected()
                    if self.devmode:
                        print("[DEV] Preconnect erfolgreich.")
                    break
                except Exception as e:
                    msg = f"[DEV] Preconnect fehlgeschlagen (Versuch {attempt}): {e}"
                    log_error(
                        f"Preconnect fehlgeschlagen (Versuch {attempt})", e
                    )
                    if attempt < max_attempts:
                        if self.devmode:
                            print(
                                f"[DEV] Warte {delay_between}s bis zum nächsten Versuch …"
                            )
                        await asyncio.sleep(delay_between)
                    else:
                        log_error(
                            "Preconnect endgültig aufgegeben – Keep-Alive übernimmt spätere Reconnects.",
                            e,
                        )

        if self.keepalive and not self._maintain_task:
            if self.devmode:
                print("[DEV] Starte Keep-Alive-Loop …")
            self._maintain_task = asyncio.create_task(self._maintain_loop())

    async def shutdown(self):
        # Keep-Alive-Task stoppen
        if self._maintain_task:
            self._maintain_task.cancel()
            try:
                await self._maintain_task
            except asyncio.CancelledError:
                pass
            self._maintain_task = None

        # BLE-Verbindung sauber trennen
        if self.client and getattr(self.client, "is_connected", False):
            try:
                print(ts() + "BLE    :  Trenne Verbindung zum Gerät (Shutdown)…")
                await self.client.disconnect()
            except Exception as e:
                log_error("Fehler beim BLE-Disconnect im Shutdown", e)
            finally:
                self.client = None


# ==== Discover ====
async def discover_ble(
    seconds: int = 5, volcano_only: bool = True
) -> List[Dict]:
    found: Dict[str, Tuple[str, int, List[str]]] = {}

    def cb(d: BLEDevice, a: AdvertisementData):
        rssi = a.rssi if (a and a.rssi is not None) else -999
        name = d.name or (a.local_name if a else None) or ""
        uuids = (a.service_uuids or []) if a else []
        if d.address not in found or rssi > found[d.address][1]:
            found[d.address] = (name, rssi, uuids)

    scanner = BleakScanner(cb)
    await scanner.start()
    await asyncio.sleep(max(2, seconds))
    await scanner.stop()
    items = []
    for addr, (name, rssi, uuids) in found.items():
        iv = _looks_like_volcano(name, uuids)
        if volcano_only and not iv:
            continue
        items.append(
            {
                "address": addr,
                "name": name,
                "rssi": rssi,
                "service_uuids": uuids,
                "is_volcano": iv,
            }
        )
    items.sort(key=lambda x: x["rssi"], reverse=True)
    return items


# ==== Verbose-Monitor ====
async def monitor_connection(v: "VolcanoBLE", interval: int = 10):
    connected = False
    old = True
    url = "http://127.0.0.1:8181/off"
    while True:
        connected = bool(v.client and getattr(v.client, "is_connected", False))
        if connected and not old:
            await send_notify(None, v, 'Volcano: Online EIN', 'online')
        if connected:
            old = True
        if not connected:
            t = threading.Thread(target=v.watch)
            t.start()
            if old:
                try:
                    await send_notify(None, v, 'Volcano: Online AUS', 'offline')
                    old = False
                except Exception as e:
                    pass
            await asyncio.sleep(interval)
        await asyncio.sleep(interval)

def value_to_rgb(v: float, vmin=0, vmax=230):
    v = max(vmin, min(vmax, v))
    t = (v - vmin) / (vmax - vmin)
    if t <= 0.5:
        tt = t / 0.5
        r = int(0 + (255 - 0) * tt)
        g = int(0 + (255 - 0) * tt)
        b = int(255 + (0 - 255) * tt)
    else:
        tt = (t - 0.5) / 0.5
        r = 255
        g = int(255 + (0 - 255) * tt)
        b = 0
    return r, g, b

def make_ball_icon(value: float, size: int = 88) -> str:
    from PIL import Image, ImageDraw
    import os, tempfile
    r, g, b = value_to_rgb(value)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Kreis + leichter Rand/Glanz
    pad = 1
    d.ellipse((pad, pad, size - pad, size - pad), fill=(r, g, b, 255))
    d.ellipse((pad, pad, size - pad, size - pad), outline=(0, 0, 0, 80), width=1)
    d.ellipse((pad+8, pad+8, size//2, size//2), fill=(255, 255, 255, 60))

    fd, path = tempfile.mkstemp(prefix="volcano_ball_", suffix=".png")
    os.close(fd)
    img.save(path, "PNG")
    return path

def map_value(temp: int) -> int:
    ranges = [(160, 174), (174, 189), (190, 209), (210, 220), (221, 230)]
    values = [80, 0, -5, -10, 0]

    for (low, high), value in zip(ranges, values):
        if low <= temp <= high:
            return value

    return 0

# ==== HTTP Helpers ====
async def _set_timer_for(seconds: float):
    global TIMER_SET
    TIMER_SET = True
    await asyncio.sleep(seconds)
    TIMER_SET = False

def set_timer(seconds: float):
    asyncio.create_task(_set_timer_for(seconds))

def vaporizer_text(temp_c: int, icon=False) -> str:
    if 160 <= temp_c <= 175:
        if icon:
            return '😋'
        return "Wirkung: Geschmack & Klarheit"

    elif 175 < temp_c <= 190:
        if icon:
            return '😶‍🌫️'
        return "Wirkung: Ausgeglichenes High"

    elif 190 <= temp_c <=210:
        if icon:
            return '😴'
        return "Wirkung: Entspannung & Schlaf"

    elif 210 <= temp_c < 221:
        if icon:
            return '⚕️'
        return "Wirkung: Medizinische Nutzung"

    elif temp_c > 221:
        if icon:
            return '🚫'
        return "Wirkung: !!!!! UNGESUNDES RISIKO !!!!!"
    else:
        return ""

def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S ")

async def send_notify(req, v, title: str, body: str, timeout_ms: int = 5000) -> None:
    """Zentraler Helper für Desktop-Notifications via notify-send."""
    from volcano_icons import get_cached_icon
    global TO_KILL_NOTIFICATION
    global LAST_MESSAGE
    global LAST_PRINT
    global FAN_STATE
    global TIMER_SET
    ist = 0
    soll = 0
    level = 'normal'
    if not req and body == LAST_MESSAGE:
        return
    old_message = LAST_MESSAGE
    if not TIMER_SET:
        pass
        #LAST_MESSAGE = body
    if req:
        level = 'normal'
        resp = await status(req)
        ist = json.loads(resp.text)["current"]
        soll = json.loads(resp.text)["target"]
    delta = soll - ist
    if 'GET /fan/on' in str(req):
        level = 'critical'
    if 'GET /fan/off' in str(req):
        TIMER_SET = False
    try:
        ball = '🟢'
        ersatzball = '🟢'
        if not req:
            ball  = '❌'
            if body == 'online':
                ball = '✅'
        if ist < soll:
            ball = '🔵'
            ersatzball = '🔵'
        elif ist == soll and ist != 0 and soll != 0:
            ball = vaporizer_text(ist,True)
            ersatzball = '🟢'
        elif ist > soll:
            ball = "🔴"
            ersatzball = "🔴"

        soll_str = str(int(soll))
        while len(soll_str) < 3:
            soll_str = ' ' + soll_str
        title += ' Soll: ' + soll_str + ' °C'
        ist_str = str(int(ist))
        if ist_str == '  0':
           ist_str = '---'
        while len(ist_str) < 3:
            ist_str = ' ' + ist_str
        title += (' Ist: ' + ist_str + ' °C  ')
        title += ball
        val = ist
        if val > 220:
            val = 230
        icon_path = get_cached_icon(int(ist - map_value(val)))
        body = vaporizer_text(soll)
        cmd: list[str] = [
        NOTIFY_PATH,
        "--expire-time", str(timeout_ms),
        "--icon", icon_path,
        "--urgency", level,
        "--replace-id", str(TO_KILL_NOTIFICATION),
        "--app-name", "Volcano",
        "-h", "boolean:transient:true",
        "-p",
        title,
        body
        ]
        if not TIMER_SET:
            res = subprocess.run(cmd, capture_output=True,  text=True)
            TO_KILL_NOTIFICATION = res.stdout.strip().split("\n")[0].strip()
            try:
                LAST_PRINT = LAST_PRINT
            except:
                LAST_PRINT = ''

            if 'Heizen' in title and LAST_PRINT:
                #print('tile=' + title)
                #print('LAST_PRINT=' + LAST_PRINT)
                if ist < soll or ist == soll:
                    title =  title.replace('AUS','EIN')
                if ist > soll:
                    title =  title.replace('EIN','AUS')
                if title.count(str(int(soll))) == 2:
                    print(ts() + title.replace(ball, ersatzball))
                    set_timer(1)
            new_print = title.split('Volcano: ')[1].split('Ist:')[0]
            if ist==soll==0:
                ersatzball  = '❌'
            #print('new_print=' + new_print)
            if new_print not in LAST_PRINT:
                print(ts() + title.replace(ball, ersatzball))
            #print(req)
            #if not 'GET /fan' in str(req):
            LAST_PRINT = new_print
            if 'Online AUS' in title:
                print()

        if 'GET /fan/on' in str(req):
            pass#set_timer(int(timeout_ms/1000) -1)
        if delta < 0:
            asyncio.create_task(notify_http_event(req, v, "Heizen AUS"))
        elif delta > 0:
            asyncio.create_task(notify_http_event(req, v, "Heizen EIN"))

    except Exception as e:
        print(str(e))
        log_error("send_notify: Fehler beim Aufruf von notify-send", e)


async def notify_http_event(req, v: "VolcanoBLE", action: str,
                            current: Optional[float] = None,
                            target: Optional[float] = None) -> None:
    """HTTP-Event-Notification mit Soll- und Ist-Temperatur."""

    try:
        if current is None or target is None:
            current, target = await asyncio.gather(v.current_temp(), v.target_temp())
    except Exception as e:
        log_error("notify_http_event: Fehler beim Lesen der Temperaturen", e)
        return

    try:
        cur_txt = "?" if current is None else f"{current:.1f} °C"
        tgt_txt = "?" if target is None else f"{target:.1f} °C"
    except Exception:
        cur_txt = str(current)
        tgt_txt = str(target)

    title = f"Volcano: {action}"
    body = f"Soll: {tgt_txt}, Ist: {cur_txt}"

    try:
        await send_notify(req, v, title, body)
    except Exception as e:
        log_error("notify_http_event: Fehler bei send_notify", e)


def ok(d):
    return web.json_response({"ok": True, **d})


def err(m, code=500):
    return web.json_response({"ok": False, "error": m}, status=code)


# ==== HTTP Handlers (User-API) ====
async def status(req):
    v: VolcanoBLE = req.app["v"]
    try:
        ct, tt = await asyncio.gather(v.current_temp(), v.target_temp())
        connected = bool(v.client and getattr(v.client, "is_connected", False))
        target = v.mac or "auto-scan"
        return ok(
            {
                "current": ct,
                "target": tt,
                "connected": connected,
                "selected": target,
            }
        )
    except Exception as e:
        if req.app["devmode"]:
            print("[DEV] /status Exception:")
            traceback.print_exc()
        log_error("/status Exception", e)
        return err(str(e))

################################################
################################################
################################################
################################################
async def on(req):
    global DEFAULT_TEMP
    global TEMP_AVAILABLE
    global TEMP_INDEX
    v: VolcanoBLE = req.app["v"]
    try:
        t = req.query.get("temp")
        temp_val = float(TEMP_AVAILABLE[TEMP_INDEX])
        if t:
            temp_val = float(t)
        temp_old = DEFAULT_TEMP
        await v.set_temp(temp_val)
        try:
            v.last_target_temp = str(temp_val).split('.')[0].split(',')[0]
            DEFAULT_TEMP = v.last_target_temp
            TEMP_INDEX += 1
            if TEMP_INDEX == len(TEMP_AVAILABLE):
                TEMP_INDEX = 0
        except Exception:
            pass
        what = "Heizen EIN"
        if DEFAULT_TEMP < temp_old:

            what = "Heizen AUS"
        await notify_http_event(req, v, what)
        await v.heat_on()
        return ok({"action": "on", "target": (float(t) if t else None)})
    except Exception as e:
        if req.app["devmode"]:
            print("[DEV] /on Exception:")
            traceback.print_exc()
        log_error("/on Exception", e)
        return err(str(e))


async def off(req):
    v: VolcanoBLE = req.app["v"]
    try:
        await v.heat_off()
        await notify_http_event(req, v, "Heizen AUS")
        return ok({"action": "off"})
    except Exception as e:
        if req.app["devmode"]:
            print("[DEV] /off Exception:")
            traceback.print_exc()
        log_error("/off Exception", e)
        return err(str(e))


async def fan_on(req):
    global FAN_STATE
    v: VolcanoBLE = req.app["v"]
    try:
        await v.fan_on()
        await notify_http_event(req, v, "Pumpen EIN")
        FAN_STATE = 'Pumpen EIN'
        return ok({"action": "fan_on"})
    except Exception as e:
        if req.app["devmode"]:
            print("[DEV] /fan/on Exception:")
            traceback.print_exc()
        log_error("/fan/on Exception", e)
        return err(str(e))


async def fan_off(req):
    global FAN_STATE
    v: VolcanoBLE = req.app["v"]
    try:
        await v.fan_off()
        await notify_http_event(req, v, "Pumpen AUS")
        FAN_STATE = 'Pumpen AUS'
        return ok({"action": "fan_off"})
    except Exception as e:
        if req.app["devmode"]:
            print("[DEV] /fan/off Exception:")
            traceback.print_exc()
        log_error("/fan/off Exception", e)
        return err(str(e))


# ==== HTTP Handlers (Dev-Tools) ====
async def settings_snapshot(req):
    if not req.app["devmode"]:
        return err("Not available without --devmode", 404)
    v: VolcanoBLE = req.app["v"]
    out = {}
    try:
        for uuid in SETTINGS_CANDIDATES:
            try:
                val = await v._read(uuid)
                out[uuid] = val.hex()
            except Exception as e:
                out[uuid] = f"ERR:{e}"
                log_error(f"/settings/snapshot READ-Fehler {uuid}", e)
        return ok({"settings_candidates": out})
    except Exception as e:
        print("[DEV] /settings/snapshot Exception:")
        traceback.print_exc()
        log_error("/settings/snapshot Exception", e)
        return err(str(e))


async def dev_read(req):
    if not req.app["devmode"]:
        return err("Not available without --devmode", 404)
    v: VolcanoBLE = req.app["v"]
    uuid = req.query.get("uuid")
    if not uuid:
        return err("uuid missing", 400)
    try:
        data = await v._read(uuid)
        return ok({"uuid": uuid, "hex": data.hex()})
    except Exception as e:
        print("[DEV] /dev/read Exception:")
        traceback.print_exc()
        log_error("/dev/read Exception", e)
        return err(str(e))


async def dev_write_bool(req):
    if not req.app["devmode"]:
        return err("Not available without --devmode", 404)
    v: VolcanoBLE = req.app["v"]
    uuid = req.query.get("uuid")
    value = req.query.get("value")
    if not uuid or value is None:
        return err("uuid/value missing", 400)
    b = b"\x01" if str(value) in ("1", "true", "on", "yes") else b"\x00"
    try:
        await v._write_safe(uuid, b)
        return ok({"uuid": uuid, "wrote": b.hex()})
    except Exception as e:
        print("[DEV] /dev/write/bool Exception:")
        traceback.print_exc()
        log_error("/dev/write/bool Exception", e)
        return err(str(e))


async def dev_write_u8(req):
    if not req.app["devmode"]:
        return err("Not available without --devmode", 404)
    v: VolcanoBLE = req.app["v"]
    uuid = req.query.get("uuid")
    val = req.query.get("value")
    if not uuid or val is None:
        return err("uuid/value missing", 400)
    x = max(0, min(255, int(val)))
    try:
        await v._write_safe(uuid, bytes([x]))
        return ok({"uuid": uuid, "wrote": f"{x:02x}"})
    except Exception as e:
        print("[DEV] /dev/write/u8 Exception:")
        traceback.print_exc()
        log_error("/dev/write/u8 Exception", e)
        return err(str(e))


async def dev_write_u16le(req):
    if not req.app["devmode"]:
        return err("Not available without --devmode", 404)
    v: VolcanoBLE = req.app["v"]
    uuid = req.query.get("uuid")
    val = req.query.get("value")
    if not uuid or val is None:
        return err("uuid/value missing", 400)
    x = max(0, min(65535, int(val)))
    data = x.to_bytes(2, "little")
    try:
        await v._write_safe(uuid, data)
        return ok({"uuid": uuid, "wrote": data.hex(), "int": x})
    except Exception as e:
        print("[DEV] /dev/write/u16le Exception:")
        traceback.print_exc()
        log_error("/dev/write/u16le Exception", e)
        return err(str(e))


async def dev_write_hex(req):
    if not req.app["devmode"]:
        return err("Not available without --devmode", 404)
    v: VolcanoBLE = req.app["v"]
    uuid = req.query.get("uuid")
    data_hex = req.query.get("data")
    if not uuid or not data_hex:
        return err("uuid/data missing", 400)
    try:
        data = bytes.fromhex(data_hex)
    except ValueError as e:
        log_error("/dev/write/hex: ungültige data-HEX-Eingabe", e)
        return err("data must be hex like '01ff'", 400)
    try:
        await v._write_safe(uuid, data)
        return ok({"uuid": uuid, "wrote": data.hex()})
    except Exception as e:
        print("[DEV] /dev/write/hex Exception:")
        traceback.print_exc()
        log_error("/dev/write/hex Exception", e)
        return err(str(e))


# ==== Discover-Handler ====
async def discover_handler(req):
    try:
        seconds = int(req.query.get("seconds", "5"))
        show_all = req.query.get("all") in ("1", "true", "yes")
        items = await discover_ble(seconds=seconds, volcano_only=not show_all)
        v: VolcanoBLE = req.app["v"]
        selected = v.mac or None
        for it in items:
            it["selected"] = (
                selected is not None and it["address"] == selected
            )
        return ok(
            {"scan_seconds": seconds, "count": len(items), "devices": items}
        )
    except Exception as e:
        if req.app["devmode"]:
            print("[DEV] /discover Exception:")
            traceback.print_exc()
        log_error("/discover Exception", e)
        return err(str(e))


def make_app(v: VolcanoBLE, devmode: bool) -> web.Application:
    a = web.Application()
    a["v"] = v
    a["devmode"] = devmode
    a.add_routes(
        [
            web.get(
                "/",
                lambda r: web.Response(
                    text=(
                        "Volcano HTTP\n\n"
                        "GET /status\nGET /on?temp=190\nGET /off\n"
                        "GET /fan/on /fan/off\nGET /pump/on /pump/off\n"
                        "GET /discover\n"
                        "DEV: /settings/snapshot, /dev/read?uuid=..., "
                        "/dev/write/bool|u8|u16le|hex\n"
                    ),
                    content_type="text/plain",
                ),
            ),
            web.get("/status", status),
            web.get("/on", on),
            web.get("/off", off),
            web.get("/fan/on", fan_on),
            web.get("/fan/off", fan_off),
            web.get("/pump/on", fan_on),
            web.get("/pump/off", fan_off),
            web.get("/discover", discover_handler),
            # Dev-Only
            web.get("/settings/snapshot", settings_snapshot),
            web.get("/dev/read", dev_read),
            web.get("/dev/write/bool", dev_write_bool),
            web.get("/dev/write/u8", dev_write_u8),
            web.get("/dev/write/u16le", dev_write_u16le),
            web.get("/dev/write/hex", dev_write_hex),
        ]
    )
    return a


# ==== Main ====
async def main_async(args):
    mac = args.mac or args.addr
    v = VolcanoBLE(
        mac=mac,
        scan_seconds=args.scan,
        keepalive=not args.no_keepalive,
        keepalive_interval=args.keepalive_interval,
        preconnect=not args.no_preconnect,
        devmode=args.devmode,
    )

    runner: Optional[web.AppRunner] = None

    try:

        # HTTP-Server aufsetzen
        app = make_app(v, devmode=args.devmode)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(
            runner, host=args.host, port=args.port
        )
        os.system("clear")
        endpoints = ['on','on?temp=180  (Bsp.)','off','pump/on','pump/off','discover']
        print(f"{ts()}Server : http://{args.host}:{args.port}\n")
        p = "Mögliche URL               : "
        for each in endpoints:
            print(f"{p}http://{args.host}:{args.port}/{each}")
        print()
        await site.start()
        # BLE initialisieren (mit robustem Preconnect)
        await v.startup()

        #Verbindungs-Monitor
        asyncio.create_task(
            monitor_connection(v, interval=args.verbose_interval)
        )

        # Hauptloop
        while True:
            await asyncio.sleep(3600)

    finally:
        print("['MAIN  : 'Shutdown angefordert, räume auf …")
        # HTTP-Server aufräumen
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as e:
                print(f"[MAIN] Fehler bei runner.cleanup(): {e}")
                log_error("Fehler bei runner.cleanup()", e)

        # BLE sauber trennen
        try:
            await v.shutdown()
        except Exception as e:
            print(f"[MAIN] Fehler bei v.shutdown(): {e}")
            log_error("Fehler bei v.shutdown()", e)

def kill_others():
    run = True
    while run:
        run = False
        for process in subprocess.run(["ps", "aux"], capture_output=True,text=True).stdout.split('\n'):
            if 'python3 ' in process \
                    and str(Path(__file__).resolve()).split('/')[-1] in process \
                    and not str(os.getpid()) in process:
                run = True
                for each in process.split(' '):
                    if each.isnumeric():
                        print('Beende laufende Server Instanz pid ' + str(each))
                        os.system('kill -2 ' + str(each))
                        time.sleep(1)
                        break


if __name__ == "__main__":
    kill_others()
    p = argparse.ArgumentParser()
    p.add_argument(
        "--mac",
        type=str,
        default=None,
        help="MAC-Adresse des Volcano (optional; sonst Auto-Scan)",
    )
    p.add_argument(
        "--addr",
        type=str,
        default=None,
        help="(Alias) MAC-Adresse – deprecated, verwende --mac",
    )
    p.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Bind-Adresse (default: 0.0.0.0)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8181,
        help="HTTP-Port (default: 8181)",
    )
    p.add_argument(
        "--scan",
        type=int,
        default=6,
        help="Scan-Dauer (Sekunden)",
    )
    p.add_argument(
        "--no-keepalive",
        action="store_true",
        help="Keep-Alive deaktivieren",
    )
    p.add_argument(
        "--keepalive-interval",
        type=int,
        default=20,
        help="Sekunden zwischen Keep-Alive-Reads",
    )
    p.add_argument(
        "--no-preconnect",
        action="store_true",
        help="Nicht beim Start verbinden",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Gibt regelmäßig den BLE-Verbindungsstatus aus",
    )
    p.add_argument(
        "--verbose-interval",
        type=int,
        default=5,
        help="Intervall in Sekunden für --verbose (default: 5)",
    )
    p.add_argument(
        "--devmode",
        action="store_true",
        help="Entwicklermodus: Dev-Endpoints & Notify-Sniffer",
    )
    args = p.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        # Normaler manueller Abbruch – Shutdown läuft vorher im finally.
        pass
    except Exception as e:
        # FATAL: Alles andere loggen wir in /tmp
        log_error("FATAL: Unbehandelte Exception in main", e)
        raise
