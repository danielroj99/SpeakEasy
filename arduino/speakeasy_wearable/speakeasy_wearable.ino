// speakeasy_wearable.ino - Arduino 101 armband (Grove Base Shield).
// Touch: Grove D2 port. LCD RGB: any Grove I2C port. Motion: the 101's built-in IMU (no Grove accel needed).
// Serial 115200 to the laptop.
//   Arduino -> laptop:  READY | TAP | HOLD | ACC x y z  (g, every 500 ms)
//   laptop -> Arduino:  STATE fresh|tiring|recap_ready  |  CAP 0|1  |  RECAP generating|playing|done|error
#include <Wire.h>
#include <CurieIMU.h>

// ---- Grove LCD RGB Backlight driver (no library needed) ----
// Text controller at 0x3E. Backlight at 0x62 (v4) or 0x30 (v5), auto-detected.
const uint8_t LCD_ADDR = 0x3E;
uint8_t rgbAddr = 0x62;

bool i2cAlive(uint8_t a) { Wire.beginTransmission(a); return Wire.endTransmission() == 0; }
void lcdCmd(uint8_t c) { Wire.beginTransmission(LCD_ADDR); Wire.write(0x80); Wire.write(c); Wire.endTransmission(); delay(2); }
void lcdChar(uint8_t c) { Wire.beginTransmission(LCD_ADDR); Wire.write(0x40); Wire.write(c); Wire.endTransmission(); }
void rgbReg(uint8_t r, uint8_t v) { Wire.beginTransmission(rgbAddr); Wire.write(r); Wire.write(v); Wire.endTransmission(); }

struct Lcd {
  void begin() {
    delay(50);
    for (int i = 0; i < 3; i++) { lcdCmd(0x28); delay(5); }   // 2 lines
    lcdCmd(0x0C);                                             // display on, no cursor
    clear();
    lcdCmd(0x06);                                             // left to right
    rgbAddr = i2cAlive(0x62) ? 0x62 : 0x30;
    if (rgbAddr == 0x62) { rgbReg(0x00, 0x00); rgbReg(0x01, 0x00); rgbReg(0x08, 0xAA); }
    else { rgbReg(0x00, 0x07); delay(2); rgbReg(0x04, 0x15); }
  }
  void clear() { lcdCmd(0x01); delay(2); }
  void setCursor(uint8_t col, uint8_t row) { lcdCmd((row == 0 ? 0x80 : 0xC0) + col); }
  void print(const String &s) { for (unsigned i = 0; i < s.length() && i < 16; i++) lcdChar(s[i]); }
  void setRGB(uint8_t r, uint8_t g, uint8_t b) {
    if (rgbAddr == 0x62) { rgbReg(0x04, r); rgbReg(0x03, g); rgbReg(0x02, b); }
    else { rgbReg(0x06, r); rgbReg(0x07, g); rgbReg(0x08, b); }
  }
};

const int TOUCH_PIN = 2;
const int VIBE_PIN = 3;      // optional Grove vibration motor in port D3 (idea from Yasmin's pod sketch)
const unsigned long TAP_MAX_MS = 600, HOLD_MS = 1200;

Lcd lcd;
String state = "fresh", recapPhase = "done", inbuf = "";
bool captionsOn = true;
bool lastTouch = false, holdSent = false;
unsigned long pressT = 0, lastAcc = 0, lastBlink = 0;
bool blinkOn = false;

void buzz(int ms) { digitalWrite(VIBE_PIN, HIGH); delay(ms); digitalWrite(VIBE_PIN, LOW); }

void setColor() {
  if (state == "tiring")           lcd.setRGB(255, 140, 0);
  else if (state == "recap_ready") lcd.setRGB(150, 0, 255);
  else                              lcd.setRGB(0, 180, 0);
}

void show() {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(captionsOn ? "CAPTIONS ON" : "CAPTIONS OFF");
  lcd.setCursor(0, 1);
  lcd.print(state == "recap_ready" ? "recap ready" : state);
  setColor();
}

bool recapActive() { return recapPhase == "generating" || recapPhase == "playing"; }

void handle(String line) {
  line.trim();
  if (line.startsWith("STATE ")) {
    String s = line.substring(6);
    if (s == "tiring" && state != "tiring") buzz(150);   // private nudge when his voice starts tiring
    state = s;
    if (!recapActive()) show();
  }
  else if (line.startsWith("CAP ")) { captionsOn = line.substring(4) == "1"; if (!recapActive()) show(); }
  else if (line.startsWith("RECAP ")) {
    recapPhase = line.substring(6);
    if (recapActive()) {
      lcd.clear(); lcd.setCursor(0, 0); lcd.print("RECAP");
      lcd.setCursor(0, 1); lcd.print(recapPhase == "generating" ? "making recap..." : "recap playing");
    } else show();
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(TOUCH_PIN, INPUT);
  pinMode(VIBE_PIN, OUTPUT);
  pinMode(LED_BUILTIN, OUTPUT);
  Wire.begin();
  lcd.begin();
  CurieIMU.begin();
  CurieIMU.setAccelerometerRange(4);   // +/- 4 g
  show();
  Serial.println("READY");
}

void loop() {
  unsigned long now = millis();

  // touch: tap vs hold
  bool t = digitalRead(TOUCH_PIN) == HIGH;
  if (t && !lastTouch) { pressT = now; holdSent = false; }
  if (t && !holdSent && now - pressT >= HOLD_MS) { Serial.println("HOLD"); holdSent = true; buzz(80); }
  if (!t && lastTouch && !holdSent && now - pressT < TAP_MAX_MS) Serial.println("TAP");
  lastTouch = t;

  // commands from the laptop
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { handle(inbuf); inbuf = ""; }
    else if (c != '\r' && inbuf.length() < 64) inbuf += c;
  }

  // purple blink while a recap runs
  if (recapActive() && now - lastBlink > 500) {
    blinkOn = !blinkOn; lastBlink = now;
    if (blinkOn) lcd.setRGB(150, 0, 255); else lcd.setRGB(20, 0, 40);
  }

  if (!recapActive()) digitalWrite(LED_BUILTIN, state != "fresh");   // on-board LED mirrors the state

  // accelerometer stream (for fall detection later)
  if (now - lastAcc > 500) {
    float x, y, z;
    CurieIMU.readAccelerometerScaled(x, y, z);
    Serial.print("ACC "); Serial.print(x, 2); Serial.print(' ');
    Serial.print(y, 2); Serial.print(' '); Serial.println(z, 2);
    lastAcc = now;
  }
  delay(10);
}
