#!/usr/bin/env python3
"""Turntable: the brain of the record player.

Runs on the Raspberry Pi that is wired to the speaker. It
  - reads your Spotify liked songs and turns them into a shelf of albums,
  - plays an album one side at a time on a Spotify Connect device
    (normally librespot/raspotify on this same Pi),
  - serves the shelf, resized cover art and the deck state to the knob puck
    (and to the browser simulator at /sim) over plain HTTP on your LAN.

Only the standard library and Pillow are needed:
    python3 turntable.py            # real Spotify, needs config.json
    python3 turntable.py --demo     # fake library, no account needed
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
import random
import re
import secrets
import shutil
import struct
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SCOPES = "user-library-read user-read-playback-state user-modify-playback-state"
DEFAULTS = {
    "client_id": "",
    "redirect_uri": "http://127.0.0.1:8888/callback",
    "port": 8888,
    "device_name": "Record Player",
    "include_singles": False,   # singles with fewer than 4 tracks stay off the shelf
    "min_liked_songs": 1,       # albums need at least this many liked songs
    "needle_sound": True,       # crackle through the Pi's speaker before each side
    "side_minutes": 23,         # longest a side should run before the album gets more sides
    "refresh_hours": 6,
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------------------
# Pure helpers (unit tested in test_turntable.py)
# ----------------------------------------------------------------------------

def fold(text: str) -> str:
    """Plain-ASCII version of a name for the puck's small built-in font."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip()
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in text)


def sort_key(album: dict):
    artist = fold(album["artist"]).casefold()
    artist = re.sub(r"^(the|a|an)\s+", "", artist)
    # names that do not start with a letter or digit go to the end of the shelf
    return (not artist[:1].isalnum(), artist, album.get("year") or 0, fold(album["title"]).casefold())


def build_shelf(saved_tracks, include_singles=False, min_liked=1) -> list[dict]:
    """Group liked tracks into albums, sorted like a record shelf (artist, then year)."""
    albums: dict[str, dict] = {}
    for t in saved_tracks:
        al = (t or {}).get("album") or {}
        if not al.get("id"):
            continue  # local files and removed tracks have no album id
        a = albums.get(al["id"])
        if a is None:
            artists = al.get("artists") or t.get("artists") or [{}]
            year = (al.get("release_date") or "")[:4]
            a = albums[al["id"]] = {
                "id": al["id"],
                "title": al.get("name") or "Untitled",
                "artist": artists[0].get("name") or "Unknown",
                "year": int(year) if year.isdigit() else 0,
                "type": al.get("album_type") or "album",
                "total": al.get("total_tracks") or 0,
                "image": pick_image(al.get("images") or []),
                "liked": 0,
            }
        a["liked"] += 1
    out = []
    for a in albums.values():
        if a["liked"] < min_liked:
            continue
        if a["type"] == "single" and a["total"] < 4 and not include_singles:
            continue
        out.append(a)
    out.sort(key=sort_key)
    return out


def pick_image(images: list[dict]) -> str:
    """Smallest cover that is still at least 300px wide (we only ever show 240)."""
    best = None
    for im in images:
        w = im.get("width") or 640
        if best is None or (w >= 300 and (best[0] < 300 or w < best[0])) or (best[0] < 300 and w > best[0]):
            best = (w, im.get("url"))
    return best[1] if best else ""


