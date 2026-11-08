# hub.py - link to the Arduino 101 armband, over USB or Bluetooth LE.
# Same bus contract as before: publishes touch.tap / touch.hold / hub.status,
# listens to state.changed / touch.tap / recap.status / metrics.minute and forwards them to the LCD.
#
# HUB_MODE in .env:  auto (default) = USB if the armband is plugged in, otherwise Bluetooth
#                    usb = USB only     ble = Bluetooth only
import asyncio, os, threading, time
import bus, config

SERVICE = "19b10000-e8f2-537e-4f6c-d104768a1214"
EVT_CHAR = "19b10001-e8f2-537e-4f6c-d104768a1214"   # armband -> laptop (notify)
CMD_CHAR = "19b10002-e8f2-537e-4f6c-d104768a1214"   # laptop -> armband (write)
BLE_NAME = "SpkEasy"

_mode = None                 # "usb" | "ble" | None
_ser = None
_ble_client = None
_ble_loop = None
_ble_started = False
_lock = threading.Lock()
_captions_on = True
_accel = None
_last = {}                   # last command of each kind, re-sent after a reconnect

# ---------- shared ----------
def is_connected():
    return (_mode == "usb" and _ser is not None) or (_mode == "ble" and _ble_client is not None)

def read_accel():
    return _accel

def _handle_line(line):
    global _accel
    if line == "TAP":
        print(f"[hub] TAP ({_mode})"); bus.publish("touch.tap", {"source": "pad"})
    elif line == "HOLD":
        print(f"[hub] HOLD ({_mode})"); bus.publish("touch.hold", {"source": "pad"})
    elif line.startswith("ACC "):
        try:
            _accel = tuple(float(v) for v in line.split()[1:4])
        except ValueError:
            pass
    elif line == "READY":
        print(f"[hub] armband ready ({_mode})")
        _resend()
    elif line:
        print("[hub] from armband:", line)

def _send(line):
    kind = line.split(" ", 1)[0]
    _last[kind] = line
    if _mode == "usb" and _ser is not None:
        with _lock:
            try:
                _ser.write((line + "\n").encode())
            except Exception as e:
                print("[hub] USB write failed:", e)
    elif _mode == "ble" and _ble_client is not None and _ble_loop is not None:
        data = line.encode()[:20]
        asyncio.run_coroutine_threadsafe(_ble_write(data), _ble_loop)
    else:
        print(f"[hub] (armband not connected) {line}")

def _resend():
    """After (re)connecting, put the LCD back in the right state."""
    for kind in ("CAP", "STATE", "METRICS"):
        if kind in _last:
            _send(_last[kind])

# ---------- USB ----------
def _find_usb_port():
    import serial.tools.list_ports as lp
    if getattr(config, "SERIAL_PORT", None):
        return config.SERIAL_PORT
    for p in lp.comports():
        desc = (p.description or "") + " " + (p.manufacturer or "")
        if p.vid == 0x8087 or "Arduino" in desc or "101" in desc:
            return p.device
    return None

def _usb_reader():
    global _ser
    while _ser is not None:
        try:
            line = _ser.readline().decode(errors="ignore").strip()
        except Exception as e:
            print("[hub] USB disconnected:", e)
            try:
                _ser.close()
            except Exception:
                pass
            _ser = None
            bus.publish("hub.status", {"connected": False, "port": None})
            if _hub_mode() == "auto":
                print("[hub] switching to Bluetooth")
                _start_ble()
            return
        if line:
            _handle_line(line)

def _start_usb():
    global _ser, _mode
    import serial
    port = _find_usb_port()
    if not port:
        return False
    _ser = serial.Serial(port, 115200, timeout=1)
    time.sleep(2)            # board resets when the port opens
    _mode = "usb"
    print(f"[hub] armband on USB ({port})")
    bus.publish("hub.status", {"connected": True, "port": port})
    threading.Thread(target=_usb_reader, daemon=True).start()
    _resend()
    return True

# ---------- Bluetooth LE ----------
async def _ble_write(data):
    try:
        if _ble_client is not None:
            await _ble_client.write_gatt_char(CMD_CHAR, data, response=False)
    except Exception as e:
        print("[hub] Bluetooth write failed:", e)

async def _ble_main():
    global _ble_client, _mode
    from bleak import BleakClient, BleakScanner
    said_looking = False
    while True:
        if _mode == "usb" and _ser is not None:      # plugged back in: let USB handle it
            await asyncio.sleep(2); continue
        if not said_looking:
            print("[hub] looking for the armband over Bluetooth...")
            said_looking = True
        dev = await BleakScanner.find_device_by_filter(
            lambda d, adv: (d.name or adv.local_name or "") in (BLE_NAME, "SpeakEasy")
            or SERVICE in [u.lower() for u in (adv.service_uuids or [])],
            timeout=10.0)
        if dev is None:
            continue
        try:
            async with BleakClient(dev) as client:
                def on_notify(_, data):
                    _handle_line(bytes(data).decode(errors="ignore").strip())
                await client.start_notify(EVT_CHAR, on_notify)
                _ble_client, _mode = client, "ble"
                said_looking = False
                print(f"[hub] armband connected over Bluetooth ({dev.address})")
                bus.publish("hub.status", {"connected": True, "port": "Bluetooth"})
                _resend()
                while client.is_connected:
                    await asyncio.sleep(1)
        except Exception as e:
            print("[hub] Bluetooth connection failed:", str(e)[:150])
        if _ble_client is not None:
            print("[hub] armband Bluetooth disconnected, reconnecting...")
            bus.publish("hub.status", {"connected": False, "port": None})
        _ble_client = None
        if _mode == "ble":
            _mode = None
        await asyncio.sleep(2)

def _start_ble():
    global _ble_started, _ble_loop
    if _ble_started:
        return
    try:
        import bleak  # noqa: F401
    except ImportError:
        print('[hub] Bluetooth needs the bleak package: pip install bleak')
        return
    _ble_started = True
    def run():
        global _ble_loop
        _ble_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_ble_loop)
        _ble_loop.run_until_complete(_ble_main())
    threading.Thread(target=run, daemon=True).start()

# ---------- bus handlers ----------
def _on_state(d): _send(f"STATE {d['state']}")
def _on_tap(_):
    global _captions_on
    _captions_on = not _captions_on
    _send(f"CAP {1 if _captions_on else 0}")
def _on_recap(d): _send(f"RECAP {d.get('phase')}")
def _on_metrics(d):
    """Once a minute: speaking rate + clarity for the LCD's second line."""
    wpm = d.get("wpm")
    if not wpm:
        return                               # demo button values have no measurements
    clar = d.get("accuracy") if d.get("accuracy") is not None else d.get("mean_conf")
    _send(f"METRICS {int(round(wpm))} {int(round((clar or 0) * 100))}")

def _hub_mode():
    return (getattr(config, "HUB_MODE", None) or os.getenv("HUB_MODE") or "auto").lower()

def start():
    bus.subscribe("state.changed", _on_state, inline=True)
    bus.subscribe("touch.tap", _on_tap, inline=True)
    bus.subscribe("recap.status", _on_recap, inline=True)
    bus.subscribe("metrics.minute", _on_metrics, inline=True)
    mode = _hub_mode()
    if mode in ("auto", "usb"):
        try:
            if _start_usb():
                if mode == "auto":
                    _start_ble()     # stays idle while USB works; takes over if the cable is pulled
                return
        except Exception as e:
            print("[hub] USB failed:", e)
        if mode == "usb":
            print("[hub] no armband on USB (use the demo buttons)")
            bus.publish("hub.status", {"connected": False, "port": None})
            return
    _start_ble()