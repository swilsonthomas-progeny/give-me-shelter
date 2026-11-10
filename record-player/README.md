# Record player

An album-first Spotify player that behaves like a turntable. A knob with a round
screen sits on the shelf; a small Raspberry Pi hidden by the speaker does the playing.

- **The shelf** is every album that contains one of your liked songs, sorted by artist then year.
- **Turn** the knob to flick through covers. **Press** to put the record on.
- The cover becomes the record's label and spins at 33⅓ rpm. The tonearm tracks inward as the side plays.
- **Turn** while it plays for volume. **Press** to lift the needle, press again to drop it.
- There is no skipping. When side A ends the music stops and the screen asks you to **flip the record**.
- **Long press** to go back to the shelf. The record keeps playing until you put on another.

## What to buy

Prices are approximate and were not checked against a shop on the day this was written.

| Part | Why | About |
|---|---|---|
| **M5Stack Dial v1.1** | The puck: 1.28" round 240x240 screen inside a rotating ring that also presses. | $35–50 |
| **Raspberry Pi Zero 2 WH** (the "H" means the pins are already soldered) | Runs Spotify Connect and the turntable server. | $18–20 |
| **HiFiBerry DAC+ Zero** (or any I2S DAC HAT for the Pi Zero with a line out) | The Pi Zero has no audio socket. This pushes onto the pins, no soldering. | $20–25 |
| microSD card, 16 GB or more | Pi storage. | $8 |
| 5V 2.5A micro-USB power supply | Pi power. | $10 |
| USB-C cable and any USB charger | Power for the puck (and how you load its software). | $8 |
| Cable from the DAC to your speakers (RCA or 3.5 mm, depending on the DAC and speakers) | | $6 |

That is roughly $110–130. You also need powered speakers or an amplifier with a line input, and Spotify Premium.

**If a Bluetooth speaker is the main plan,** buy a Raspberry Pi 4 or 5 instead of the Zero and skip the DAC.
The Zero's Wi-Fi and Bluetooth share one 2.4 GHz radio and audio tends to stutter. Bluetooth pairing is not
set up by the installer here; wired output is.

## How it fits together

```
 puck (M5Stack Dial)  --Wi-Fi-->  Pi: turntable.py  --Spotify Web API-->  Spotify
   draws the interface              shelf, sides, cover art                  |
   sends knob turns/presses         Pi: raspotify (Spotify Connect) <--------+
                                      --> DAC --> speakers
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
Add `--demo-speed 30` to see a side end and the flip prompt without waiting 20 minutes.

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
2. Push the DAC onto the pins. For a HiFiBerry DAC+ Zero add `dtoverlay=hifiberry-dac` to
   `/boot/firmware/config.txt` and comment out `dtparam=audio=on`, then reboot. Other DACs name their own overlay.
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
3. Plug the Dial into your computer with USB-C. If it is not detected, hold the button on the back
   (G0) while plugging it in. Then:
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
| `side_minutes` | `23` | Longest a side may run. Long albums get sides C, D and so on. |
| `needle_sound` | `true` | A short crackle through the speaker before each side. |

Sides are cut per disc at the track boundary nearest the halfway point. Releases under four tracks or
twelve minutes are a single side.

## Known limits

- **The firmware has not been built or run on a real Dial.** It was type-checked against stand-in headers
  only, because the build tools could not be downloaded where it was written. Expect to fix small things on
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