def split_sides(tracks: list[dict], side_minutes: float = 23) -> list[list[dict]]:
    """Split an album into record sides.

    Each disc is split separately. A short disc (under 4 tracks or 12 minutes)
    is one side. Otherwise it gets at least two sides, more if a side would run
    past `side_minutes`, cut at the track boundaries nearest to equal lengths.
    """
    discs: dict[int, list[dict]] = {}
    for t in tracks:
        discs.setdefault(t.get("disc") or 1, []).append(t)
    sides: list[list[dict]] = []
    for disc in sorted(discs):
        ts = sorted(discs[disc], key=lambda t: t.get("no") or 0)
        total = sum(t["ms"] for t in ts)
        if len(ts) < 4 or total < 12 * 60_000:
            sides.append(ts)
            continue
        n = max(2, math.ceil(total / (side_minutes * 60_000)))
        n = min(n, len(ts))
        ends, acc = [], 0
        for t in ts:
            acc += t["ms"]
            ends.append(acc)
        cuts, lo = [], 0
        for k in range(1, n):
            target = total * k / n
            # leave at least one track for every remaining side
            hi = len(ts) - (n - k)
            idx = min(range(lo, hi), key=lambda i: abs(ends[i] - target))
            cuts.append(idx + 1)
            lo = idx + 1
        prev = 0
        for c in cuts + [len(ts)]:
            sides.append(ts[prev:c])
            prev = c
    return [s for s in sides if s]


def side_letter(i: int) -> str:
    return chr(ord("A") + i) if i < 26 else str(i + 1)


