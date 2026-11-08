# hub.py - serial bridge to the Arduino 101 armband (replaces the Pi version).
# Same bus contract as before: publishes touch.tap / touch.hold,
# listens to state.changed / touch.tap / recap.status and forwards them to the LCD.
import threading, time
import bus, config

_ser = None
_lock = threading.Lock()
_captions_on = True
_accel = None

def _find_port():
    import serial.tools.list_ports as lp
    port = getattr(config, "SERIAL_PORT", None)
    if port:
        return port
    for p in lp.comports():
        if (p.vid == 0x8087) or "Arduino" in (p.description or "") or "101" in (p.description or ""):
            return p.device
    ports = [p.device for p in lp.comports()]
    return ports[0] if ports else None

def _send(line):
    with _lock:
        if _ser is None:
            print(f"[hub] (no Arduino) {line}"); return
        try:
            _ser.write((line + "\n").encode())
        except Exception as e:
            print("[hub] write failed:", e)

def lcd_state(state): _send(f"STATE {state}")
def read_accel(): return _accel

def _reader():
    global _accel
    while True:
        try:
            line = _ser.readline().decode(errors="ignore").strip()
        except Exception as e:
            print("[hub] serial read error:", e); time.sleep(1); continue
        if line == "TAP":
            print("[hub] TAP"); bus.publish("touch.tap", {"source": "pad"})
        elif line == "HOLD":
            print("[hub] HOLD"); bus.publish("touch.hold", {"source": "pad"})
        elif line.startswith("ACC "):
            try:
                _accel = tuple(float(v) for v in line.split()[1:4])
            except ValueError:
                pass
        elif line == "READY":
            print("[hub] Arduino ready")
        elif line:
            print("[hub] from Arduino:", line)

def _on_state(d): _send(f"STATE {d['state']}")
def _on_tap(_):
    global _captions_on
    _captions_on = not _captions_on
    _send(f"CAP {1 if _captions_on else 0}")
def _on_recap(d): _send(f"RECAP {d.get('phase')}")

def start():
    global _ser
    try:
        import serial
        port = _find_port()
        _ser = serial.Serial(port, 115200, timeout=1)
        time.sleep(2)  # board resets when the port opens
        print(f"[hub] Arduino on {port}")
        bus.publish("hub.status", {"connected": True, "port": port})
        threading.Thread(target=_reader, daemon=True).start()
    except Exception as e:
        _ser = None
        print("[hub] no Arduino found, LCD commands print here (use the demo buttons):", e)
        bus.publish("hub.status", {"connected": False, "port": None})
    bus.subscribe("state.changed", _on_state, inline=True)
    bus.subscribe("touch.tap", _on_tap, inline=True)
    bus.subscribe("recap.status", _on_recap, inline=True)
