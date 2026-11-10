# Record player

An album-first Spotify player that behaves like a turntable. A knob with a round
screen sits on the shelf; a small Raspberry Pi hidden by the speaker does the playing.

- **The shelf** is every album that contains one of your liked songs, sorted by artist then year.
- **Turn** the knob to flick through covers. **Press** to put the record on.
- The cover fills the round screen with a tonearm over it that tracks inward as the album plays.
- **Turn** while it plays for volume. **Press** to lift the needle, press again to drop it.
- There is no skipping. The album plays start to finish, then stops.
- **Long press** to go back to the shelf. The record keeps playing until you put on another.

## What to buy

Two orders cover the electronics. Prices are approximate; check stock on the day.

**From Adafruit (adafruit.com)**

| Part | Product no. | Why | About |
|---|---|---|---|
| Raspberry Pi Zero 2 WH (pins already soldered) | search "Zero 2 W with header" | Runs Spotify Connect and the turntable server. | $18–20 |
| I2S 3W Stereo Speaker Bonnet | 3346 | Amplifier that pushes onto the Pi's pins. Buy the current version with terminal blocks already soldered. | $13 |
| Medium Surface Transducer with Wires, 4 ohm 3 W (buy two) | 1785 | The exciters: they stick to a panel of the box and turn it into the speaker. | $15 each |
| 5V 2.5A micro-USB power supply | 1995 | Powers the Pi and the amplifier. | $8 |
| microSD card, 16 GB or more | any | Pi storage. | $8–10 |

**From Elecrow (elecrow.com)**

| Part | Why | About |
|---|---|---|
| CrowPanel 2.1" HMI ESP32 Rotary Display (480x480) | The puck: round screen inside a rotating ring that also presses. | $36–44 |

**From anywhere**

- A thin wooden box, roughly 25 x 20 x 8 cm, with panels 3–6 mm thick (a craft-shop plywood box or a cigar box).
- A USB-C cable and USB charger for the puck, and four small rubber feet.

That is roughly $115–125 plus the box. You also need Spotify Premium.

### Building the box

- Stick the two exciters to the inside of the largest thin panel, away from its centre and from each other.
  Clean the wood first; the adhesive ring only sticks well once.
- Screw the exciter wires into the amplifier's two terminal blocks (one exciter per block). No soldering.
- Do not fix the Pi to the panel that carries the exciters. Mount it on the base with a little foam under it.
- Put rubber feet under the puck if it sits on a vibrating panel, or it will buzz. Feet under the box help too.
- Leave a notch for the two power cables.

Expect clear mids, little bass and room-filling rather than loud volume. A thin, stiff, light panel sounds
best; a thick one sounds dull.

## How it fits together

```
 puck (CrowPanel 2.1")  --Wi-Fi-->  Pi: turntable.py  --Spotify Web API-->  Spotify
   draws the interface              shelf, cover art, playback              |
   sends knob turns/presses         Pi: raspotify (Spotify Connect) <--------+
                                      --> amplifier --> exciters on the box
```

- `server/turntable.py` – the brain. Standard-library Python plus Pillow.
- `server/sim.html` – the puck's interface in a browser, served at `/sim`. Use it before the hardware arrives.
- `firmware/` – the puck's software (PlatformIO, Arduino framework).
- `pi/install.sh` – sets up the Pi.

## Try it now, no hardware

```
cd server
python3 turntable.py --demo        # made-up library, pretend speaker
```

Open <http://127.0.0.1:8888/sim>. Scroll to turn, click to press, hold to long press.
Add `--demo-speed 30` to see an album finish without waiting 40 minutes.

To use your real library on a laptop, do steps 1 and 3 below on the laptop and set `device_name`
in `config.json` to a speaker Spotify can already see (for example your computer's name with the Spotify app open).

## Setup

### 1. Spotify app (once)

1. Go to <https://developer.spotify.com/dashboard> and create an app. Choose "Web API".
2. Add the redirect URI `http://127.0.0.1:8888/callback` exactly.
3. Copy the **Client ID**. No client secret is needed.

### 2. The Pi

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In its settings set the hostname to
   `recordplayer`, add your Wi-Fi, and turn on SSH.
2. Push the Speaker Bonnet onto the pins. Add `dtoverlay=hifiberry-dac` to `/boot/firmware/config.txt` and
   comment out `dtparam=audio=on`, then reboot. (That generic overlay is the one Adafruit has long used for this
   board. If there is no sound, follow the setup script in Adafruit's Speaker Bonnet guide instead.)
3. Copy this `record-player` folder to the Pi, then:
   ```
   cd record-player
   bash pi/install.sh
   nano server/config.json          # paste your Client ID
   sudo systemctl restart turntable
   ```
4. In the Spotify app on your phone, choose the speaker **Record Player** once and play a few seconds of
   anything. This lets the Pi remember your account so the speaker is there after every reboot.

### 3. Sign in

Open `http://recordplayer.local:8888/setup` and follow the two steps. After you approve, your browser lands on a
page that will not load. That is expected: copy its whole address and paste it into the setup page.
The shelf fills in after a minute or so, and `http://recordplayer.local:8888/sim` now drives the real thing.

### 4. The puck

1. Install [PlatformIO](https://platformio.org/install) (the VS Code extension is easiest).
2. Copy `firmware/include/config.example.h` to `config.h` and fill in your Wi-Fi and the Pi's address.
3. Plug the puck into your computer with USB-C. If it is not detected, hold its BOOT button while
   plugging it in. Then:
   ```
   cd firmware
   pio run -t upload
   ```

## Settings (`server/config.json`)

| Key | Default | Meaning |
|---|---|---|
| `device_name` | `Record Player` | The Spotify Connect speaker to play on. |
| `min_liked_songs` | `1` | Raise to 2 or 3 to keep stray one-song albums off the shelf. |
| `include_singles` | `false` | Singles with fewer than four tracks are left off unless this is true. |
| `flip_sides` | `false` | Set to true to stop at the end of each side and ask you to flip the record. |
| `side_minutes` | `23` | With `flip_sides`, the longest a side may run. Long albums get sides C, D and so on. |
| `needle_sound` | `true` | A short crackle through the speaker each time the needle drops. |

## Known limits

- **The firmware has not been built or run on a real puck.** It was type-checked against stand-in headers
  only, because the build tools could not be downloaded where it was written. The pins and screen setup are
  copied from Elecrow's own example (`Board21.h` in their GitHub repository). Expect to fix small things on
  first flash. If one click of the ring moves the wrong number of albums, change `ENC_COUNTS_PER_STEP` in `config.h`.
- **Not tested against live Spotify.** The server was tested end to end with the built-in demo library. It uses
  `GET /me/tracks`, `GET /albums/{id}/tracks` and the `/me/player` endpoints. Spotify cut back its developer API
  in February 2026; third-party write-ups do not list these among the removals, but Spotify's own documentation
  could not be read to confirm.
- The puck's font is plain ASCII. Accents are stripped; names in other scripts show as question marks.
- Anyone on your home network can reach the turntable's web page. Do not expose port 8888 to the internet.

## Tests

```
cd server && python3 -m unittest
```