def make_needle_wav(path: Path, seconds: float = 1.7, rate: int = 22050):
    """Synthesise a needle drop: a soft thump, then surface crackle fading out."""
    rnd = random.Random(33)
    n = int(seconds * rate)
    out = [0.0] * n
    for i in range(int(0.12 * rate)):                      # thump
        out[i] += 0.55 * math.sin(2 * math.pi * 70 * i / rate) * math.exp(-i / (0.03 * rate))
    hiss = 0.0
    for i in range(n):                                     # hiss and pops
        hiss = 0.92 * hiss + 0.08 * rnd.uniform(-1, 1)
        out[i] += 0.10 * hiss
        if rnd.random() < 0.0016:
            amp = rnd.uniform(0.15, 0.6)
            for j in range(min(40, n - i)):
                out[i + j] += amp * math.exp(-j / 6.0) * (1 if j % 2 == 0 else -0.6)
    fade = int(0.5 * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i, v in enumerate(out):
            if i > n - fade:
                v *= (n - i) / fade
            frames += struct.pack("<h", int(max(-1, min(1, v)) * 30000))
        w.writeframes(bytes(frames))


# ----------------------------------------------------------------------------
# Spotify
# ----------------------------------------------------------------------------

class SpotifyError(Exception):
    def __init__(self, status, message):
        super().__init__(f"Spotify {status}: {message}")
        self.status = status
        self.message = message


class Spotify:
    """The handful of Web API calls the turntable needs, with PKCE sign-in."""

    def __init__(self, cfg: dict, data: Path):
        self.cfg = cfg
        self.token_file = data / "tokens.json"
        self.tokens = {}
        self.verifier = ""
        self.lock = threading.Lock()
        if self.token_file.exists():
            self.tokens = json.loads(self.token_file.read_text())

    # -- sign-in --------------------------------------------------------------
    @property
    def signed_in(self) -> bool:
        return bool(self.tokens.get("refresh_token"))

    def auth_url(self) -> str:
        self.verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
            "client_id": self.cfg["client_id"], "response_type": "code",
            "redirect_uri": self.cfg["redirect_uri"], "scope": SCOPES,
            "code_challenge_method": "S256", "code_challenge": challenge})

    def finish_auth(self, code: str):
        self._token({"grant_type": "authorization_code", "code": code,
                     "redirect_uri": self.cfg["redirect_uri"], "code_verifier": self.verifier})

    def _token(self, form: dict):
        form["client_id"] = self.cfg["client_id"]
        req = urllib.request.Request("https://accounts.spotify.com/api/token",
                                     data=urllib.parse.urlencode(form).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                tok = json.load(r)
        except urllib.error.HTTPError as e:
            raise SpotifyError(e.code, e.read().decode(errors="replace")[:300])
        self.tokens["access_token"] = tok["access_token"]
        self.tokens["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
        if tok.get("refresh_token"):            # PKCE refresh tokens rotate
            self.tokens["refresh_token"] = tok["refresh_token"]
        self.token_file.write_text(json.dumps(self.tokens))
        os.chmod(self.token_file, 0o600)

    def _access(self) -> str:
        with self.lock:
            if not self.signed_in:
                raise SpotifyError(401, "not signed in")
            if time.time() >= self.tokens.get("expires_at", 0):
                self._token({"grant_type": "refresh_token", "refresh_token": self.tokens["refresh_token"]})
            return self.tokens["access_token"]

    # -- raw call -------------------------------------------------------------
    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None, tries: int = 3):
        url = "https://api.spotify.com/v1" + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        for attempt in range(tries):
            data = json.dumps(body).encode() if body is not None else (b"" if method != "GET" else None)
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": "Bearer " + self._access(), "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    raw = r.read()
                    try:
                        return json.loads(raw) if raw else None
                    except ValueError:
                        return None  # some player endpoints answer with a bare token
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                if e.code == 429 and attempt < tries - 1:
                    time.sleep(min(int(e.headers.get("Retry-After") or 2), 30))
                    continue
                if e.code == 401 and attempt < tries - 1:
                    self.tokens["expires_at"] = 0
                    continue
                if e.code >= 500 and attempt < tries - 1:
                    time.sleep(1 + attempt)
                    continue
                try:
                    text = json.loads(text)["error"]["message"]
                except Exception:
                    text = text[:200]
                raise SpotifyError(e.code, text)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < tries - 1:
                    time.sleep(1 + attempt)
                    continue
                raise SpotifyError(0, f"network: {e}")

    # -- what the turntable uses ---------------------------------------------
    def saved_tracks(self):
        offset = 0
        while True:
            page = self.call("GET", "/me/tracks", {"limit": 50, "offset": offset}) or {}
            items = page.get("items") or []
            for it in items:
                yield it.get("track")
            offset += len(items)
            if not items or not page.get("next"):
                return

    def album_tracks(self, album_id: str) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self.call("GET", f"/albums/{album_id}/tracks", {"limit": 50, "offset": offset}) or {}
            items = page.get("items") or []
            for t in items:
                out.append({"uri": t["uri"], "name": t.get("name") or "", "ms": t.get("duration_ms") or 0,
                            "disc": t.get("disc_number") or 1, "no": t.get("track_number") or 0})
            offset += len(items)
            if not items or not page.get("next"):
                return out

    def devices(self) -> list[dict]:
        return (self.call("GET", "/me/player/devices") or {}).get("devices") or []

    def play(self, device_id: str, uris: list[str], index: int = 0, position_ms: int = 0):
        self.call("PUT", "/me/player/play", {"device_id": device_id},
                  {"uris": uris, "offset": {"position": index}, "position_ms": int(position_ms)})
        for path, params in (("/me/player/shuffle", {"state": "false"}), ("/me/player/repeat", {"state": "off"})):
            try:
                self.call("PUT", path, {**params, "device_id": device_id}, tries=1)
            except SpotifyError:
                pass

    def pause(self):
        self.call("PUT", "/me/player/pause")

    def volume(self, percent: int, device_id: str | None):
        self.call("PUT", "/me/player/volume", {"volume_percent": percent, "device_id": device_id})

    def player(self) -> dict | None:
        st = self.call("GET", "/me/player", tries=2)
        if not st:
            return None
        item = st.get("item") or {}
        dev = st.get("device") or {}
        return {"playing": bool(st.get("is_playing")), "uri": item.get("uri"),
                "progress": st.get("progress_ms") or 0, "volume": dev.get("volume_percent"),
                "device": dev.get("name")}

    def cover(self, album: dict) -> bytes:
        with urllib.request.urlopen(album["image"], timeout=15) as r:
            return r.read()


class DemoSpotify:
    """A made-up library and a pretend speaker, for trying the interface."""

    signed_in = True

    def __init__(self, speed: float = 1.0):
        self.speed = speed
        rnd = random.Random(7)
        artists = ["Alder & Ash", "The Birch Tapes", "Calico Radio", "Dune Parade", "Elm Street Choir",
                   "The Fathoms", "Glass Orchard", "Harbour Lights", "Ines Moreau", "Juniper Vale",
                   "Kite Season", "Low Tide Social", "Marigold Hour", "Night Ferry", "Otis Wren",
                   "Paper Lanterns", "Quiet Mile", "The Rooftops", "Salt & Cedar", "Tallow",
                   "Umber", "Velvet Tram", "Wren Hollow", "Yarrow", "Zephyr Arcade", "808 Orchard"]
        words = ["Blue", "Hours", "Northern", "Rooms", "Static", "Summer", "Letters", "Gold", "Weather",
                 "Slow", "Motel", "Rivers", "Small", "Engines", "Late", "Satellites", "Honey", "Signals"]
        self.albums, self.tracks = [], {}
        for artist in artists:
            for _ in range(rnd.randint(1, 4)):
                aid = "demo%018d" % len(self.albums)
                n = rnd.choice([3, 8, 9, 10, 11, 12, 14, 22])
                self.albums.append({"id": aid, "name": " ".join(rnd.sample(words, rnd.randint(1, 3))),
                                    "artists": [{"name": artist}], "release_date": str(rnd.randint(1967, 2026)),
                                    "album_type": "album", "total_tracks": n, "images": []})
                self.tracks[aid] = [{"uri": f"spotify:track:{aid}{i:02d}", "name": f"Track {i + 1}",
                                     "ms": rnd.randint(150, 330) * 1000, "disc": 1, "no": i + 1} for i in range(n)]
        self.queue, self.idx, self.pos, self.at, self.playing, self.vol = [], 0, 0.0, time.monotonic(), False, 40

    def saved_tracks(self):
        for al in self.albums:
            yield {"album": al, "artists": al["artists"]}

    def album_tracks(self, album_id):
        return self.tracks[album_id]

    def devices(self):
        return [{"id": "demo-device", "name": "Record Player", "is_active": True}]

    def _advance(self):
        now = time.monotonic()
        if self.playing:
            self.pos += (now - self.at) * 1000 * self.speed
            while self.queue and self.pos >= self.queue[self.idx]["ms"]:
                self.pos -= self.queue[self.idx]["ms"]
                self.idx += 1
                if self.idx >= len(self.queue):      # list finished: back to the top, stopped
                    self.idx, self.pos, self.playing = 0, 0.0, False
                    break
        self.at = now

    def play(self, device_id, uris, index=0, position_ms=0):
        by_uri = {t["uri"]: t for ts in self.tracks.values() for t in ts}
        self.queue, self.idx, self.pos, self.at, self.playing = [by_uri[u] for u in uris], index, position_ms, time.monotonic(), True

    def pause(self):
        self._advance()
        self.playing = False

    def volume(self, percent, device_id):
        self.vol = percent

    def player(self):
        self._advance()
        if not self.queue:
            return None
        return {"playing": self.playing, "uri": self.queue[self.idx]["uri"], "progress": int(self.pos),
                "volume": self.vol, "device": "Record Player"}

    def cover(self, album):
        rnd = random.Random(album["id"])
        hue = rnd.random()
        im = Image.new("RGB", (480, 480))
        d = ImageDraw.Draw(im)
        for y in range(480):
            f = y / 480
            d.line([(0, y), (480, y)], fill=_hsv(hue + 0.12 * f, 0.55, 0.85 - 0.45 * f))
        for _ in range(5):
            x, y, r = rnd.randint(0, 480), rnd.randint(0, 480), rnd.randint(40, 190)
            d.ellipse([x - r, y - r, x + r, y + r], outline=_hsv(hue + 0.5, 0.35, 0.95), width=rnd.randint(3, 16))
        d.rectangle([28, 28, 452, 452], outline=(245, 240, 225), width=3)
        buf = io.BytesIO()
        im.save(buf, "JPEG")
        return buf.getvalue()


def _hsv(h, s, v):
    import colorsys
    return tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h % 1, s, v))


