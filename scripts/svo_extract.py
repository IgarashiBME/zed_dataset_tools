#!/usr/bin/env python3
"""Extract selected left/right/depth frames from ZED SVO/SVO2 files."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CONFIG: dict[str, Any] = {
    "input": {"root": ".", "pattern": "*/*/recording.svo2"},
    "export": {"name": "candidates_v1", "existing": "error"},
    "sampling": {
        "method": "stratified_random",
        "count_per_svo": 100,
        "seed": 42,
        "start_margin_seconds": 0.0,
        "end_margin_seconds": 0.0,
        "min_gap_seconds": 0.0,
        "frames": [],
        "interval_seconds": None,
        "interval_frames": None,
    },
    "playback": {"strategy": "hybrid", "seek_threshold_frames": 120},
    "progress": {"interval_seconds": 5.0},
    "outputs": {
        "left": {"enabled": True, "format": "jpg", "quality": 95, "compression": 3},
        "right": {"enabled": True, "format": "jpg", "quality": 95, "compression": 3},
        "depth": {
            "enabled": True,
            "format": "png16",
            "unit": "millimeter",
            "invalid_value": 0,
            "max_depth_meters": 40.0,
        },
        "depth_preview": {"enabled": False, "format": "png", "colormap": "grayscale"},
    },
    "zed": {
        "depth_mode": "NEURAL_LIGHT",
        "confidence_threshold": 50,
        "texture_confidence_threshold": 100,
    },
    "image": {"resolution": "native"},
}

MANIFEST_FIELDS = [
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


class ExtractorError(RuntimeError):
    """Expected user-facing failure."""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise ExtractorError("PyYAMLが必要です: pip install PyYAML") from exc

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        raise ExtractorError(f"設定ファイルを読み込めません: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExtractorError("設定ファイルのルートはYAMLマッピングである必要があります")
    config = deep_merge(DEFAULT_CONFIG, raw)
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    export_name = str(config["export"]["name"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", export_name):
        raise ExtractorError("export.nameには英数字、_、-、.のみ使用できます")
    if config["export"]["existing"] not in {"error", "resume", "skip", "overwrite"}:
        raise ExtractorError("export.existingはerror/resume/skip/overwriteのいずれかです")
    if config["sampling"]["method"] not in {
        "stratified_random", "uniform_random", "interval", "explicit", "existing"
    }:
        raise ExtractorError("未対応のsampling.methodです")
    if int(config["sampling"]["count_per_svo"]) < 1:
        raise ExtractorError("sampling.count_per_svoは1以上にしてください")
    if config["playback"]["strategy"] not in {"seek", "scan", "hybrid"}:
        raise ExtractorError("playback.strategyはseek/scan/hybridのいずれかです")
    if float(config["progress"]["interval_seconds"]) < 0:
        raise ExtractorError("progress.interval_secondsは0以上にしてください")
    if config["image"]["resolution"] != "native":
        raise ExtractorError("初期版で対応するimage.resolutionはnativeのみです")
    for name in ("left", "right"):
        if config["outputs"][name]["format"].lower() not in {"jpg", "jpeg", "png"}:
            raise ExtractorError(f"outputs.{name}.formatはjpgまたはpngにしてください")
    if config["outputs"]["depth"]["format"].lower() not in {"png16", "npy"}:
        raise ExtractorError("outputs.depth.formatはpng16またはnpyにしてください")
    if config["outputs"]["depth"]["unit"] not in {"millimeter", "meter"}:
        raise ExtractorError("outputs.depth.unitはmillimeterまたはmeterにしてください")
    if (config["outputs"]["depth"]["format"].lower() == "png16"
            and config["outputs"]["depth"]["unit"] != "millimeter"):
        raise ExtractorError("png16深度の単位はmillimeterにしてください")
    if config["outputs"]["depth_preview"].get("colormap", "grayscale") != "grayscale":
        raise ExtractorError("初期版のdepth_preview.colormapはgrayscaleのみ対応します")
    if not any(bool(config["outputs"][name]["enabled"])
               for name in ("left", "right", "depth", "depth_preview")):
        raise ExtractorError("少なくとも1種類の出力を有効にしてください")


def config_fingerprint(config: dict[str, Any]) -> str:
    """Fingerprint settings that affect frame selection or generated files."""
    relevant = deepcopy(config)
    relevant["export"] = {"name": relevant["export"]["name"]}
    relevant.pop("input", None)
    relevant.pop("progress", None)
    encoded = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stable_seed(seed: int, relative_path: str) -> int:
    digest = hashlib.sha256(f"{seed}:{relative_path}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def discover_svos(root: Path, pattern: str) -> list[Path]:
    paths = []
    for path in root.glob(pattern):
        if path.is_file() and not any(part.endswith("_exports") for part in path.parts):
            paths.append(path.resolve())
    return sorted(set(paths))


def output_dir_for(svo_path: Path, export_name: str) -> Path:
    session_dir = svo_path.parent
    return session_dir.with_name(f"{session_dir.name}_exports") / export_name


def _compressed_stratified_sample(
    first: int, last: int, count: int, min_gap: int, rng: random.Random
) -> list[int]:
    total = last - first + 1
    compressed_total = total - (count - 1) * (min_gap - 1)
    if compressed_total < count:
        raise ExtractorError(
            f"有効範囲{total}フレームから、最小間隔{min_gap}で{count}枚は抽出できません"
        )
    chosen: list[int] = []
    for index in range(count):
        low = math.floor(index * compressed_total / count)
        high = math.floor((index + 1) * compressed_total / count) - 1
        chosen.append(rng.randint(low, max(low, high)))
    return [first + value + index * (min_gap - 1) for index, value in enumerate(chosen)]


def sample_frames(
    method: str,
    frame_count: int,
    fps: float,
    sampling: dict[str, Any],
    rng: random.Random,
    existing_dir: Path | None = None,
) -> list[int]:
    if frame_count < 1 or fps <= 0:
        raise ExtractorError("SVO2のフレーム数またはFPSが不正です")
    start = int(math.ceil(float(sampling["start_margin_seconds"]) * fps))
    end = frame_count - 1 - int(math.ceil(float(sampling["end_margin_seconds"]) * fps))
    if start > end:
        raise ExtractorError("開始・終了マージンにより抽出可能なフレームがありません")

    if method == "explicit":
        values = sorted(set(int(value) for value in sampling.get("frames", [])))
        invalid = [value for value in values if value < start or value > end]
        if invalid:
            raise ExtractorError(f"範囲外の明示フレームがあります: {invalid[:10]}")
        if not values:
            raise ExtractorError("sampling.framesが空です")
        return values

    if method == "existing":
        if existing_dir is None or not existing_dir.is_dir():
            raise ExtractorError(f"既存framesディレクトリがありません: {existing_dir}")
        session = existing_dir.parent.name
        pattern = re.compile(rf"^{re.escape(session)}_(\d+)\.[^.]+$")
        values = sorted({int(match.group(1)) for path in existing_dir.iterdir()
                         if path.is_file() and (match := pattern.match(path.name))})
        values = [value for value in values if start <= value <= end]
        if not values:
            raise ExtractorError(f"既存画像からフレーム番号を取得できません: {existing_dir}")
        return values

    if method == "interval":
        interval_frames = sampling.get("interval_frames")
        if interval_frames is None:
            seconds = sampling.get("interval_seconds")
            if seconds is None or float(seconds) <= 0:
                raise ExtractorError("interval_secondsまたはinterval_framesを指定してください")
            interval_frames = max(1, round(float(seconds) * fps))
        interval_frames = int(interval_frames)
        if interval_frames < 1:
            raise ExtractorError("抽出間隔は1フレーム以上にしてください")
        return list(range(start, end + 1, interval_frames))

    count = int(sampling["count_per_svo"])
    min_gap = max(1, int(math.ceil(float(sampling["min_gap_seconds"]) * fps)))
    if method == "stratified_random":
        return _compressed_stratified_sample(start, end, count, min_gap, rng)

    total = end - start + 1
    compressed_total = total - (count - 1) * (min_gap - 1)
    if compressed_total < count:
        raise ExtractorError(
            f"有効範囲{total}フレームから、最小間隔{min_gap}で{count}枚は抽出できません"
        )
    compressed = sorted(rng.sample(range(compressed_total), count))
    return [start + value + index * (min_gap - 1) for index, value in enumerate(compressed)]


def import_zed():
    try:
        import pyzed.sl as sl
    except ImportError as exc:
        raise ExtractorError("ZED SDK Python API (pyzed) をimportできません") from exc
    return sl


def enum_member(enum_class: Any, name: str, field: str) -> Any:
    try:
        return getattr(enum_class, str(name).upper())
    except AttributeError as exc:
        raise ExtractorError(f"未対応の{field}: {name}") from exc


def open_svo(svo_path: Path, config: dict[str, Any], need_depth: bool):
    sl = import_zed()
    camera = sl.Camera()
    init = sl.InitParameters()
    init.set_from_svo_file(str(svo_path))
    init.svo_real_time_mode = False
    depth = config["outputs"]["depth"]
    init.coordinate_units = sl.UNIT.MILLIMETER if depth["unit"] == "millimeter" else sl.UNIT.METER
    init.depth_mode = enum_member(sl.DEPTH_MODE, config["zed"]["depth_mode"], "depth_mode") if need_depth else sl.DEPTH_MODE.NONE
    init.depth_maximum_distance = float(depth["max_depth_meters"]) * (1000.0 if depth["unit"] == "millimeter" else 1.0)
    status = camera.open(init)
    if status != sl.ERROR_CODE.SUCCESS:
        camera.close()
        raise ExtractorError(f"SVO2を開けません: {svo_path}: {status}")
    return sl, camera


def svo_metadata(svo_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    sl, camera = open_svo(svo_path, config, need_depth=False)
    try:
        info = camera.get_camera_information()
        camera_config = info.camera_configuration
        stat = svo_path.stat()
        return {
            "path": str(svo_path),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "frame_count": int(camera.get_svo_number_of_frames()),
            "fps": float(camera_config.fps),
            "width": int(camera_config.resolution.width),
            "height": int(camera_config.resolution.height),
            "camera_model": str(info.camera_model),
            "sdk_version": str(sl.Camera.get_sdk_version()),
        }
    finally:
        camera.close()


def extension_for(output: dict[str, Any], depth: bool = False) -> str:
    fmt = output["format"].lower()
    if depth:
        return ".png" if fmt == "png16" else ".npy"
    return ".jpg" if fmt in {"jpg", "jpeg"} else ".png"


def make_manifest_rows(
    svo_path: Path, output_dir: Path, frames: list[int], config: dict[str, Any]
) -> list[dict[str, str]]:
    session = svo_path.parent.name
    outputs = config["outputs"]
    rows = []
    for order, frame in enumerate(sorted(frames), 1):
        image_id = f"{session}_{frame:06d}"
        row = {field: "" for field in MANIFEST_FIELDS}
        row.update({
            "sample_order": str(order),
            "image_id": image_id,
            "source_svo": os.path.relpath(svo_path, output_dir),
            "requested_frame_index": str(frame),
            "left_path": f"left/{image_id}{extension_for(outputs['left'])}" if outputs["left"]["enabled"] else "",
            "right_path": f"right/{image_id}{extension_for(outputs['right'])}" if outputs["right"]["enabled"] else "",
            "depth_path": f"depth/{image_id}{extension_for(outputs['depth'], depth=True)}" if outputs["depth"]["enabled"] else "",
            "depth_preview_path": f"depth_preview/{image_id}.png" if outputs["depth_preview"]["enabled"] else "",
            "status": "pending",
        })
        rows.append(row)
    return rows


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def write_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


def read_manifest(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != MANIFEST_FIELDS:
                raise ExtractorError(f"manifestの列が期待値と異なります: {path}")
            return list(reader)
    except OSError as exc:
        raise ExtractorError(f"manifestを読み込めません: {path}: {exc}") from exc


def config_snapshot(config: dict[str, Any], metadata: dict[str, Any], svo_path: Path, output_dir: Path) -> dict[str, Any]:
    snapshot = deepcopy(config)
    snapshot["source"] = deepcopy(metadata)
    snapshot["source"]["path"] = os.path.relpath(svo_path, output_dir)
    snapshot["extractor"] = {
        "version": 1,
        "created_at_unix": time.time(),
        "config_fingerprint": config_fingerprint(config),
    }
    return snapshot


def write_config_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    import yaml
    atomic_text(path, yaml.safe_dump(snapshot, allow_unicode=True, sort_keys=False))


def assert_snapshot_matches(path: Path, config: dict[str, Any]) -> None:
    try:
        import yaml
        snapshot = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        recorded = snapshot.get("extractor", {}).get("config_fingerprint")
    except (OSError, ValueError, AttributeError) as exc:
        raise ExtractorError(f"既存config.yamlを確認できません: {path}: {exc}") from exc
    expected = config_fingerprint(config)
    if not recorded:
        raise ExtractorError(f"既存config.yamlに設定フィンガープリントがありません: {path}")
    if recorded != expected:
        raise ExtractorError(
            f"既存の抽出計画と現在の設定が異なります。export.nameを変更してください: {path}"
        )


def plan_one(svo_path: Path, root: Path, config: dict[str, Any]) -> tuple[Path, int]:
    output_dir = output_dir_for(svo_path, config["export"]["name"])
    manifest_path = output_dir / "manifest.csv"
    existing_mode = config["export"]["existing"]
    if manifest_path.exists():
        if existing_mode in {"resume", "skip"}:
            assert_snapshot_matches(output_dir / "config.yaml", config)
            return output_dir, len(read_manifest(manifest_path))
        if existing_mode == "error":
            raise ExtractorError(f"既存の抽出計画があります: {manifest_path}")

    metadata = svo_metadata(svo_path, config)
    relative = os.path.relpath(svo_path, root)
    rng = random.Random(stable_seed(int(config["sampling"]["seed"]), relative))
    frames = sample_frames(
        config["sampling"]["method"],
        metadata["frame_count"],
        metadata["fps"],
        config["sampling"],
        rng,
        svo_path.parent / "frames",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = make_manifest_rows(svo_path, output_dir, frames, config)
    write_manifest(manifest_path, rows)
    write_config_snapshot(output_dir / "config.yaml", config_snapshot(config, metadata, svo_path, output_dir))
    return output_dir, len(rows)


def _atomic_image(path: Path, array: Any, output: dict[str, Any]) -> None:
    from PIL import Image
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.stem}.tmp{path.suffix}")
    pixels = np.asarray(array)
    if pixels.ndim == 3 and pixels.shape[2] == 4:
        # ZED retrieve_image() returns four-channel images in BGRA order.
        # Pillow expects RGB(A), so swap the blue and red channels and drop alpha.
        image = Image.fromarray(pixels[:, :, [2, 1, 0]].astype(np.uint8), "RGB")
    elif pixels.ndim == 3 and pixels.shape[2] == 3:
        image = Image.fromarray(pixels.astype(np.uint8), "RGB")
    else:
        image = Image.fromarray(pixels)
    kwargs: dict[str, Any] = {}
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        kwargs.update(quality=int(output.get("quality", 95)), subsampling=0)
    elif path.suffix.lower() == ".png":
        kwargs["compress_level"] = int(output.get("compression", 3))
    image.save(temp, **kwargs)
    os.replace(temp, path)


def _atomic_npy(path: Path, array: Any) -> None:
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("wb") as stream:
        np.save(stream, np.asarray(array, dtype=np.float32), allow_pickle=False)
    os.replace(temp, path)


def save_depth(path: Path, depth_array: Any, output: dict[str, Any]) -> None:
    import numpy as np

    data = np.asarray(depth_array)
    if data.ndim == 3:
        data = data[:, :, 0]
    if output["format"].lower() == "npy":
        _atomic_npy(path, data)
        return
    max_mm = min(65535.0, float(output["max_depth_meters"]) * 1000.0)
    valid = np.isfinite(data) & (data > 0) & (data <= max_mm)
    encoded = np.full(data.shape, int(output.get("invalid_value", 0)), dtype=np.uint16)
    encoded[valid] = np.rint(data[valid]).astype(np.uint16)
    _atomic_image(path, encoded, {"compression": 3})


def save_depth_preview(path: Path, depth_array: Any, depth_output: dict[str, Any]) -> None:
    import numpy as np

    data = np.asarray(depth_array)
    if data.ndim == 3:
        data = data[:, :, 0]
    scale = float(depth_output["max_depth_meters"])
    if depth_output["unit"] == "millimeter":
        scale *= 1000.0
    valid = np.isfinite(data) & (data > 0)
    preview = np.zeros(data.shape, dtype=np.uint8)
    preview[valid] = np.clip((1.0 - data[valid] / scale) * 255.0, 0, 255).astype(np.uint8)
    _atomic_image(path, preview, {"compression": 3})


def result_ok(status: Any, sl: Any) -> bool:
    return status <= sl.ERROR_CODE.SUCCESS


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class ProgressReporter:
    def __init__(self, total: int, interval_seconds: float) -> None:
        self.total = total
        self.interval_seconds = interval_seconds
        self.started = time.monotonic()
        self.last_reported = float("-inf")

    def update(
        self,
        current: int,
        frame_index: int,
        completed: int,
        failed: int,
    ) -> None:
        now = time.monotonic()
        if current != self.total and now - self.last_reported < self.interval_seconds:
            return
        elapsed = now - self.started
        percent = current / self.total * 100.0 if self.total else 100.0
        print(
            f"  progress {current}/{self.total} ({percent:5.1f}%) "
            f"frame={frame_index} completed={completed} failed={failed} "
            f"elapsed={format_duration(elapsed)}",
            flush=True,
        )
        self.last_reported = now


def grab_until(camera: Any, sl: Any, runtime: Any, next_index: int, target: int) -> int:
    while next_index < target:
        status = camera.grab(runtime)
        if not result_ok(status, sl):
            raise ExtractorError(f"フレーム{next_index}のスキップ中にgrabが失敗しました: {status}")
        actual = int(camera.get_svo_position())
        next_index = actual + 1
    return next_index


def extract_one(svo_path: Path, config: dict[str, Any]) -> tuple[int, int]:
    output_dir = output_dir_for(svo_path, config["export"]["name"])
    manifest_path = output_dir / "manifest.csv"
    if not manifest_path.exists():
        raise ExtractorError(f"先にplanを実行してください: {manifest_path}")
    assert_snapshot_matches(output_dir / "config.yaml", config)
    rows = read_manifest(manifest_path)
    need_depth = bool(config["outputs"]["depth"]["enabled"] or config["outputs"]["depth_preview"]["enabled"])
    sl, camera = open_svo(svo_path, config, need_depth=need_depth)
    left_mat, right_mat, depth_mat = sl.Mat(), sl.Mat(), sl.Mat()
    skip_runtime = sl.RuntimeParameters()
    skip_runtime.enable_depth = False
    selected_runtime = sl.RuntimeParameters()
    selected_runtime.enable_depth = need_depth
    selected_runtime.confidence_threshold = int(config["zed"]["confidence_threshold"])
    selected_runtime.texture_confidence_threshold = int(config["zed"]["texture_confidence_threshold"])
    strategy = config["playback"]["strategy"]
    threshold = int(config["playback"]["seek_threshold_frames"])
    completed = failed = 0
    next_index = 0
    ordered_rows = sorted(rows, key=lambda item: int(item["requested_frame_index"]))
    progress = ProgressReporter(len(ordered_rows), float(config["progress"]["interval_seconds"]))
    try:
        for progress_index, row in enumerate(ordered_rows, 1):
            target = int(row["requested_frame_index"])
            if row["status"] == "completed" and verify_row(output_dir, row) is None:
                completed += 1
                progress.update(progress_index, target, completed, failed)
                continue
            try:
                if target < next_index or strategy == "seek" or (strategy == "hybrid" and target - next_index > threshold):
                    camera.set_svo_position(target)
                    next_index = target
                else:
                    next_index = grab_until(camera, sl, skip_runtime, next_index, target)

                status = camera.grab(selected_runtime)
                if not result_ok(status, sl):
                    raise ExtractorError(f"grabが失敗しました: {status}")
                actual = int(camera.get_svo_position())
                next_index = actual + 1
                if actual != target:
                    raise ExtractorError(f"要求フレーム{target}に対して{actual}を取得しました")

                outputs = config["outputs"]
                if outputs["left"]["enabled"]:
                    status = camera.retrieve_image(left_mat, sl.VIEW.LEFT, sl.MEM.CPU)
                    if not result_ok(status, sl):
                        raise ExtractorError(f"左画像の取得に失敗しました: {status}")
                    _atomic_image(output_dir / row["left_path"], left_mat.get_data(), outputs["left"])
                if outputs["right"]["enabled"]:
                    status = camera.retrieve_image(right_mat, sl.VIEW.RIGHT, sl.MEM.CPU)
                    if not result_ok(status, sl):
                        raise ExtractorError(f"右画像の取得に失敗しました: {status}")
                    _atomic_image(output_dir / row["right_path"], right_mat.get_data(), outputs["right"])
                depth_data = None
                if need_depth:
                    status = camera.retrieve_measure(depth_mat, sl.MEASURE.DEPTH, sl.MEM.CPU)
                    if not result_ok(status, sl):
                        raise ExtractorError(f"深度の取得に失敗しました: {status}")
                    depth_data = depth_mat.get_data()
                if outputs["depth"]["enabled"]:
                    save_depth(output_dir / row["depth_path"], depth_data, outputs["depth"])
                if outputs["depth_preview"]["enabled"]:
                    save_depth_preview(output_dir / row["depth_preview_path"], depth_data, outputs["depth"])

                timestamp = camera.get_timestamp(sl.TIME_REFERENCE.IMAGE)
                if outputs["left"]["enabled"]:
                    size_mat = left_mat
                elif outputs["right"]["enabled"]:
                    size_mat = right_mat
                else:
                    size_mat = depth_mat
                row.update({
                    "actual_frame_index": str(actual),
                    "timestamp_ns": str(timestamp.get_nanoseconds()),
                    "width": str(int(size_mat.get_width())),
                    "height": str(int(size_mat.get_height())),
                    "status": "completed",
                    "error": "",
                })
                completed += 1
            except Exception as exc:  # Preserve per-frame failures and continue.
                row["status"] = "failed"
                row["error"] = str(exc).replace("\n", " ")[:1000]
                failed += 1
                print(f"  FAILED frame={target}: {row['error']}", file=sys.stderr)
            finally:
                write_manifest(manifest_path, rows)
                progress.update(progress_index, target, completed, failed)
    finally:
        camera.close()
    return completed, failed


def verify_row(output_dir: Path, row: dict[str, str]) -> str | None:
    from PIL import Image
    import numpy as np

    sizes: list[tuple[int, int]] = []
    for key in ("left_path", "right_path", "depth_preview_path"):
        relative = row.get(key, "")
        if not relative:
            continue
        path = output_dir / relative
        if not path.is_file() or path.stat().st_size == 0:
            return f"欠損: {relative}"
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                sizes.append(image.size)
        except Exception as exc:
            return f"破損: {relative}: {exc}"
    relative = row.get("depth_path", "")
    if relative:
        path = output_dir / relative
        if not path.is_file() or path.stat().st_size == 0:
            return f"欠損: {relative}"
        try:
            if path.suffix.lower() == ".npy":
                array = np.load(path, mmap_mode="r", allow_pickle=False)
                sizes.append((int(array.shape[1]), int(array.shape[0])))
            else:
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    sizes.append(image.size)
        except Exception as exc:
            return f"破損: {relative}: {exc}"
    if sizes and len(set(sizes)) != 1:
        return f"解像度不一致: {sizes}"
    return None


def verify_output(output_dir: Path) -> tuple[int, list[str]]:
    rows = read_manifest(output_dir / "manifest.csv")
    errors = []
    for row in rows:
        error = verify_row(output_dir, row)
        if error:
            errors.append(f"{row['image_id']}: {error}")
    return len(rows), errors


def resolve_root(config: dict[str, Any]) -> Path:
    return Path(config["input"]["root"]).expanduser().resolve()


def command_inspect(args: argparse.Namespace) -> int:
    config = deepcopy(DEFAULT_CONFIG)
    root = Path(args.root).expanduser().resolve()
    pattern = args.pattern or config["input"]["pattern"]
    svos = discover_svos(root, pattern)
    if not svos:
        raise ExtractorError(f"SVO2が見つかりません: root={root}, pattern={pattern}")
    print("SVO2\tframes\tfps\tduration_s\tresolution\tsize_GiB\tcamera")
    for svo in svos:
        metadata = svo_metadata(svo, config)
        duration = metadata["frame_count"] / metadata["fps"]
        print(
            f"{os.path.relpath(svo, root)}\t{metadata['frame_count']}\t{metadata['fps']:g}\t"
            f"{duration:.1f}\t{metadata['width']}x{metadata['height']}\t"
            f"{metadata['size_bytes'] / 2**30:.2f}\t{metadata['camera_model']}"
        )
    return 0


def command_plan(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    root = resolve_root(config)
    svos = discover_svos(root, config["input"]["pattern"])
    if not svos:
        raise ExtractorError(f"SVO2が見つかりません: {root}")
    total = 0
    for svo in svos:
        output_dir, count = plan_one(svo, root, config)
        total += count
        print(f"PLAN {os.path.relpath(svo, root)} -> {output_dir} ({count} frames)")
    print(f"合計: {len(svos)} SVO2, {total} frames")
    return 0


def command_extract(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    if args.resume:
        config["export"]["existing"] = "resume"
    root = resolve_root(config)
    svos = discover_svos(root, config["input"]["pattern"])
    if not svos:
        raise ExtractorError(f"SVO2が見つかりません: {root}")
    if args.plan_if_missing:
        for svo in svos:
            manifest = output_dir_for(svo, config["export"]["name"]) / "manifest.csv"
            if not manifest.exists():
                plan_one(svo, root, config)
    total_completed = total_failed = 0
    for svo in svos:
        print(f"EXTRACT {os.path.relpath(svo, root)}")
        completed, failed = extract_one(svo, config)
        total_completed += completed
        total_failed += failed
        print(f"  completed={completed}, failed={failed}")
    print(f"合計: completed={total_completed}, failed={total_failed}")
    return 1 if total_failed else 0


def command_verify(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).expanduser().resolve()
    count, errors = verify_output(output_dir)
    for error in errors:
        print(f"ERROR {error}")
    print(f"検査: {count} entries, 正常={count - len(errors)}, 異常={len(errors)}")
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ZED SVO2から一部フレームを抽出します")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="SVO2の情報を表示")
    inspect_parser.add_argument("root", nargs="?", default=".")
    inspect_parser.add_argument("--pattern", default=None)
    inspect_parser.set_defaults(handler=command_inspect)

    plan_parser = subparsers.add_parser("plan", help="抽出対象を決めてmanifestを作成")
    plan_parser.add_argument("--config", required=True)
    plan_parser.set_defaults(handler=command_plan)

    extract_parser = subparsers.add_parser("extract", help="manifestに従って抽出")
    extract_parser.add_argument("--config", required=True)
    extract_parser.add_argument("--resume", action="store_true")
    extract_parser.add_argument("--plan-if-missing", action="store_true")
    extract_parser.set_defaults(handler=command_extract)

    verify_parser = subparsers.add_parser("verify", help="抽出結果を検査")
    verify_parser.add_argument("output_dir")
    verify_parser.set_defaults(handler=command_verify)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except ExtractorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("中断しました。--resumeで再開できます。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
