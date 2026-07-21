import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from scripts import dataset_viewer


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


class DatasetViewerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "dataset_test"
        (self.root / "metadata").mkdir(parents=True)
        for modality in ("images", "right", "depth"):
            (self.root / modality).mkdir()
        rows = []
        order = 0
        for site_index, session in ((1, "20260611_100000"), (2, "20260611_110000")):
            for group, count in (("add_0001", 1), ("add_0002", 2)):
                group_dir = f"site{site_index:02d}_add{dataset_viewer.increment_value(group):03d}"
                for modality in ("images", "right", "depth"):
                    (self.root / modality / group_dir).mkdir(parents=True, exist_ok=True)
                for group_index in range(count):
                    order += 1
                    image_id = f"{session}_{order:06d}"
                    left = f"images/{group_dir}/{image_id}.jpg"
                    right = f"right/{group_dir}/{image_id}.jpg"
                    depth = f"depth/{group_dir}/{image_id}.png"
                    Image.new("RGB", (32, 18), (20 * order, 40, 60)).save(self.root / left)
                    Image.new("RGB", (32, 18), (60, 40, 20 * order)).save(self.root / right)
                    depth_values = np.array([
                        [0, 500, 1_000, 2_000],
                        [3_000, 5_000, 10_000, 20_000],
                    ], dtype=np.uint16)
                    Image.fromarray(depth_values).save(self.root / depth)
                    rows.append({
                        "sample_order": str(order),
                        "image_id": image_id,
                        "session_id": session,
                        "site_id": f"site{site_index:02d}",
                        "increment_group": group,
                        "actual_frame_index": str(order),
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

    def tearDown(self):
        self.temporary.cleanup()

    def test_manifest_filters_presets_and_sorting(self):
        viewer = dataset_viewer.DatasetViewer(self.root, page_size=2)
        first = viewer.state()
        self.assertEqual(first["total"], 6)
        self.assertEqual(first["filtered_total"], 6)
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(
            [(group["label"], group["count"]) for group in first["groups"]],
            [("add001", 2), ("add002", 4)],
        )
        self.assertEqual(
            first["presets"],
            [
                {"label": "n001", "groups": ["add_0001"]},
                {"label": "n003", "groups": ["add_0001", "add_0002"]},
            ],
        )

        filtered = viewer.state(
            limit=20, groups=["add_0002"], sites=["site02"], sort="image_id"
        )
        self.assertEqual(filtered["filtered_total"], 2)
        self.assertTrue(all(
            item["site_id"] == "site02" and item["increment_group"] == "add_0002"
            for item in filtered["items"]
        ))
        searched = viewer.state(limit=20, search="110000")
        self.assertEqual(searched["filtered_total"], 3)
        self.assertEqual(
            viewer.state(groups=["__none__"])["filtered_total"], 0
        )
        self.assertEqual(
            viewer.state(sites=["__none__"])["filtered_total"], 0
        )

    def test_assets_and_thumbnails_are_read_only(self):
        viewer = dataset_viewer.DatasetViewer(self.root)
        item = viewer.state(limit=1)["items"][0]
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}
        self.assertEqual(item["available_modalities"], ["left", "right", "depth"])
        self.assertTrue(viewer.regular_thumbnail("left", item["image_id"]).startswith(b"\xff\xd8"))
        depth_thumbnail = viewer.depth_thumbnail(
            item["image_id"], dataset_viewer.DepthOptions()
        )
        self.assertTrue(depth_thumbnail.startswith(b"\x89PNG"))
        after = {path.relative_to(self.root) for path in self.root.rglob("*")}
        self.assertEqual(before, after)

    def test_depth_colormap_uses_png16_values_and_invalid_black(self):
        depth_path = next((self.root / "depth").glob("*/*.png"))
        options = dataset_viewer.DepthOptions(
            minimum_m=0.5,
            maximum_m=10.0,
            gamma=1.0,
            colormap="viridis",
            invert=False,
        )
        image = dataset_viewer.depth_to_image(depth_path, options)
        pixels = np.asarray(image)
        self.assertEqual(image.size, (4, 2))
        self.assertEqual(tuple(pixels[0, 0]), (0, 0, 0))
        self.assertNotEqual(tuple(pixels[0, 2]), tuple(pixels[1, 2]))

        inverted = np.asarray(dataset_viewer.depth_to_image(
            depth_path,
            dataset_viewer.DepthOptions(
                minimum_m=0.5,
                maximum_m=10.0,
                colormap="viridis",
                invert=True,
            ),
        ))
        self.assertFalse(np.array_equal(pixels[0, 2], inverted[0, 2]))
        automatic = dataset_viewer.depth_to_image(
            depth_path,
            dataset_viewer.DepthOptions(auto_range=True),
            thumbnail=True,
        )
        self.assertEqual(automatic.size, (4, 2))

    def test_dataset_paths_cannot_escape_root(self):
        viewer = dataset_viewer.DatasetViewer(self.root)
        row = viewer.rows[0]
        original = row["left_path"]
        row["left_path"] = "../../outside.jpg"
        try:
            with self.assertRaises(dataset_viewer.ViewerError):
                viewer.resolve_asset(row, "left")
        finally:
            row["left_path"] = original

    def test_invalid_depth_options_are_rejected(self):
        with self.assertRaises(dataset_viewer.ViewerError):
            dataset_viewer.DepthOptions(minimum_m=10, maximum_m=5).validate()
        with self.assertRaises(dataset_viewer.ViewerError):
            dataset_viewer.DepthOptions(colormap="unknown").validate()

    def test_lateral_depth_removes_forward_trend_and_preserves_side_differences(self):
        height, width = 64, 128
        y = np.arange(height, dtype=np.float32)[:, None]
        depth = np.broadcast_to(2000 + 20 * y, (height, width)).copy()
        depth[:, 45:60] -= 120
        depth[:, 85:100] += 150
        depth[0, 0] = 0
        residual, valid = dataset_viewer.lateral_depth_residual(
            depth, dataset_viewer.LateralDepthOptions(scale_mm=250)
        )

        self.assertEqual(residual.shape, depth.shape)
        self.assertFalse(valid[0, 0])
        self.assertGreater(float(np.median(residual[5:, 50])), 110)
        self.assertLess(float(np.median(residual[5:, 90])), -140)
        self.assertLess(abs(float(np.median(residual[5:, 70]))), 1)
        self.assertLess(float(np.max(np.abs(residual[5:, 70]))), 1)

    def test_lateral_depth_colorizes_all_valid_pixels_and_invalid_black(self):
        depth_path = next((self.root / "depth").glob("*/*.png"))
        height, width = 64, 128
        y = np.arange(height, dtype=np.uint16)[:, None]
        depth = np.broadcast_to(2000 + 10 * y, (height, width)).copy()
        depth[:, 45:60] -= 120
        depth[:, 85:100] += 150
        depth[0, 0] = 0
        Image.fromarray(depth).save(depth_path)

        pixels = np.asarray(dataset_viewer.lateral_depth_to_image(
            depth_path, dataset_viewer.LateralDepthOptions(scale_mm=250)
        ))
        automatic = dataset_viewer.lateral_depth_to_image(
            depth_path, dataset_viewer.LateralDepthOptions(scale_mm=None)
        )

        self.assertEqual(pixels.shape, (height, width, 3))
        self.assertEqual(tuple(pixels[0, 0]), (0, 0, 0))
        self.assertTrue(np.all(np.any(pixels[depth > 0] != 0, axis=1)))
        self.assertGreater(pixels[32, 50, 0], pixels[32, 50, 2])
        self.assertGreater(pixels[32, 90, 2], pixels[32, 90, 0])
        self.assertEqual(automatic.size, (width, height))

    def test_invalid_lateral_depth_options_are_rejected(self):
        with self.assertRaises(dataset_viewer.ViewerError):
            dataset_viewer.LateralDepthOptions(scale_mm=10).validate()
        with self.assertRaises(dataset_viewer.ViewerError):
            dataset_viewer.LateralDepthOptions(smoothing_rows=20).validate()

if __name__ == "__main__":
    unittest.main()
