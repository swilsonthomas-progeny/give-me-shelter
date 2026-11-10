// Record player puck for the Elecrow CrowPanel 2.1" rotary display
// (ESP32-S3R8, 480x480 round ST7701 RGB panel, knob ring with press).
//
// The puck holds no Spotify logic. It asks the turntable server on the Pi for
// the shelf, cover art and deck state, and tells it what the knob did.
// server/sim.html is the same interface in a browser: keep the two in step.
//
//   Shelf: turn = browse albums, press = play, long press = back to the record
//   Deck:  turn = volume, press = lift / drop the needle,
//          long press = back to the shelf (the music keeps playing)

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Arduino_GFX_Library.h>
#include <JPEGDEC.h>
#include <vector>
#include "fonts/FreeSansBold18pt7b.h"
#include "fonts/FreeSans12pt7b.h"
#include "fonts/FreeSans9pt7b.h"
#include "fonts/FreeSerifBold24pt7b.h"
#include "config.h"

// ---- board (pins and panel setup are from Elecrow's Board21.h example) ------
static const int I2C_SDA_PIN = 38, I2C_SCL_PIN = 39;
static const int BACKLIGHT_PIN = 6, LED_PIN = 43;
static const int ENCODER_A_PIN = 42, ENCODER_B_PIN = 4;
static const uint8_t PCF_ADDR = 0x21;     // I/O expander: P0 touch reset, P2 touch int, P3 LCD power, P4 LCD reset, P5 knob button
static const int W = 480;

static Arduino_DataBus *bus = new Arduino_SWSPI(GFX_NOT_DEFINED, 16, 2, 1, GFX_NOT_DEFINED);
static Arduino_ESP32RGBPanel *rgb = new Arduino_ESP32RGBPanel(
    40, 7, 15, 41, 46, 3, 8, 18, 17, 14, 13, 12, 11, 10, 9,
    5, 45, 48, 47, 21, 1, 10, 4, 20, 1, 10, 4, 20, 0, 12000000,
    false, 0, 0, 480 * 20);
static Arduino_RGB_Display *gfx = new Arduino_RGB_Display(
    W, W, rgb, 0, true, bus, GFX_NOT_DEFINED, st7701_type5_init_operations, sizeof(st7701_type5_init_operations));
static Arduino_Canvas *cv = new Arduino_Canvas(W, W, gfx);   // everything is drawn here, then flushed
static uint16_t *fb = nullptr;                               // the canvas pixels
static uint16_t *coverPx = nullptr;                          // the decoded cover, kept for redraws
static JPEGDEC jpeg;

static uint8_t pcfOut = 0xFF;
static void pcfSet(uint8_t pin, bool high) {
  pcfOut = high ? (pcfOut | (1 << pin)) : (pcfOut & ~(1 << pin));
  Wire.beginTransmission(PCF_ADDR);
  Wire.write(pcfOut | 0x20);              // P5 stays high so it can be read as an input
  Wire.endTransmission();
}
static bool knobDown() {                  // active low
  if (Wire.requestFrom(PCF_ADDR, (uint8_t)1) != 1) return false;
  return !(Wire.read() & 0x20);
}
static void boardBegin() {
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  pcfOut = 0xE2;                          // P0, P2, P3, P4 low to start
  pcfSet(3, true);  delay(50);            // LCD power
  pcfSet(4, false); delay(100);           // LCD reset
  pcfSet(4, true);  delay(100);
  pcfSet(0, false); delay(100);           // touch reset
  pcfSet(0, true);  delay(100);
  pcfSet(2, true);
  ledcAttach(BACKLIGHT_PIN, 5000, 8);
  ledcWrite(BACKLIGHT_PIN, 0);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  gfx->begin();
  bus->beginWrite();                      // panel orientation for this board
  bus->writeCommand(0x36);
  bus->write(0x08);
  bus->endWrite();
  gfx->fillScreen(0);
}