# ----------------------------------------------------------------------------
# The shelf and the deck
# ----------------------------------------------------------------------------

class Shelf:
    def __init__(self, sp, cfg: dict, data: Path):
        self.sp, self.cfg = sp, cfg
        self.file = data / "shelf.json"
        self.art = data / "art"
        self.art.mkdir(parents=True, exist_ok=True)
        self.albums: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.version = "0"
        self.error = ""
        self.lock = threading.Lock()
        self.art_lock = threading.Lock()
        if self.file.exists():
            try:
                self._set(json.loads(self.file.read_text()))
            except ValueError:
                pass

    def _set(self, albums):
        with self.lock:
            self.albums = albums
            self.by_id = {a["id"]: a for a in albums}
            self.version = hashlib.sha1("".join(a["id"] for a in albums).encode()).hexdigest()[:8]

    def refresh(self):
        try:
            albums = build_shelf(self.sp.saved_tracks(), self.cfg["include_singles"], self.cfg["min_liked_songs"])
        except SpotifyError as e:
            self.error = str(e)
            log("shelf refresh failed:", e)
            return
        self.error = ""
        self._set(albums)
        self.file.write_text(json.dumps(albums))
        log(f"shelf: {len(albums)} albums (version {self.version})")

    def tsv(self) -> str:
        """One album per line for the puck: id, artist, title, year."""
        with self.lock:
            lines = [f"{self.version}\t{len(self.albums)}"]
            lines += [f"{a['id']}\t{fold(a['artist'])[:60]}\t{fold(a['title'])[:60]}\t{a['year'] or ''}" for a in self.albums]
        return "\n".join(lines) + "\n"

    def cover(self, album_id: str, size: int) -> bytes | None:
        album = self.by_id.get(album_id)
        if not album:
            return None
        size = max(32, min(640, size))
        path = self.art / f"{album_id}_{size}.jpg"
        if path.exists():
            return path.read_bytes()
        with self.art_lock:
            src = self.art / f"{album_id}.src"
            if not src.exists():
                if not album.get("image") and not isinstance(self.sp, DemoSpotify):
                    return None
                src.write_bytes(self.sp.cover(album))
            im = Image.open(src).convert("RGB").resize((size, size), Image.LANCZOS)
            buf = io.BytesIO()
            # baseline (not progressive) JPEG: the puck's decoder cannot read progressive files
            im.save(buf, "JPEG", quality=85, progressive=False, optimize=False)
            path.write_bytes(buf.getvalue())
            return buf.getvalue()


