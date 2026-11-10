// Record player puck for the M5Stack Dial.
//
// The puck holds no Spotify logic. It asks the turntable server on the Pi for
// the shelf, cover art and deck state, and tells it what the knob did.
// server/sim.html is the same interface in a browser: keep the two in step.
//
//   Shelf: turn = browse albums, press = play, long press = back to the record
//   Deck:  turn = volume, press = lift / drop the needle or flip the record,
//          long press = back to the shelf (the music keeps playing)

#include <M5Dial.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include <ArduinoJson.h>
#include <vector>
#include "config.h"

// ---- layout (matches sim.html) ---------------------------------------------
static const int C = 120;                 // centre of the 240x240 screen
static const int LABEL_R = 84;            // the album art is the record's label
static const int LABEL_PX = LABEL_R * 2;
static const float ARM_PX = 200, ARM_PY = 40, ARM_LEN = 100;
static const float ARM_REST = 72, ARM_OUTER = 63, ARM_INNER = 49.5f;
static const size_t MAX_JPG = 48 * 1024;

static const uint32_t COL_CREAM = 0xF2EAD8u, COL_SOFT = 0xD8CFC0u, COL_DIM = 0xA89E8Eu;
static const uint32_t COL_SLEEVE = 0x16120Fu, COL_VINYL = 0x0C0C0Cu;

M5Canvas canvas(&M5Dial.Display);         // everything is drawn here, then pushed
M5Canvas label(&canvas);                  // the cover, kept so it can spin

// ---- state shared with the network task ------------------------------------
struct DeckState {
  String mode = "idle", album, title, side, err, version;
  int vol = 40;
  long pos = 0, dur = 0;
  uint32_t at = 0;                        // millis() when pos was read
  bool auth = true, reachable = false;
};
struct ArtSlot {                          // 0 = shelf cover (240), 1 = label (168)
  String want, have;
  uint8_t *buf = nullptr;
  size_t len = 0;
  bool fresh = false;
};

SemaphoreHandle_t mux;
DeckState st;
ArtSlot art[2];
String cmdPath;                           // one waiting command (play / press)
int volWanted = -1;
String serverBase;

std::vector<uint32_t> shelfOffsets;       // where each album's line starts in /shelf.tsv
String shelfVersion;

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
  st.title = doc["title"] | "";
  st.side = doc["side"] | "";
  st.err = doc["err"] | "";
  st.version = doc["v"] | "";
  st.vol = doc["vol"] | 40;
  st.pos = doc["pos"] | 0;
  st.dur = doc["dur"] | 0;
  st.auth = doc["auth"] | true;
  st.at = millis();
  st.reachable = true;
}

static bool request(const char *method, const String &path, String &out) {
  HTTPClient http;
  http.setConnectTimeout(3000);
  http.setTimeout(8000);
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
  HTTPClient http;
  http.setTimeout(15000);
  if (!http.begin(serverBase + "/api/shelf")) return;
  if (http.GET() != 200) { http.end(); return; }
  File f = LittleFS.open("/shelf.new", "w");
  if (!f) { http.end(); return; }
  http.writeToStream(&f);
  f.close();
  http.end();

  std::vector<uint32_t> offsets;
  f = LittleFS.open("/shelf.new", "r");
  String header = f.readStringUntil('\n');
  while (f.available()) {
    uint32_t at = f.position();
    String line = f.readStringUntil('\n');
    if (line.length() > 22) offsets.push_back(at);
  }
  f.close();
  if (!header.startsWith(version)) return;      // changed again while downloading
  Lock l;
  LittleFS.remove("/shelf.tsv");
  LittleFS.rename("/shelf.new", "/shelf.tsv");
  shelfOffsets.swap(offsets);
  shelfVersion = version;
}