static volatile int32_t encCount = 0;
static volatile uint8_t encState = 0;
static const int8_t DRAM_ATTR ENC_TABLE[16] = {0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0};
static void IRAM_ATTR encISR() {
  uint8_t s = (digitalRead(ENCODER_A_PIN) << 1) | digitalRead(ENCODER_B_PIN);
  encCount += ENC_TABLE[(encState << 2) | s];
  encState = s;
}

// ---- layout (matches sim.html) ---------------------------------------------
static const int C = 240;
static const float ARM_PX = 400, ARM_PY = 80, ARM_LEN = 200;
static const float ARM_REST = 72, ARM_OUTER = 63, ARM_INNER = 32;
static const size_t MAX_JPG = 200 * 1024;

#define C565(r, g, b) ((uint16_t)RGB565(r, g, b))
static const uint16_t COL_CREAM = C565(0xF2, 0xEA, 0xD8), COL_SOFT = C565(0xD8, 0xCF, 0xC0), COL_DIM = C565(0xA8, 0x9E, 0x8E);
static const uint16_t COL_SLEEVE = C565(0x16, 0x12, 0x0F), COL_VINYL = C565(0x0C, 0x0C, 0x0C), COL_GROOVE = C565(0x2A, 0x2A, 0x2A);
static const uint16_t COL_ARM = C565(0xC9, 0xC5, 0xBD), COL_HEAD = C565(0xEF, 0xEA, 0xE0), COL_PIVOT = C565(0x8F, 0x8B, 0x84);

// ---- state shared with the network task ------------------------------------
struct DeckState {
  String mode = "idle", album, artist, title, side, err, version;
  int vol = 40, sides = 1;
  long pos = 0, dur = 0;
  uint32_t at = 0;                        // millis() when pos was read
  bool auth = true, reachable = false;
};
struct Art {
  String want, have;
  uint8_t *buf = nullptr;
  size_t len = 0;
  bool fresh = false;
};

SemaphoreHandle_t mux;
DeckState st;
Art art;
String cmdPath;                           // one waiting command (play / press)
int volWanted = -1;
String serverBase;

String shelfText, shelfVersion;           // the server's shelf, one album per line
std::vector<uint32_t> shelfOffsets;       // where each album's line starts in shelfText

struct Lock {
  Lock() { xSemaphoreTake(mux, portMAX_DELAY); }
  ~Lock() { xSemaphoreGive(mux); }
};

// ---- network task -----------------------------------------------------------
static void parseState(const String &body) {
  JsonDocument doc;
  if (deserializeJson(doc, body)) return;
  Lock l;
  st.mode = doc["mode"] | "idle";
  st.album = doc["album"] | "";
  st.artist = doc["artist"] | "";
  st.title = doc["title"] | "";
  st.side = doc["side"] | "";
  st.err = doc["err"] | "";
  st.version = doc["v"] | "";
  st.vol = doc["vol"] | 40;
  st.sides = doc["sides"] | 1;
  st.pos = doc["pos"] | 0;
  st.dur = doc["dur"] | 0;
  st.auth = doc["auth"] | true;
  st.at = millis();
  st.reachable = true;
}

static bool request(const char *method, const String &path, String &out) {
  HTTPClient http;
  http.setConnectTimeout(3000);
  http.setTimeout(15000);
  if (!http.begin(serverBase + path)) return false;
  int code = strcmp(method, "POST") == 0 ? http.POST("") : http.GET();
  bool ok = code == 200;
  if (ok) out = http.getString();
  http.end();
  return ok;
}

static bool resolveServer() {
  IPAddress ip;
  String host = SERVER_HOST;
  if (!ip.fromString(host)) {
    static bool mdns = false;
    if (!mdns) mdns = MDNS.begin("record-puck");
    if (host.endsWith(".local")) host = host.substring(0, host.length() - 6);
    ip = MDNS.queryHost(host, 2000);
    if (ip == IPAddress((uint32_t)0)) return false;
  }
  serverBase = "http://" + ip.toString() + ":" + String(SERVER_PORT);
  return true;
}