class Deck:
    """The record currently on the platter. One side plays, then it waits to be flipped."""

    def __init__(self, sp, shelf: Shelf, cfg: dict, data: Path):
        self.sp, self.shelf, self.cfg = sp, shelf, cfg
        self.lock = threading.RLock()
        self.mode = "idle"            # idle | playing | paused | flip | end
        self.album: dict | None = None
        self.sides: list[list[dict]] = []
        self.side = 0
        self.pos = 0.0                # ms into the current side
        self.pos_at = time.monotonic()
        self.volume = 40
        self.volume_set_at = 0.0
        self.grace_until = 0.0
        self.misses = 0
        self.error = ""
        self.error_at = 0.0
        self.device_id, self.device_at = None, 0.0
        self.track_cache: dict[str, list[dict]] = {}
        self.needle = data / "needle.wav"
        if not self.needle.exists():
            make_needle_wav(self.needle)

    # -- helpers --------------------------------------------------------------
    def _side_ms(self) -> int:
        return sum(t["ms"] for t in self.sides[self.side]) if self.sides else 0

    def _est(self) -> float:
        if self.mode == "playing":
            return min(self.pos + (time.monotonic() - self.pos_at) * 1000 * getattr(self.sp, "speed", 1.0), self._side_ms())
        return self.pos

    def _locate(self, pos: float):
        """Track index and offset inside it for a position within the side."""
        tracks = self.sides[self.side]
        for i, t in enumerate(tracks):
            if pos < t["ms"] or i == len(tracks) - 1:
                return i, max(0, min(pos, t["ms"] - 1000))
            pos -= t["ms"]

    def _fail(self, e: Exception):
        self.error, self.error_at = str(getattr(e, "message", e)), time.time()
        log("deck error:", e)

    def _device(self) -> str:
        if self.device_id and time.time() - self.device_at < 60:
            return self.device_id
        want = (self.cfg["device_name"] or "").casefold()
        devices = self.sp.devices()
        for d in devices:
            if d.get("name", "").casefold() == want:
                self.device_id, self.device_at = d["id"], time.time()
                return d["id"]
        names = ", ".join(d.get("name", "?") for d in devices) or "none"
        raise SpotifyError(404, f"Speaker '{self.cfg['device_name']}' not found (Spotify sees: {names})")

    def _needle_drop(self):
        if self.cfg["needle_sound"] and shutil.which("aplay"):
            try:
                subprocess.run(["aplay", "-q", str(self.needle)], timeout=4, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (subprocess.SubprocessError, OSError):
                pass

    def _start(self, side: int, pos: float = 0.0):
        self.side = side
        idx, offset = self._locate(pos)
        device = self._device()
        self._needle_drop()
        self.sp.play(device, [t["uri"] for t in self.sides[side]], idx, offset)
        self.mode, self.pos, self.pos_at = "playing", pos, time.monotonic()
        self.grace_until = time.monotonic() + 6     # Spotify's reported state lags a few seconds
        self.misses = 0

    # -- what the knob does ---------------------------------------------------
    def play(self, album_id: str):
        with self.lock:
            album = self.shelf.by_id.get(album_id)
            if not album:
                return self._fail(Exception("That album is not on the shelf"))
            try:
                if album_id not in self.track_cache:
                    self.track_cache[album_id] = self.sp.album_tracks(album_id)
                sides = split_sides(self.track_cache[album_id], self.cfg["side_minutes"])
                if not sides:
                    return self._fail(Exception("Album has no tracks"))
                self.album, self.sides = album, sides
                self._start(0)
            except SpotifyError as e:
                self._fail(e)

    def press(self):
        with self.lock:
            try:
                if self.mode == "playing":
                    self.pos = self._est()
                    self.mode = "paused"
                    self.sp.pause()
                elif self.mode == "paused":
                    self._start(self.side, self.pos)
                elif self.mode == "flip":
                    self._start(self.side + 1)
                elif self.mode == "end":
                    self._start(0)
            except SpotifyError as e:
                self._fail(e)

    def set_volume(self, percent: int):
        with self.lock:
            self.volume, self.volume_set_at = max(0, min(100, percent)), time.monotonic()
            try:
                self.sp.volume(self.volume, self.device_id)
            except SpotifyError as e:
                if e.status != 404:              # 404: nothing is playing yet
                    self._fail(e)

    # -- keeping in step with Spotify ----------------------------------------
    def poll(self):
        """One look at Spotify. Returns how long to wait before the next look."""
        with self.lock:
            if self.mode not in ("playing", "paused"):
                return 2.0
            mode_before, est = self.mode, self._est()
        try:
            st = self.sp.player()
        except SpotifyError as e:
            log("poll failed:", e)
            return 5.0
        with self.lock:
            if self.mode != mode_before or not self.sides:
                return 1.0
            tracks = self.sides[self.side]
            uris = [t["uri"] for t in tracks]
            side_ms = self._side_ms()
            if st and st.get("volume") is not None and time.monotonic() - self.volume_set_at > 3:
                self.volume = st["volume"]
            in_side = bool(st) and st["uri"] in uris
            if self.mode == "paused":
                if in_side and st["playing"]:      # resumed from the phone
                    self.mode = "playing"
                    self.pos = sum(t["ms"] for t in tracks[:uris.index(st["uri"])]) + st["progress"]
                    self.pos_at = time.monotonic()
                return 4.0
            if time.monotonic() < self.grace_until:
                return 1.0
            near_end = est >= side_ms - 6000
            if in_side and st["playing"]:
                idx = uris.index(st["uri"])
                self.pos = sum(t["ms"] for t in tracks[:idx]) + st["progress"]
                self.pos_at = time.monotonic()
                self.misses = 0
            elif in_side:
                idx = uris.index(st["uri"])
                last = len(tracks) - 1
                if near_end and (idx != last or st["progress"] < 2000 or st["progress"] > tracks[last]["ms"] - 2000):
                    self._side_over()
                else:                              # paused from the phone
                    self.pos = sum(t["ms"] for t in tracks[:idx]) + st["progress"]
                    self.mode = "paused"
            elif near_end:
                if st and st["playing"]:           # Spotify autoplay started something else
                    try:
                        self.sp.pause()
                    except SpotifyError:
                        pass
                self._side_over()
            else:
                self.misses += 1
                if self.misses >= 3:               # something else took over the speaker
                    log("playback moved elsewhere; taking the record off")
                    self.mode, self.album, self.sides = "idle", None, []
            remaining = side_ms - self._est()
            return 1.0 if remaining < 12000 else 3.0

    def _side_over(self):
        self.pos = self._side_ms()
        self.mode = "flip" if self.side + 1 < len(self.sides) else "end"
        log(f"side {side_letter(self.side)} finished ->", self.mode)

    def state(self) -> dict:
        with self.lock:
            s = {"v": self.shelf.version, "n": len(self.shelf.albums), "mode": self.mode,
                 "vol": self.volume, "auth": bool(self.sp.signed_in)}
            if self.album:
                idx, _ = self._locate(self._est())
                s.update({"album": self.album["id"], "artist": fold(self.album["artist"]),
                          "title": fold(self.album["title"]), "side": side_letter(self.side),
                          "sides": len(self.sides), "pos": int(self._est()), "dur": self._side_ms(),
                          "track": fold(self.sides[self.side][idx]["name"])})
            err = self.error if time.time() - self.error_at < 6 else self.shelf.error
            if err:
                s["err"] = fold(err)[:90]
            return s


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

SETUP_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Record player setup</title>
<style>body{{font:16px/1.5 system-ui;max-width:40rem;margin:2rem auto;padding:0 1rem;color:#222;background:#faf7f2}}
input[type=text]{{width:100%;padding:.5rem;font:inherit;box-sizing:border-box}}button,a.b{{display:inline-block;padding:.5rem 1rem;
background:#222;color:#fff;border:0;border-radius:.3rem;font:inherit;text-decoration:none;cursor:pointer}}.ok{{color:#176b2c}}.bad{{color:#a11}}
code{{background:#eee;padding:0 .2rem}}</style>
<h1>Record player setup</h1>
<p>Status: {status}</p>
{body}
<p><a href="/sim">Open the simulator</a></p>"""


def make_handler(sp, shelf: Shelf, deck: Deck, cfg: dict):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj):
            self.send(200, json.dumps(obj, separators=(",", ":")), "application/json", {"Cache-Control": "no-store"})

        def setup_page(self, note=""):
            if sp.signed_in:
                status = f"<b class=ok>connected to Spotify</b>, {len(shelf.albums)} albums on the shelf."
                body = "<form method=post action=/api/refresh><button>Re-read my liked songs</button></form>"
            elif not cfg["client_id"]:
                status = "<b class=bad>no client_id</b> in config.json yet. See the README."
                body = ""
            else:
                status = "<b class=bad>not connected to Spotify</b>"
                body = (f"<ol><li><a class=b href=\"{sp.auth_url()}\" target=_blank>Sign in to Spotify</a></li>"
                        "<li>After you agree, your browser lands on a page that will not load "
                        f"(<code>{cfg['redirect_uri']}?code=...</code>). That is expected. "
                        "Copy the whole address from the address bar and paste it here:</li></ol>"
                        "<form method=post action=/setup><input type=text name=url placeholder='http://127.0.0.1:8888/callback?code=...'>"
                        "<p><button>Connect</button></p></form>")
            if note:
                status += f"<br><b class=bad>{note}</b>"
            self.send(200, SETUP_PAGE.format(status=status, body=body), "text/html; charset=utf-8")

        def finish_auth(self, code):
            try:
                sp.finish_auth(code)
            except SpotifyError as e:
                return self.setup_page(f"Spotify refused the code: {e.message}. Start again from step 1.")
            threading.Thread(target=shelf.refresh, daemon=True).start()
            self.send(303, headers={"Location": "/setup"})

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            p = url.path
            if p == "/":
                self.send(303, headers={"Location": "/sim" if sp.signed_in else "/setup"})
            elif p == "/api/state":
                self.json(deck.state())
            elif p == "/api/shelf":
                self.send(200, shelf.tsv(), headers={"Cache-Control": "no-store"})
            elif p.startswith("/art/"):
                try:
                    img = shelf.cover(p[5:].split(".")[0], int(q.get("s", 240)))
                except Exception as e:
                    log("cover failed:", e)
                    img = None
                if img:
                    self.send(200, img, "image/jpeg", {"Cache-Control": "max-age=86400"})
                else:
                    self.send(404, "no cover")
            elif p == "/sim":
                self.send(200, (HERE / "sim.html").read_bytes(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            elif p == "/setup":
                self.setup_page()
            elif p == "/callback":
                if q.get("code"):
                    self.finish_auth(q["code"])
                else:
                    self.setup_page("Spotify sign-in was cancelled: " + q.get("error", "no code"))
            else:
                self.send(404, "not found")

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode(errors="replace") if length else ""
            p = url.path
            if p == "/api/play":
                deck.play(q.get("album", ""))
                self.json(deck.state())
            elif p == "/api/press":
                deck.press()
                self.json(deck.state())
            elif p == "/api/volume":
                try:
                    deck.set_volume(int(q.get("v", "")))
                except ValueError:
                    return self.send(400, "v must be a number")
                self.json(deck.state())
            elif p == "/api/refresh":
                threading.Thread(target=shelf.refresh, daemon=True).start()
                self.send(303, headers={"Location": "/setup"})
            elif p == "/setup":
                pasted = dict(urllib.parse.parse_qsl(body)).get("url", "").strip()
                code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(pasted).query)).get("code") or ""
                if not code:
                    return self.setup_page("That address has no code in it. Copy the whole address after signing in.")
                self.finish_auth(code)
            else:
                self.send(404, "not found")

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--data", default=str(HERE / "data"), help="where tokens, the shelf and cover art are kept")
    ap.add_argument("--demo", action="store_true", help="fake library and speaker, no Spotify account needed")
    ap.add_argument("--demo-speed", type=float, default=1.0, help="play demo records this many times faster")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    cfg = dict(DEFAULTS)
    if Path(args.config).exists():
        cfg.update(json.loads(Path(args.config).read_text()))
    if args.port:
        cfg["port"] = args.port
    data = Path(args.data + ("-demo" if args.demo else ""))
    data.mkdir(parents=True, exist_ok=True)

    sp = DemoSpotify(args.demo_speed) if args.demo else Spotify(cfg, data)
    if args.demo:
        cfg["needle_sound"] = False
    shelf = Shelf(sp, cfg, data)
    deck = Deck(sp, shelf, cfg, data)

    def refresher():
        while True:
            if sp.signed_in:
                shelf.refresh()
            time.sleep(cfg["refresh_hours"] * 3600 if sp.signed_in else 10)

    def poller():
        while True:
            try:
                wait = deck.poll()
            except Exception as e:          # never let the poller die
                log("poller:", repr(e))
                wait = 5.0
            time.sleep(wait / max(1.0, getattr(sp, "speed", 1.0)))

    threading.Thread(target=refresher, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", cfg["port"]), make_handler(sp, shelf, deck, cfg))
    server.daemon_threads = True
    log(f"turntable listening on port {cfg['port']}" + (" (demo)" if args.demo else ""))
    if not sp.signed_in:
        log(f"not connected to Spotify yet: open http://<this machine>:{cfg['port']}/setup")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
