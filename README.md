# SpeakEasy

Wearable that keeps a teacher with ALS teaching: live captions, a fatigue cue on his arm,
and an end-of-class recap in his own cloned voice.

- Laptop runs the app (this folder). Built-in mic, JBL speaker over Bluetooth.
- Arduino 101 + Grove Base Shield in the armband: touch pad (port D2), RGB LCD (any I2C port), built-in IMU.

## Setup (Windows)
1. Put the Vosk model folder `vosk-model-small-en-us-0.15` inside this folder.
2. Copy `.env.example` to `.env` and add your Gemini and ElevenLabs keys.
3. In PowerShell, in this folder:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. Arduino IDE: board "Arduino/Genuino 101" (Intel Curie Boards), library "Grove - LCD RGB Backlight".
   Upload `arduino/speakeasy_wearable/speakeasy_wearable.ino`. Shield switch on 5V. Close Serial Monitor after.
5. Windows sound settings: output = JBL.
6. `python app.py`
   - Captions (projector): http://localhost:5000  (other laptops: http://<this laptop's IP>:5000)
   - Control panel: http://localhost:5000/control

## Files
| File | Owner | Does |
| --- | --- | --- |
| app.py | Nihal | starts everything |
| bus.py | Nihal | in-process event bus |
| hub.py | Nihal | USB serial bridge to the Arduino |
| audio.py | | mic capture (or AUDIO_FILE replay) |
| stt.py | Yasmin | Vosk speech to text |
| web.py + templates/ | Yasmin | captions screen, control panel, demo controls |
| voicebank.py | Yasmin | saves clean speech, ElevenLabs voice clone |
| recap.py | Yasmin | Gemini summary + cloned-voice recap |
| fatigue.py | Daniel | fatigue score (needs align_metrics.py from Daniel) |
| fatigue_simple.py | | fallback used automatically if fatigue.py can't load |
| config.py | | settings (fatigue demo values at the bottom) |