static void fetchArt(int slot) {
  String want;
  { Lock l; want = art[slot].want; }
  HTTPClient http;
  http.setTimeout(8000);
  uint8_t *buf = nullptr;
  size_t len = 0;
  if (http.begin(serverBase + "/art/" + want + "?s=" + String(slot == 0 ? 240 : LABEL_PX)) && http.GET() == 200) {
    int size = http.getSize();
    if (size > 0 && (size_t)size <= MAX_JPG && (buf = (uint8_t *)malloc(size))) {
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
  free(art[slot].buf);
  art[slot].buf = buf;
  art[slot].len = buf ? len : 0;
  art[slot].have = want;                   // even on failure, so we do not hammer the server
  art[slot].fresh = buf != nullptr;
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
    int vol = -1, slot = -1;
    {
      Lock l;
      cmd = cmdPath; cmdPath = "";
      if (volWanted >= 0 && millis() - lastVol > 200) { vol = volWanted; volWanted = -1; }
      for (int i = 0; i < 2; i++)
        if (art[i].want.length() && art[i].want != art[i].have) slot = i;
      version = st.version; have = shelfVersion;
    }
    if (cmd.length()) {
      if (request("POST", cmd, body)) parseState(body);
    } else if (vol >= 0) {
      lastVol = millis();
      if (request("POST", "/api/volume?v=" + String(vol), body)) parseState(body);
    } else if (version.length() && version != have) {
      fetchShelf(version);
    } else if (slot >= 0) {
      fetchArt(slot);
    } else if (millis() - lastPoll > 1000) {
      lastPoll = millis();
      if (request("GET", "/api/state", body)) parseState(body);
      else { Lock l; st.reachable = false; serverBase = ""; }
    } else {
      delay(20);
    }
  }
}

// ---- interface --------------------------------------------------------------
enum View { SHELF, DECK };
View view = SHELF;
int shelfIndex = 0;
String curId, curArtist, curTitle, curYear;   // album under the knob
String coverShown, labelShown;
bool dirty = true;
uint32_t lastTurn = 0, lastInput = 0, lastFrame = 0;
uint32_t volShown = 0, sideShown = 0, loadingUntil = 0, toastUntil = 0;
int localVol = 40;
String shownSide, lastMode, lastAlbum, toast;
float angle = 0, spin = 0, arm = ARM_REST;
long encLast = 0, encRest = 0;

static bool loadAlbum(int i) {             // caller holds the lock
  if (i < 0 || i >= (int)shelfOffsets.size()) return false;
  File f = LittleFS.open("/shelf.tsv", "r");
  if (!f) return false;
  f.seek(shelfOffsets[i]);
  String line = f.readStringUntil('\n');
  f.close();
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
  File f = LittleFS.open("/shelf.tsv", "r");
  if (!f) return -1;
  int found = -1;
  for (size_t i = 0; i < shelfOffsets.size() && found < 0; i++) {
    f.seek(shelfOffsets[i]);
    if (f.readStringUntil('\t') == id) found = i;
  }
  f.close();
  return found;
}

static int chordWidth(int y) {             // usable text width at height y on a round screen
  float d = 118.0f * 118.0f - (float)(y - C) * (y - C);
  return d > 0 ? (int)(2 * sqrtf(d)) - 16 : 0;
}

static void text(String s, int y, const lgfx::IFont *font, uint32_t color) {
  canvas.setFont(font);
  canvas.setTextSize(1);
  canvas.setTextDatum(middle_center);
  canvas.setTextColor(color);
  int w = chordWidth(y);
  if (canvas.textWidth(s) > w) {
    while (s.length() > 1 && canvas.textWidth(s + "...") > w) s.remove(s.length() - 1);
    s.trim();
    s += "...";
  }
  canvas.drawString(s, C, y);
}

static void banner(const String &a, const String &b) {
  canvas.fillRectAlpha(0, 142, 240, 54, 180, TFT_BLACK);
  text(a, 160, &fonts::FreeSansBold9pt7b, COL_CREAM);
  if (b.length()) text(b, 181, &fonts::Font2, COL_SOFT);
}

static void showToast(const String &t) { toast = t; toastUntil = millis() + 4000; dirty = true; }

static void drawShelf(const DeckState &s) {
  canvas.fillScreen(COL_SLEEVE);
  if (curId.isEmpty()) {
    text(!s.reachable ? "Looking for the turntable" : !s.auth ? "Not connected to Spotify" : "Reading your shelf...", 112, &fonts::Font2, COL_CREAM);
    return;
  }
  bool haveCover = false;
  {
    Lock l;
    if (art[0].have == curId && art[0].buf && art[0].want == curId) {
      haveCover = canvas.drawJpg(art[0].buf, art[0].len, 0, 0);
      art[0].fresh = false;
    }
  }
  if (haveCover) {
    for (int y = 138; y < 240; y++)        // darken the bottom so the words read
      canvas.fillRectAlpha(0, y, 240, 1, (uint8_t)min(225, (y - 138) * 5), TFT_BLACK);
    coverShown = curId;
  } else {
    String key = curArtist;
    String low = key; low.toLowerCase();
    if (low.startsWith("the ")) key = key.substring(4);
    else if (low.startsWith("an ")) key = key.substring(3);
    else if (low.startsWith("a ")) key = key.substring(2);
    char ch = key.length() ? toupper(key[0]) : '#';
    if (ch < 'A' || ch > 'Z') ch = '#';
    canvas.setFont(&fonts::FreeSerifBold24pt7b);
    canvas.setTextSize(2.4f);
    canvas.setTextDatum(middle_center);
    canvas.setTextColor(0x4A3F37u);
    canvas.drawString(String(ch), C, 92);
    coverShown = "";
  }
  text(curArtist, 180, &fonts::FreeSansBold9pt7b, COL_CREAM);
  text(curTitle, 199, &fonts::Font2, COL_SOFT);
  text(curYear, 216, &fonts::Font0, COL_DIM);
  int n = max(1, (int)shelfOffsets.size());
  float at = -90.0f + 360.0f * shelfIndex / n;                 // where you are along the shelf
  canvas.fillArc(C, C, 114, 120, at - 2.5f, at + 2.5f, COL_CREAM);
  if (s.mode == "playing") {                                    // a record is still playing
    canvas.fillCircle(C, 24, 11, TFT_BLACK);
    canvas.fillCircle(C, 24, 7, COL_CREAM);
    canvas.fillCircle(C, 24, 2, TFT_BLACK);
  }
}

static void drawDeck(const DeckState &s, float dt) {
  uint32_t now = millis();
  bool playing = s.mode == "playing";
  spin += ((playing ? 200.0f : 0.0f) - spin) * min(1.0f, dt * 2.5f);   // 33 1/3 rpm = 200 degrees a second
  angle = fmodf(angle + spin * dt, 360.0f);
  float pos = s.pos + (playing ? (long)(now - s.at) : 0);
  float progress = s.dur > 0 ? min(1.0f, pos / s.dur) : 0;
  float target = playing ? ARM_OUTER + (ARM_INNER - ARM_OUTER) * progress : ARM_REST;
  arm += (target - arm) * min(1.0f, dt * 4);

  canvas.fillScreen(COL_VINYL);
  if (labelShown.length()) label.pushRotateZoom(&canvas, C, C, angle, 1.0f, 1.0f);
  else canvas.fillCircle(C, C, LABEL_R, 0x3A302Au);
  canvas.fillArc(C, C, LABEL_R, 121, 0, 360, COL_VINYL);        // trims the square cover to a round label
  for (int r = 88; r <= 118; r += 3) canvas.drawCircle(C, C, r, (r & 1) ? 0x1C1C1Cu : 0x242424u);
  canvas.fillCircle(C, C, 9, COL_VINYL);
  canvas.fillCircle(C, C, 3, 0xC9C5BDu);

  float dir = (135.0f - arm) * DEG_TO_RAD;                      // tonearm
  int sx = ARM_PX + ARM_LEN * cosf(dir), sy = ARM_PY + ARM_LEN * sinf(dir);
  canvas.drawWideLine(ARM_PX, ARM_PY, sx, sy, 2.5f, 0xC9C5BDu);
  canvas.drawWideLine(sx - 9 * cosf(dir), sy - 9 * sinf(dir), sx, sy, 4.5f, 0xEFEAE0u);
  canvas.fillSmoothCircle(ARM_PX, ARM_PY, 13, 0x8F8B84u);
  canvas.fillSmoothCircle(ARM_PX, ARM_PY, 5, 0x2A2A2Au);

  if (s.mode == "flip") banner("Flip to side " + String((char)(s.side[0] + 1)), "press to turn it over");
  else if (s.mode == "end") banner("Side " + s.side + " finished", "press to play it again");
  else if (now < loadingUntil && !playing) banner("Putting it on...", "");
  else if (playing && now - sideShown < 3500) banner("Side " + s.side, s.title);
  if (now - volShown < 1500) {
    canvas.fillArc(C, C, 111, 118, 135, 405, TFT_BLACK);
    if (localVol > 0) canvas.fillArc(C, C, 112, 117, 135, 135 + 270.0f * localVol / 100, COL_CREAM);
  }
}

static void turned(int steps, const DeckState &s) {
  lastInput = millis();
  if (view == SHELF) {
    Lock l;
    int n = shelfOffsets.size();
    if (!n) return;
    shelfIndex = ((shelfIndex + steps) % n + n) % n;
    loadAlbum(shelfIndex);
    lastTurn = millis();
    dirty = true;
  } else {
    localVol = constrain(localVol + steps * 3, 0, 100);
    volShown = millis();
    Lock l;
    volWanted = localVol;
  }
}

static void pressed(const DeckState &s) {
  lastInput = millis();
  if (view == SHELF) {
    if (curId.isEmpty()) return;
    view = DECK;
    if (!(s.album == curId && s.mode != "idle")) {
      loadingUntil = millis() + 8000;
      Lock l;
      cmdPath = "/api/play?album=" + curId;
      art[1].want = curId;
    }
  } else if (millis() > loadingUntil || s.mode != "idle") {
    Lock l;
    cmdPath = "/api/press";
  }
}

static void held(const DeckState &s) {
  lastInput = millis();
  if (view == DECK) {
    view = SHELF;
    Lock l;
    int i = findAlbum(s.album);
    if (i >= 0) { shelfIndex = i; loadAlbum(i); }
    lastTurn = 0;
    dirty = true;
  } else if (s.album.length() && s.mode != "idle") {
    view = DECK;
  }
}

void setup() {
  auto cfg = M5.config();
  M5Dial.begin(cfg, true, false);          // encoder on, RFID off
  M5Dial.BtnA.setHoldThresh(600);
  M5Dial.Display.setBrightness(160);
  M5Dial.Display.fillScreen(TFT_BLACK);
  mux = xSemaphoreCreateMutex();

  // Grab the two big buffers before Wi-Fi fragments the heap (this board has no PSRAM).
  canvas.setColorDepth(16);
  label.setColorDepth(16);
  if (!canvas.createSprite(240, 240) || !label.createSprite(LABEL_PX, LABEL_PX)) {
    M5Dial.Display.setTextDatum(middle_center);
    M5Dial.Display.drawString("Out of memory", C, C, &fonts::Font2);
    for (;;) delay(1000);
  }
  label.setPivot(LABEL_R, LABEL_R);

  LittleFS.begin(true);
  LittleFS.remove("/shelf.tsv");           // always start from the server's current shelf
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  xTaskCreatePinnedToCore(netTask, "net", 8192, nullptr, 1, nullptr, 0);
  encLast = M5Dial.Encoder.read();
  lastFrame = lastInput = millis();
}

void loop() {
  M5Dial.update();
  uint32_t now = millis();
  float dt = min(0.1f, (now - lastFrame) / 1000.0f);
  lastFrame = now;

  DeckState s;
  { Lock l; s = st; }

  // things the server told us
  if (s.err.length() && s.err != toast) showToast(s.err);
  if (s.err.isEmpty() && now > toastUntil) toast = "";
  if (now - volShown > 2000) localVol = s.vol;
  if (s.mode != lastMode || s.side != shownSide) {
    if (s.mode == "playing" && (s.side != shownSide || lastMode == "flip" || lastMode == "end" || lastMode == "idle")) sideShown = now;
    if (s.mode == "idle" && view == DECK && now > loadingUntil) view = SHELF;
    if (s.mode != "idle") loadingUntil = 0;
    lastMode = s.mode;
    shownSide = s.side;
    dirty = true;
  }
  if (s.album != lastAlbum) {              // a different record went on
    lastAlbum = s.album;
    if (s.mode == "playing") sideShown = now;
  }
  {
    Lock l;
    if (loadingUntil && s.album == art[1].want && s.mode != "idle") loadingUntil = 0;
    if (s.album.length() && now > loadingUntil && art[1].want != s.album) art[1].want = s.album;
    if (art[1].fresh && art[1].have == art[1].want) {
      label.fillScreen(0x3A302Au);
      label.drawJpg(art[1].buf, art[1].len, 0, 0);
      art[1].fresh = false;
      labelShown = art[1].have;
    }
    if (labelShown != art[1].want) labelShown = "";
  }
  if (curId.isEmpty()) {                   // first shelf arrived
    Lock l;
    if (!shelfOffsets.empty() && loadAlbum(shelfIndex = 0)) dirty = true;
  } else {
    Lock l;
    if (shelfIndex >= (int)shelfOffsets.size()) { shelfIndex = 0; loadAlbum(0); dirty = true; }
  }

  // the knob
  long enc = M5Dial.Encoder.read();
  encRest += enc - encLast;
  encLast = enc;
  int steps = encRest / ENC_COUNTS_PER_STEP;
  encRest -= steps * ENC_COUNTS_PER_STEP;
  if (steps) turned(ENC_REVERSE ? -steps : steps, s);
  if (M5Dial.BtnA.wasClicked()) pressed(s);
  if (M5Dial.BtnA.wasHold()) held(s);

  if (view == SHELF && s.mode == "playing" && now - lastInput > 20000) view = DECK;   // drift back to the record

  // dim when nothing is happening
  bool asleep = s.mode != "playing" && now - lastInput > 120000;
  static bool wasAsleep = false;
  if (asleep != wasAsleep) { M5Dial.Display.setBrightness(asleep ? 12 : 160); wasAsleep = asleep; }

  static View lastView = SHELF;
  if (view != lastView) { lastView = view; dirty = true; }

  if (view == SHELF) {
    // ask for the cover once the knob has rested on an album for a moment
    if (curId.length() && now - lastTurn > 180) {
      Lock l;
      if (art[0].want != curId) art[0].want = curId;
      if (art[0].fresh && art[0].have == curId) dirty = true;
    }
    if (!dirty && curId.isEmpty()) { static uint32_t t; if (now - t > 1000) { t = now; dirty = true; } }
    if (!dirty && now >= toastUntil && toast.length() && s.err.isEmpty()) { toast = ""; dirty = true; }
    if (dirty) {
      drawShelf(s);
      if (now < toastUntil) { canvas.fillRectAlpha(0, 96, 240, 48, 215, TFT_BLACK); text(toast, 120, &fonts::Font2, COL_CREAM); }
      canvas.pushSprite(0, 0);
      dirty = false;
    }
    delay(5);
  } else {
    drawDeck(s, dt);
    if (now < toastUntil) { canvas.fillRectAlpha(0, 96, 240, 48, 215, TFT_BLACK); text(toast, 120, &fonts::Font2, COL_CREAM); }
    canvas.pushSprite(0, 0);
    dirty = false;
  }
}
