import csv
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts import dataset_prepare


EXTRACT_FIELDS = [
    "sample_order",
    "image_id",
    "source_svo",
    "requested_frame_index",
    "actual_frame_index",
    "timestamp_ns",
    "left_path",
    "right_path",
    "depth_path",
    "depth_preview_path",
    "width",
    "height",
    "status",
    "error",
]


class DatasetPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input = self.root / "images"
        self.output = self.root / "ridge_data"
        self.labels = self.root / "source_labels"
        self.labels.mkdir(parents=True)
        self.config = dataset_prepare.deep_merge(dataset_prepare.DEFAULT_CONFIG, {
            "input": {
                "roots": [str(self.input)],
                "manifest_pattern": "*_exports/*/manifest.csv",
                "required_modalities": ["left", "right", "depth", "depth_preview"],
            },
            "output": {
                "root": str(self.output),
                "dataset_name": "dataset01_test",
                "review_dir": ".review",
            },
            "selection": {"scope": "per_session", "increments": [1, 2], "seed": 17},
            "review": {"import_from": None},
            "materialize": {"mode": "copy", "fallback_to_copy": True},
            "labels": {
                "format": "yolo_segmentation",
                "extension": "txt",
                "source_root": str(self.labels),
                "required": True,
                "target_view": "left",
                "classes": ["nakaaze"],
            },
        })

    def tearDown(self):
        self.temporary.cleanup()

    def add_session(self, session: str, frames: list[int], include_missing=False):
        export = self.input / f"{session}_exports" / "1000images_v1"
        for modality in ("left", "right", "depth", "depth_preview"):
            (export / modality).mkdir(parents=True, exist_ok=True)
        rows = []
        for order, frame in enumerate(frames, 1):
            image_id = f"{session}_{frame:06d}"
            extensions = {"left": ".jpg", "right": ".jpg", "depth": ".png", "depth_preview": ".png"}
            paths = {}
            for modality, extension in extensions.items():
                relative = f"{modality}/{image_id}{extension}"
                paths[modality] = relative
                if not (include_missing and order == len(frames) and modality == "right"):
                    Image.new("RGB", (16, 9), color=(order * 20, 40, 60)).save(export / relative)
            (self.labels / f"{image_id}.txt").write_text("", encoding="utf-8")
            rows.append({
                "sample_order": str(order),
                "image_id": image_id,
                "source_svo": f"../../{session}/recording.svo2",
                "requested_frame_index": str(frame),
                "actual_frame_index": str(frame),
                "timestamp_ns": str(1_000_000_000 + frame),
                "left_path": paths["left"],
                "right_path": paths["right"],
                "depth_path": paths["depth"],
                "depth_preview_path": paths["depth_preview"],
                "width": "1280",
                "height": "720",
                "status": "completed",
                "error": "",
            })
        with (export / "manifest.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=EXTRACT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def test_plan_is_reproducible_interleaved_and_skips_missing(self):
        self.add_session("20260611_100000", [1, 2, 3])
        self.add_session("20260611_110000", [1, 2, 3], include_missing=True)

        count, stats = dataset_prepare.plan_review(self.config)
        first = dataset_prepare.read_csv(
            dataset_prepare.review_dir(self.config) / "candidates.csv",
            dataset_prepare.CANDIDATE_FIELDS,
        )
        self.assertEqual(count, 5)
        self.assertEqual(stats["missing"], 1)
        self.assertNotEqual(first[0]["session_id"], first[1]["session_id"])
        self.assertEqual({row["site_id"] for row in first}, {"site01", "site02"})
        application = dataset_prepare.ReviewApplication(self.config)
        state = application.state(0, 1, "unreviewed")
        self.assertEqual(state["page_size"], 24)
        self.assertNotIn("depth", state["items"][0]["assets"])
        session_state = application.state(
            0, 10, "unreviewed", state["items"][0]["session_id"]
        )
        self.assertTrue(all(
            item["session_id"] == state["items"][0]["session_id"]
            for item in session_state["items"]
        ))
        thumbnail = application.thumbnail(state["items"][0]["image_id"])
        self.assertTrue(thumbnail.is_file())
        saved = application.save({
            "image_id": state["items"][0]["image_id"],
            "decision": "hold",
            "reject_reason": "",
            "note": "再確認",
            "reviewer": "tester",
        })
        self.assertEqual(saved["counts"]["hold"], 1)

        migrated_config = dataset_prepare.deep_merge(self.config, {
            "output": {"dataset_name": "dataset02_test"},
            "review": {"import_from": str(dataset_prepare.review_dir(self.config))},
        })
        _, migrated_stats = dataset_prepare.plan_review(migrated_config)
        _, migrated_reviews = dataset_prepare.read_review_workspace(migrated_config)
        self.assertEqual(migrated_stats["preserved_reviews"], 1)
        self.assertEqual(
            next(
                row for row in migrated_reviews
                if row["image_id"] == state["items"][0]["image_id"]
            )["decision"],
            "hold",
        )

        dataset_prepare.plan_review(self.config, overwrite=True)
        second = dataset_prepare.read_csv(
            dataset_prepare.review_dir(self.config) / "candidates.csv",
            dataset_prepare.CANDIDATE_FIELDS,
        )
        self.assertEqual(
            [row["image_id"] for row in first],
            [row["image_id"] for row in second],
        )
        self.assertEqual(
            {row["session_id"]: row["site_id"] for row in first},
            {row["session_id"]: row["site_id"] for row in second},
        )
        _, reviews = dataset_prepare.read_review_workspace(self.config)
        self.assertEqual(
            next(row for row in reviews if row["image_id"] == state["items"][0]["image_id"])["decision"],
            "hold",
        )

    def test_review_selection_build_and_verify_txt_labels(self):
        self.add_session("20260611_100000", [1, 2, 3, 4])
        self.add_session("20260611_110000", [1, 2, 3, 4])
        dataset_prepare.plan_review(self.config)
        candidates, _ = dataset_prepare.read_review_workspace(self.config)

        for candidate in candidates:
            dataset_prepare.update_review(
                self.config, candidate["image_id"], "keep", "", "", "tester"
            )
        selections = dataset_prepare.create_selection(self.config)
        self.assertEqual(len(selections), 6)
        self.assertEqual(
            [row["increment_group"] for row in selections],
            ["add_0001", "add_0001", "add_0002", "add_0002", "add_0002", "add_0002"],
        )
        self.assertEqual(
            {row["session_id"] for row in selections[:2]},
            {"20260611_100000", "20260611_110000"},
        )

        destination, count = dataset_prepare.build_dataset(self.config)
        self.assertEqual(count, 6)
        manifest = dataset_prepare.read_csv(
            destination / "metadata" / "manifest.csv", dataset_prepare.DATASET_FIELDS
        )
        self.assertTrue(all(row["label_path"].endswith(".txt") for row in manifest))
        self.assertTrue(all(row["label_status"] == "completed" for row in manifest))
        self.assertTrue((destination / "images" / "site01_add001").is_dir())
        self.assertTrue((destination / "labels" / "site02_add002").is_dir())
        small = dataset_prepare.load_yaml(destination / "yaml" / "dataset_n001.yaml")
        self.assertEqual(
            small["train"],
            ["images/site01_add001", "images/site02_add001"],
        )
        large = dataset_prepare.load_yaml(destination / "yaml" / "dataset_n003.yaml")
        self.assertEqual(
            large["train"],
            [
                "images/site01_add001", "images/site01_add002",
                "images/site02_add001", "images/site02_add002",
            ],
        )
        self.assertTrue((destination / "metadata" / "site_map.csv").is_file())
        verified, errors = dataset_prepare.verify_dataset(destination)
        self.assertEqual(verified, 6)
        self.assertEqual(errors, [])

    def test_reject_defaults_to_other_and_does_not_count_as_keep(self):
        self.add_session("20260611_100000", [1, 2, 3])
        dataset_prepare.plan_review(self.config)
        candidates, _ = dataset_prepare.read_review_workspace(self.config)
        rejected = dataset_prepare.update_review(
            self.config, candidates[0]["image_id"], "reject", "", "", "tester"
        )
        self.assertEqual(rejected["reject_reason"], "other")
        for candidate in candidates[1:]:
            dataset_prepare.update_review(
                self.config, candidate["image_id"], "keep", "", "", "tester"
            )
        with self.assertRaises(dataset_prepare.DatasetError):
            dataset_prepare.create_selection(self.config)


if __name__ == "__main__":
    unittest.main()