static void fetchShelf(const String &version) {
  String body;
  if (!request("GET", "/api/shelf", body) || !body.startsWith(version)) return;
  std::vector<uint32_t> offsets;
  int at = body.indexOf('\n');            // skip the header line
  while (at >= 0 && at + 1 < (int)body.length()) {
    int next = body.indexOf('\n', at + 1);
    if ((next < 0 ? (int)body.length() : next) - at > 23) offsets.push_back(at + 1);
    at = next;
  }
  Lock l;
  shelfText = body;
  shelfOffsets.swap(offsets);
  shelfVersion = version;
}

static void fetchArt() {
  String want;
  { Lock l; want = art.want; }
  HTTPClient http;
  http.setTimeout(8000);
  uint8_t *buf = nullptr;
  size_t len = 0;
  if (http.begin(serverBase + "/art/" + want + "?s=" + String(W)) && http.GET() == 200) {
    int size = http.getSize();
    if (size > 0 && (size_t)size <= MAX_JPG && (buf = (uint8_t *)ps_malloc(size))) {
      WiFiClient *s = http.getStreamPtr();
      uint32_t until = millis() + 8000;
      while (len < (size_t)size && millis() < until) {
        int n = s->read(buf + len, size - len);
        if (n > 0) len += n; else delay(2);
      }
      if (len != (size_t)size) { free(buf); buf = nullptr; }
    }
  }
  http.end();
  Lock l;
  free(art.buf);
  art.buf = buf;
  art.len = buf ? len : 0;
  art.have = want;                        // even on failure, so we do not hammer the server
  art.fresh = buf != nullptr;
}

static void netTask(void *) {
  uint32_t lastPoll = 0, lastVol = 0;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED || (serverBase.isEmpty() && !resolveServer())) {
      { Lock l; st.reachable = false; }
      delay(1000);
      continue;
    }
    String cmd, body, version, have;
    int vol = -1;
    bool needArt;
    {
      Lock l;
      cmd = cmdPath; cmdPath = "";
      if (volWanted >= 0 && millis() - lastVol > 200) { vol = volWanted; volWanted = -1; }
      needArt = art.want.length() && art.want != art.have;
      version = st.version; have = shelfVersion;
    }
    if (cmd.length()) {
      if (request("POST", cmd, body)) parseState(body);
    } else if (vol >= 0) {
      lastVol = millis();
      if (request("POST", "/api/volume?v=" + String(vol), body)) parseState(body);
    } else if (version.length() && version != have) {
      fetchShelf(version);
    } else if (needArt) {
      fetchArt();
    } else if (millis() - lastPoll > 1000) {
      lastPoll = millis();
      if (request("GET", "/api/state", body)) parseState(body);
      else { Lock l; st.reachable = false; serverBase = ""; }
    } else {
      delay(20);
    }
  }
}

