import csv
import random
import tempfile
import unittest
from pathlib import Path

from scripts import svo_extract


class SamplingTests(unittest.TestCase):
    def test_stratified_is_reproducible_sorted_and_spaced(self):
        settings = {
            "count_per_svo": 20,
            "start_margin_seconds": 1,
            "end_margin_seconds": 1,
            "min_gap_seconds": 0.5,
        }
        first = svo_extract.sample_frames(
            "stratified_random", 1000, 10, settings, random.Random(42)
        )
        second = svo_extract.sample_frames(
            "stratified_random", 1000, 10, settings, random.Random(42)
        )
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))
        self.assertEqual(len(first), 20)
        self.assertTrue(all(b - a >= 5 for a, b in zip(first, first[1:])))
        self.assertGreaterEqual(first[0], 10)
        self.assertLessEqual(first[-1], 989)

    def test_uniform_respects_gap(self):
        settings = {
            "count_per_svo": 30,
            "start_margin_seconds": 0,
            "end_margin_seconds": 0,
            "min_gap_seconds": 0.2,
        }
        frames = svo_extract.sample_frames(
            "uniform_random", 500, 10, settings, random.Random(7)
        )
        self.assertTrue(all(b - a >= 2 for a, b in zip(frames, frames[1:])))

    def test_impossible_gap_fails(self):
        settings = {
            "count_per_svo": 100,
            "start_margin_seconds": 0,
            "end_margin_seconds": 0,
            "min_gap_seconds": 1,
        }
        with self.assertRaises(svo_extract.ExtractorError):
            svo_extract.sample_frames(
                "stratified_random", 100, 10, settings, random.Random(1)
            )

    def test_existing_parses_current_naming(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "20260611_104824"
            frames_dir = session / "frames"
            frames_dir.mkdir(parents=True)
            (frames_dir / "20260611_104824_000123.jpg").touch()
            (frames_dir / "20260611_104824_000456.png").touch()
            (frames_dir / "unrelated.jpg").touch()
            settings = {
                "start_margin_seconds": 0,
                "end_margin_seconds": 0,
            }
            frames = svo_extract.sample_frames(
                "existing", 1000, 30, settings, random.Random(1), frames_dir
            )
            self.assertEqual(frames, [123, 456])


class PathAndManifestTests(unittest.TestCase):
    def test_output_is_sibling_exports_directory(self):
        source = Path("/data/day/session/recording.svo2")
        self.assertEqual(
            svo_extract.output_dir_for(source, "run_v1"),
            Path("/data/day/session_exports/run_v1"),
        )

    def test_manifest_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "manifest.csv"
            row = {field: "" for field in svo_extract.MANIFEST_FIELDS}
            row.update({"image_id": "session_000001", "status": "pending"})
            svo_extract.write_manifest(path, [row])
            loaded = svo_extract.read_manifest(path)
            self.assertEqual(loaded, [row])
            with path.open(newline="", encoding="utf-8") as stream:
                self.assertEqual(next(csv.reader(stream)), svo_extract.MANIFEST_FIELDS)

    def test_stable_seed_depends_on_path(self):
        self.assertEqual(
            svo_extract.stable_seed(42, "a/recording.svo2"),
            svo_extract.stable_seed(42, "a/recording.svo2"),
        )
        self.assertNotEqual(
            svo_extract.stable_seed(42, "a/recording.svo2"),
            svo_extract.stable_seed(42, "b/recording.svo2"),
        )

    def test_fingerprint_ignores_existing_mode_and_input_location(self):
        first = svo_extract.deep_merge(svo_extract.DEFAULT_CONFIG, {
            "input": {"root": "/first"},
            "export": {"existing": "error"},
        })
        second = svo_extract.deep_merge(first, {
            "input": {"root": "/second"},
            "export": {"existing": "resume"},
            "progress": {"interval_seconds": 30},
        })
        self.assertEqual(
            svo_extract.config_fingerprint(first),
            svo_extract.config_fingerprint(second),
        )

    def test_fingerprint_changes_for_image_settings(self):
        first = svo_extract.deep_merge(svo_extract.DEFAULT_CONFIG, {})
        second = svo_extract.deep_merge(first, {"outputs": {"left": {"quality": 80}}})
        self.assertNotEqual(
            svo_extract.config_fingerprint(first),
            svo_extract.config_fingerprint(second),
        )

    def test_duration_format(self):
        self.assertEqual(svo_extract.format_duration(0), "00:00")
        self.assertEqual(svo_extract.format_duration(65), "01:05")
        self.assertEqual(svo_extract.format_duration(3661), "1:01:01")


if __name__ == "__main__":
    unittest.main()
