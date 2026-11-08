# app.py - SpeakEasy. Runs everything on the laptop; the Arduino 101 armband connects over USB.
#   python app.py   ->  captions: http://localhost:5000   control panel: http://localhost:5000/control
import importlib, time
import config

def _fatigue():
    """Daniel's fatigue.py (needs align_metrics.py). Falls back to fatigue_simple.py if it can't load."""
    try:
        return importlib.import_module("fatigue")
    except Exception as e:
        print(f"[app] fatigue.py could not load ({e}); using fatigue_simple.py instead")
        return importlib.import_module("fatigue_simple")

def _optional(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None

if __name__ == "__main__":
    import hub, web, stt, voicebank, recap, audio
    mods = [hub, _optional("store"), _fatigue(), web, stt, voicebank, recap]
    for m in mods:
        if m:
            m.start()
    audio.start()   # last, so every listener is subscribed before audio flows
    print(f"SpeakEasy running: captions http://localhost:{config.WEB_PORT}   "
          f"control http://localhost:{config.WEB_PORT}/control   (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