// ---- drawing ----------------------------------------------------------------
static inline uint16_t shade(uint16_t c, uint32_t keep) {      // keep: 0 (black) .. 256 (unchanged)
  return (uint16_t)(((((uint32_t)(c & 0xF81F)) * keep >> 8) & 0xF81F) | ((((uint32_t)(c & 0x07E0)) * keep >> 8) & 0x07E0));
}
static void darken(int y0, int y1, uint32_t keep) {
  for (uint16_t *p = fb + y0 * W, *end = fb + y1 * W; p < end; p++) *p = shade(*p, keep);
}
static void wideLine(float x0, float y0, float x1, float y1, float half, uint16_t color) {
  float dx = x1 - x0, dy = y1 - y0, len = sqrtf(dx * dx + dy * dy);
  if (len < 0.5f) return;
  float nx = -dy / len * half, ny = dx / len * half;
  cv->fillTriangle(x0 + nx, y0 + ny, x0 - nx, y0 - ny, x1 + nx, y1 + ny, color);
  cv->fillTriangle(x0 - nx, y0 - ny, x1 - nx, y1 - ny, x1 + nx, y1 + ny, color);
  cv->fillCircle(x0, y0, half, color);
  cv->fillCircle(x1, y1, half, color);
}
static void ring(int rInner, uint16_t color) {                 // from rInner out to the edge of the glass
  for (int y = 0; y < W; y++) {
    int dy = y - C;
    int xo = (int)sqrtf((float)(C * C - dy * dy));
    int xi = abs(dy) < rInner ? (int)sqrtf((float)(rInner * rInner - dy * dy)) : 0;
    uint16_t *row = fb + y * W;
    for (int x = max(0, C - xo); x < C - xi; x++) row[x] = color;
    for (int x = C + xi; x < min(W, C + xo); x++) row[x] = color;
  }
}
static void arcDots(float r, float a0, float a1, int dotR, uint16_t color) {
  for (float a = a0; a <= a1; a += 1.5f)
    cv->fillCircle(C + r * cosf(a * DEG_TO_RAD), C + r * sinf(a * DEG_TO_RAD), dotR, color);
}
static int chordWidth(int y) {             // usable text width at height y on a round screen
  float d = 236.0f * 236.0f - (float)(y - C) * (y - C);
  return d > 0 ? (int)(2 * sqrtf(d)) - 32 : 0;
}
static int textWidth(const String &s) {
  int16_t x1, y1; uint16_t w, h;
  cv->getTextBounds(s, 0, 0, &x1, &y1, &w, &h);
  return w;
}
static void text(String s, int baseline, const GFXfont *font, uint16_t color, uint8_t size = 1) {
  cv->setFont(font);
  cv->setTextSize(size);
  cv->setTextWrap(false);
  cv->setTextColor(color);
  int w = chordWidth(baseline);
  if (textWidth(s) > w) {
    while (s.length() > 1 && textWidth(s + "...") > w) s.remove(s.length() - 1);
    s.trim();
    s += "...";
  }
  int16_t x1, y1; uint16_t tw, th;
  cv->getTextBounds(s, 0, baseline, &x1, &y1, &tw, &th);
  cv->setCursor(C - tw / 2 - x1, baseline);
  cv->print(s);
}
static void banner(const String &a, const String &b) {
  darken(284, 392, 76);
  text(a, 334, &FreeSansBold18pt7b, COL_CREAM);
  if (b.length()) text(b, 372, &FreeSans12pt7b, COL_SOFT);
}

// ---- interface --------------------------------------------------------------
enum View { SHELF, DECK };
View view = SHELF;
int shelfIndex = 0;
String curId, curArtist, curTitle, curYear;   // album under the knob
String coverId;                               // album whose cover is in coverPx
String pendingAlbum, lastMode, lastAlbum, lastSide, toast;
bool dirty = true;
uint32_t lastTurn = 0, lastInput = 0, lastFrame = 0, lastDraw = 0;
uint32_t volShown = 0, startShown = 0, loadingUntil = 0, toastUntil = 0;
int localVol = 40;
float arm = ARM_REST;
int32_t encLast = 0, encRest = 0;

