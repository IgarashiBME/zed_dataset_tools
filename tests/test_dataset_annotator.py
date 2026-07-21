import csv
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts import dataset_annotator


MANIFEST_FIELDS = [
    "sample_order",
    "image_id",
    "session_id",
    "site_id",
    "increment_group",
    "actual_frame_index",
    "left_path",
    "right_path",
    "depth_path",
]


class DatasetAnnotatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "dataset_test"
        (self.root / "metadata").mkdir(parents=True)
        (self.root / "metadata" / "dataset.yaml").write_text(
            "labels:\n  classes:\n    - nakaaze\n", encoding="utf-8"
        )
        rows = []
        order = 0
        for site_number in (1, 2):
            session = f"20260611_{site_number:02d}0000"
            for group, count in (("add_0001", 1), ("add_0002", 2)):
                directory = f"site{site_number:02d}_add{int(group[4:]):03d}"
                for root_name in ("images", "right", "depth", "labels"):
                    (self.root / root_name / directory).mkdir(parents=True, exist_ok=True)
                for _ in range(count):
                    order += 1
                    image_id = f"{session}_{order:06d}"
                    left = f"images/{directory}/{image_id}.jpg"
                    right = f"right/{directory}/{image_id}.jpg"
                    depth = f"depth/{directory}/{image_id}.png"
                    Image.new("RGB", (32, 18), (order * 20, 50, 80)).save(self.root / left)
                    Image.new("RGB", (32, 18), (80, 50, order * 20)).save(self.root / right)
                    Image.new("I;16", (32, 18), 1000).save(self.root / depth)
                    rows.append({
                        "sample_order": str(order),
                        "image_id": image_id,
                        "session_id": session,
                        "site_id": f"site{site_number:02d}",
                        "increment_group": group,
                        "actual_frame_index": str(order * 10),
                        "left_path": left,
                        "right_path": right,
                        "depth_path": depth,
                    })
        with (self.root / "metadata" / "manifest.csv").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        self.application = dataset_annotator.DatasetAnnotator(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def body(far_end="image_boundary"):
        end_points = [] if far_end == "image_boundary" else [[0.35, 0.3], [0.7, 0.2]]
        return {
            "polygon": [[0.3, 0.2], [0.7, 0.2], [0.8, 1.0], [0.2, 1.0]],
            "edit": {
                "mode": "line",
                "left_points": [[0.3, 0.2], [0.2, 0.9]],
                "right_points": [[0.7, 0.2], [0.8, 0.9]],
                "far_end": {"mode": far_end, "points": end_points},
            },
        }

    def test_filters_counts_presets_and_readiness(self):
        state = self.application.state(groups=["add_0001"], statuses=["unannotated"])
        self.assertEqual(state["scope_total"], 2)
        self.assertEqual(state["filtered_total"], 2)
        self.assertEqual(state["counts"]["unannotated"], 2)
        self.assertEqual(
            state["presets"],
            [
                {"label": "n001", "groups": ["add_0001"]},
                {"label": "n003", "groups": ["add_0001", "add_0002"]},
            ],
        )
        self.assertEqual(state["classes"], ["nakaaze"])
        self.assertFalse(state["readiness"][0]["ready"])
        self.assertIn("lateral", state["items"][0]["assets"])

    def test_positive_label_and_edit_state_round_trip(self):
        item = self.application.state(groups=["add_0001"])["items"][0]
        result = self.application.save_positive(item["image_id"], self.body("line"))
        self.assertEqual(result["status"], "positive")
        row = self.application.row_by_id[item["image_id"]]
        label_path = self.application.label_path(row)
        self.assertEqual(
            label_path.read_text(encoding="utf-8"),
            "0 0.300000 0.200000 0.700000 0.200000 0.800000 1.000000 0.200000 1.000000\n",
        )
        annotation = self.application.annotation(item["image_id"])
        self.assertEqual(annotation["status"], "positive")
        self.assertEqual(annotation["polygon"][0], [0.3, 0.2])
        self.assertEqual(annotation["edit"]["far_end"]["mode"], "line")
        sidecar = json.loads(
            self.application.sidecar_path(item["image_id"]).read_text(encoding="utf-8")
        )
        self.assertEqual(sidecar["class_name"], "nakaaze")

    def test_empty_delete_and_status_filter(self):
        image_id = self.application.rows[0]["image_id"]
        self.application.save_empty(image_id)
        self.assertEqual(self.application.annotation(image_id)["status"], "empty")
        self.assertEqual(self.application.label_path(self.application.rows[0]).read_bytes(), b"")
        state = self.application.state(statuses=["empty"])
        self.assertEqual(state["filtered_total"], 1)
        self.assertEqual(state["counts"]["empty"], 1)
        self.application.delete(image_id)
        self.assertFalse(self.application.label_path(self.application.rows[0]).exists())
        self.assertFalse(self.application.sidecar_path(image_id).exists())
        self.assertEqual(self.application.annotation(image_id)["status"], "unannotated")

    def test_validation_rejects_invalid_geometry_and_paths(self):
        image_id = self.application.rows[0]["image_id"]
        invalid = self.body()
        invalid["polygon"] = [[0.1, 0.1], [0.2, 0.2], [0.3, 0.3]]
        with self.assertRaises(dataset_annotator.AnnotatorError):
            self.application.save_positive(image_id, invalid)

        invalid = self.body("line")
        invalid["edit"]["far_end"]["points"] = [[0.2, 0.2], [1.2, 0.3]]
        with self.assertRaises(dataset_annotator.AnnotatorError):
            self.application.save_positive(image_id, invalid)

        row = self.application.rows[0]
        original = row["left_path"]
        row["left_path"] = "../../outside.jpg"
        try:
            with self.assertRaises(dataset_annotator.AnnotatorError):
                self.application.label_path(row)
        finally:
            row["left_path"] = original

    def test_lateral_depth_request_options(self):
        options = dataset_annotator._lateral_depth_request({"scale": ["500"]})
        self.assertEqual(options.scale_mm, 500)
        automatic = dataset_annotator._lateral_depth_request({"scale": ["auto"]})
        self.assertIsNone(automatic.scale_mm)
        defaults = dataset_annotator._lateral_depth_request({})
        self.assertEqual(defaults.scale_mm, 250)
        with self.assertRaises(dataset_annotator.dataset_viewer.ViewerError):
            dataset_annotator._lateral_depth_request({"scale": ["10"]})


if __name__ == "__main__":
    unittest.main()
