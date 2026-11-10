"""Run with:  python3 -m unittest -v  (from this folder)"""
import tempfile
import time
import unittest
from pathlib import Path

import turntable as tt


def track(no, minutes, disc=1):
    return {"uri": f"spotify:track:{disc}-{no}", "name": f"T{no}", "ms": int(minutes * 60_000), "disc": disc, "no": no}


def liked(album_id, title, artist, date="2001-05-01", kind="album", total=10):
    return {"album": {"id": album_id, "name": title, "artists": [{"name": artist}], "release_date": date,
                      "album_type": kind, "total_tracks": total,
                      "images": [{"url": "big", "width": 640}, {"url": "mid", "width": 300}, {"url": "small", "width": 64}]}}


class SidesTest(unittest.TestCase):
    def test_classic_lp_gets_two_even_sides(self):
        sides = tt.split_sides([track(i + 1, 4) for i in range(10)])
        self.assertEqual([len(s) for s in sides], [5, 5])

    def test_cut_lands_on_the_boundary_nearest_half(self):
        sides = tt.split_sides([track(1, 3), track(2, 3), track(3, 12), track(4, 3), track(5, 3), track(6, 12)])
        self.assertEqual([[t["no"] for t in s] for s in sides], [[1, 2, 3], [4, 5, 6]])

    def test_short_release_is_one_side(self):
        self.assertEqual(len(tt.split_sides([track(1, 4), track(2, 4), track(3, 4)])), 1)
        self.assertEqual(len(tt.split_sides([track(i + 1, 2) for i in range(5)])), 1)

    def test_long_album_gets_more_sides(self):
        sides = tt.split_sides([track(i + 1, 5) for i in range(16)])      # 80 minutes
        self.assertEqual(len(sides), 4)
        self.assertTrue(all(sum(t["ms"] for t in s) <= 23 * 60_000 for s in sides))

    def test_each_disc_is_split_separately_and_order_is_kept(self):
        tracks = [track(i + 1, 4, disc=2) for i in range(6)] + [track(i + 1, 4, disc=1) for i in range(6)]
        sides = tt.split_sides(tracks)
        self.assertEqual([(s[0]["disc"], [t["no"] for t in s]) for s in sides],
                         [(1, [1, 2, 3]), (1, [4, 5, 6]), (2, [1, 2, 3]), (2, [4, 5, 6])])

    def test_every_track_is_on_exactly_one_side(self):
        tracks = [track(i + 1, 1 + (i * 7) % 9) for i in range(23)]
        flat = [t for s in tt.split_sides(tracks) for t in s]
        self.assertEqual(flat, tracks)


class ShelfTest(unittest.TestCase):
    def test_groups_sorts_and_filters(self):
        saved = [liked("b", "Later", "The Beatles", "1969"), liked("a", "Early", "The Beatles", "1963"),
                 liked("c", "Solo", "Abba"), liked("a", "Early", "The Beatles", "1963"),
                 liked("s", "One Song", "Zed", kind="single", total=1), liked("e", "An EP", "Zed", kind="single", total=5),
                 liked("n", "Numbers", "!!!"), {"album": {"id": None}}, None]
        shelf = tt.build_shelf(saved)
        self.assertEqual([a["id"] for a in shelf], ["c", "a", "b", "e", "n"])
        self.assertEqual(shelf[1]["liked"], 2)
        self.assertEqual(shelf[1]["image"], "mid")
        self.assertEqual([a["id"] for a in tt.build_shelf(saved, include_singles=True)][-2], "s")
        self.assertEqual([a["id"] for a in tt.build_shelf(saved, min_liked=2)], ["a"])

    def test_fold_makes_names_ascii(self):
        self.assertEqual(tt.fold("Beyoncé – Sigur Rós\tx"), "Beyonce - Sigur Ros x")


class DeckTest(unittest.TestCase):
    """Play a whole demo record at high speed and watch the sides go by."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        data = Path(self.dir.name)
        self.cfg = dict(tt.DEFAULTS, needle_sound=False)
        self.sp = tt.DemoSpotify(speed=400)
        self.shelf = tt.Shelf(self.sp, self.cfg, data)
        self.shelf.refresh()
        self.deck = tt.Deck(self.sp, self.shelf, self.cfg, data)

    def tearDown(self):
        self.dir.cleanup()

    def run_until(self, mode, seconds=20):
        end = time.time() + seconds
        while time.time() < end:
            self.deck.grace_until = 0
            self.deck.poll()
            if self.deck.mode == mode:
                return
            time.sleep(0.01)
        self.fail(f"never reached {mode}, stuck in {self.deck.mode}")

    def test_sides_flip_and_end(self):
        album = next(a for a in self.shelf.albums if len(tt.split_sides(self.sp.album_tracks(a["id"]))) == 2)
        self.deck.play(album["id"])
        self.assertEqual(self.deck.state()["mode"], "playing")
        self.assertEqual(self.deck.state()["side"], "A")
        self.run_until("flip")
        self.assertFalse(self.sp.playing)
        self.deck.press()
        self.assertEqual(self.deck.state()["side"], "B")
        self.run_until("end")
        self.deck.press()
        self.assertEqual((self.deck.mode, self.deck.side), ("playing", 0))

    def test_pause_and_resume_keep_the_place(self):
        self.sp.speed = 1
        album = self.shelf.albums[0]
        self.deck.play(album["id"])
        self.deck.pos = 200_000          # pretend we are a few minutes in
        self.deck.press()
        self.assertEqual(self.deck.mode, "paused")
        self.assertFalse(self.sp.playing)
        self.deck.press()
        self.assertEqual(self.deck.mode, "playing")
        idx, offset = self.deck._locate(200_000)
        self.assertEqual((self.sp.idx, int(self.sp.pos)), (idx, int(offset)))

    def test_pause_from_the_phone_is_noticed(self):
        self.sp.speed = 1
        self.deck.play(self.shelf.albums[0]["id"])
        self.sp.pause()
        self.deck.grace_until = 0
        self.deck.poll()
        self.assertEqual(self.deck.mode, "paused")

    def test_something_else_playing_takes_the_record_off(self):
        self.sp.speed = 1
        a, b = self.shelf.albums[0], self.shelf.albums[1]
        self.deck.play(a["id"])
        self.sp.play("x", [t["uri"] for t in self.sp.album_tracks(b["id"])])
        for _ in range(3):
            self.deck.grace_until = 0
            self.deck.poll()
        self.assertEqual(self.deck.state()["mode"], "idle")

    def test_cover_is_a_baseline_jpeg_of_the_asked_size(self):
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(self.shelf.cover(self.shelf.albums[0]["id"], 168)))
        self.assertEqual((img.format, img.size), ("JPEG", (168, 168)))
        self.assertFalse(img.info.get("progressive"))


if __name__ == "__main__":
    unittest.main()