static bool loadAlbum(int i) {             // caller holds the lock
  if (i < 0 || i >= (int)shelfOffsets.size()) return false;
  int start = shelfOffsets[i], end = shelfText.indexOf('\n', start);
  String line = shelfText.substring(start, end < 0 ? shelfText.length() : end);
  int a = line.indexOf('\t'), b = line.indexOf('\t', a + 1), c = line.indexOf('\t', b + 1);
  if (a < 0 || b < 0 || c < 0) return false;
  curId = line.substring(0, a);
  curArtist = line.substring(a + 1, b);
  curTitle = line.substring(b + 1, c);
  curYear = line.substring(c + 1);
  curYear.trim();
  return true;
}
static int findAlbum(const String &id) {   // caller holds the lock
  if (id.isEmpty()) return -1;
  for (size_t i = 0; i < shelfOffsets.size(); i++)
    if (shelfText.startsWith(id + "\t", shelfOffsets[i])) return i;
  return -1;
}
static int jpgRow(JPEGDRAW *d) {
  for (int row = 0; row < d->iHeight; row++) {
    int y = d->y + row, w = min(d->iWidthUsed, W - d->x);
    if (y < 0 || y >= W || d->x < 0 || w <= 0) continue;
    memcpy(coverPx + y * W + d->x, d->pPixels + row * d->iWidth, w * 2);
  }
  return 1;
}
static bool paintCover(const String &id) { // cover as the background, or a plain sleeve
  if (id.length() && id == coverId) { memcpy(fb, coverPx, W * W * 2); return true; }
  cv->fillScreen(COL_SLEEVE);
  return false;
}
static void showToast(const String &t) { toast = t; toastUntil = millis() + 4000; dirty = true; }

static void drawShelf(const DeckState &s) {
  if (curId.isEmpty()) {
    cv->fillScreen(COL_SLEEVE);
    text(!s.reachable ? "Looking for the turntable" : !s.auth ? "Not connected to Spotify" : "Reading your shelf...", 248, &FreeSans12pt7b, COL_CREAM);
    return;
  }
  if (paintCover(curId)) {
    for (int y = 276; y < W; y++) darken(y, y + 1, 256 - min(230, (y - 276) * 5 / 2));   // so the words read
  } else {
    String key = curArtist, low = curArtist;
    low.toLowerCase();
    if (low.startsWith("the ")) key = key.substring(4);
    else if (low.startsWith("an ")) key = key.substring(3);
    else if (low.startsWith("a ")) key = key.substring(2);
    char ch = key.length() ? toupper(key[0]) : '#';
    if (ch < 'A' || ch > 'Z') ch = '#';
    text(String(ch), 250, &FreeSerifBold24pt7b, C565(0x4A, 0x3F, 0x37), 4);
  }
  text(curArtist, 370, &FreeSansBold18pt7b, COL_CREAM);
  text(curTitle, 404, &FreeSans12pt7b, COL_SOFT);
  text(curYear, 434, &FreeSans9pt7b, COL_DIM);
  float at = (-90.0f + 360.0f * shelfIndex / max(1, (int)shelfOffsets.size())) * DEG_TO_RAD;   // where you are along the shelf
  cv->fillCircle(C + 232 * cosf(at), C + 232 * sinf(at), 6, COL_CREAM);
  if (s.mode == "playing") {                // a record is still playing
    cv->fillCircle(C, 46, 20, 0);
    cv->fillCircle(C, 46, 13, COL_CREAM);
    cv->fillCircle(C, 46, 4, 0);
  }
}

static void drawDeck(const DeckState &s) {
  uint32_t now = millis();
  bool playing = s.mode == "playing";
  String showing = pendingAlbum.length() ? pendingAlbum : s.album;
  paintCover(showing);
  if (!playing) darken(0, W, 144);          // needle up: the cover dims
  ring(226, COL_VINYL);                     // edge of the record
  cv->drawCircle(C, C, 230, COL_GROOVE);
  cv->drawCircle(C, C, 235, COL_GROOVE);
  cv->fillCircle(C, C, 18, COL_VINYL);      // spindle
  cv->fillCircle(C, C, 6, COL_ARM);

  float dir = (135.0f - arm) * DEG_TO_RAD;  // tonearm
  float sx = ARM_PX + ARM_LEN * cosf(dir), sy = ARM_PY + ARM_LEN * sinf(dir);
  wideLine(ARM_PX, ARM_PY, sx, sy, 5, COL_ARM);
  wideLine(sx - 18 * cosf(dir), sy - 18 * sinf(dir), sx, sy, 9, COL_HEAD);
  cv->fillCircle(ARM_PX, ARM_PY, 26, COL_PIVOT);
  cv->fillCircle(ARM_PX, ARM_PY, 10, COL_GROOVE);

  if (s.mode == "flip") banner("Flip to side " + String((char)(s.side[0] + 1)), "press to turn it over");
  else if (s.mode == "end") banner("Finished", "press to play it again");
  else if (now < loadingUntil && !playing) banner("Putting it on...", curTitle);
  else if (playing && now - startShown < 3500) banner(s.sides > 1 ? "Side " + s.side : s.title, s.sides > 1 ? s.title : s.artist);
  if (now - volShown < 1500) {
    arcDots(228, 135, 405, 8, 0);
    if (localVol > 0) arcDots(228, 135, 135 + 270.0f * localVol / 100, 5, COL_CREAM);
  }
}

static void turned(int steps) {
  lastInput = millis();
  if (view == SHELF) {
    Lock l;
    int n = shelfOffsets.size();
    if (!n) return;
    shelfIndex = ((shelfIndex + steps) % n + n) % n;
    loadAlbum(shelfIndex);
    lastTurn = millis();
  } else {
    localVol = constrain(localVol + steps * 3, 0, 100);
    volShown = millis();
    Lock l;
    volWanted = localVol;
  }
  dirty = true;
}

static void pressed(const DeckState &s) {
  lastInput = millis();
  if (view == SHELF) {
    if (curId.isEmpty()) return;
    view = DECK;
    if (!(s.album == curId && s.mode != "idle")) {
      loadingUntil = millis() + 8000;
      pendingAlbum = curId;
      Lock l;
      cmdPath = "/api/play?album=" + curId;
    }
  } else if (pendingAlbum.isEmpty()) {
    Lock l;
    cmdPath = "/api/press";
  }
  dirty = true;
}

static void held(const DeckState &s) {
  lastInput = millis();
  if (view == DECK) {
    view = SHELF;
    Lock l;
    int i = findAlbum(s.album);
    if (i >= 0) { shelfIndex = i; loadAlbum(i); }
    lastTurn = 0;
  } else if (s.album.length() && s.mode != "idle") {
    view = DECK;
  }
  dirty = true;
}

void setup() {
  Serial.begin(115200);
  mux = xSemaphoreCreateMutex();
  boardBegin();
  coverPx = (uint16_t *)ps_malloc(W * W * 2);
  if (!cv->begin(GFX_SKIP_OUTPUT_BEGIN) || !(fb = cv->getFramebuffer()) || !coverPx) {
    gfx->setCursor(150, 236);
    gfx->print("Out of memory (is PSRAM enabled?)");
    ledcWrite(BACKLIGHT_PIN, 200);
    for (;;) delay(1000);
  }
  cv->fillScreen(COL_SLEEVE);
  cv->flush();
  ledcWrite(BACKLIGHT_PIN, 200);

  pinMode(ENCODER_A_PIN, INPUT);
  pinMode(ENCODER_B_PIN, INPUT);
  encState = (digitalRead(ENCODER_A_PIN) << 1) | digitalRead(ENCODER_B_PIN);
  attachInterrupt(digitalPinToInterrupt(ENCODER_A_PIN), encISR, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENCODER_B_PIN), encISR, CHANGE);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  xTaskCreatePinnedToCore(netTask, "net", 8192, nullptr, 1, nullptr, 0);
  lastFrame = lastInput = millis();
}

void loop() {
  uint32_t now = millis();
  float dt = min(0.1f, (now - lastFrame) / 1000.0f);
  lastFrame = now;

  DeckState s;
  { Lock l; s = st; }

  // things the server told us
  if (s.err.length() && s.err != toast) showToast(s.err);
  if (now - volShown > 2000) localVol = s.vol;
  if (pendingAlbum.length() && ((s.album == pendingAlbum && s.mode != "idle") || now > loadingUntil)) {
    pendingAlbum = "";
    loadingUntil = 0;
    dirty = true;
  }
  if (s.mode != lastMode || s.album != lastAlbum || s.side != lastSide) {
    if (s.mode == "playing" && (s.album != lastAlbum || s.side != lastSide || lastMode == "flip" || lastMode == "end" || lastMode == "idle")) startShown = now;
    if (s.mode == "idle" && view == DECK && pendingAlbum.isEmpty()) view = SHELF;
    lastMode = s.mode; lastAlbum = s.album; lastSide = s.side;
    dirty = true;
  }
  {
    Lock l;
    if (curId.isEmpty() || shelfIndex >= (int)shelfOffsets.size()) {      // first shelf arrived, or it shrank
      if (!shelfOffsets.empty() && loadAlbum(shelfIndex = 0)) dirty = true;
    }
  }

  // the knob
  noInterrupts();
  int32_t enc = encCount;
  interrupts();
  encRest += enc - encLast;
  encLast = enc;
  int steps = encRest / ENC_COUNTS_PER_STEP;
  encRest -= steps * ENC_COUNTS_PER_STEP;
  if (steps) turned(ENC_REVERSE ? -steps : steps);

  static bool wasDown = false, holdSent = false;
  static uint32_t downAt = 0, changedAt = 0;
  bool down = knobDown();
  if (down != wasDown && now - changedAt > 30) {
    changedAt = now;
    wasDown = down;
    if (down) { downAt = now; holdSent = false; }
    else if (!holdSent) pressed(s);
  }
  if (wasDown && !holdSent && now - downAt > 600) { holdSent = true; held(s); }

  if (view == SHELF && s.mode == "playing" && now - lastInput > 20000) { view = DECK; dirty = true; }   // drift back to the record

  // which cover do we want on screen?
  {
    String want = view == DECK ? (pendingAlbum.length() ? pendingAlbum : s.album) : (now - lastTurn > 180 ? curId : String());
    Lock l;
    if (want.length() && art.want != want) art.want = want;
    if (art.fresh && art.buf) {
      if (jpeg.openRAM(art.buf, art.len, jpgRow)) {
        jpeg.setPixelType(RGB565_LITTLE_ENDIAN);
        coverId = jpeg.decode(0, 0, 0) ? art.have : String();
        jpeg.close();
      }
      art.fresh = false;
      dirty = true;
    }
  }

  // dim when nothing is happening
  static bool wasAsleep = false;
  bool asleep = s.mode != "playing" && now - lastInput > 120000;
  if (asleep != wasAsleep) { ledcWrite(BACKLIGHT_PIN, asleep ? 15 : 200); wasAsleep = asleep; }

  // things that change the picture over time
  static bool hadVol = false, hadBanner = false, hadToast = false;
  bool hasVol = now - volShown < 1500, hasBanner = now - startShown < 3500, hasToast = now < toastUntil;
  if (hasVol != hadVol || hasBanner != hadBanner || hasToast != hadToast) dirty = true;
  hadVol = hasVol; hadBanner = hasBanner; hadToast = hasToast;
  if (!hasToast && s.err.isEmpty()) toast = "";
  if (view == SHELF && curId.isEmpty() && now - lastDraw > 1000) dirty = true;
  if (view == DECK) {
    float pos = s.pos + (s.mode == "playing" ? (long)(now - s.at) : 0);
    float progress = s.dur > 0 ? min(1.0f, pos / s.dur) : 0;
    float target = s.mode == "playing" ? ARM_OUTER + (ARM_INNER - ARM_OUTER) * progress : ARM_REST;
    if (fabsf(target - arm) > 0.15f) { arm += (target - arm) * min(1.0f, dt * 4); dirty = true; }
  }

  if (dirty && now - lastDraw >= 33) {
    if (view == SHELF) drawShelf(s); else drawDeck(s);
    if (hasToast) { darken(196, 284, 40); text(toast, 248, &FreeSans12pt7b, COL_CREAM); }
    cv->flush();
    lastDraw = now;
    dirty = false;
  }
  delay(2);
}
