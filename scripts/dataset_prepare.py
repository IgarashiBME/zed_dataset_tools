#!/usr/bin/env python3
"""Review extracted ZED frames and build reproducible dataset releases."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import shutil
import sys
import threading
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse


BUILDER_VERSION = 1

DEFAULT_CONFIG: dict[str, Any] = {
    "input": {
        "root": "../20260611-12Ehime_images",
        "manifest_pattern": "*_exports/*/manifest.csv",
        "required_modalities": ["left", "right", "depth", "depth_preview"],
    },
    "output": {
        "root": "../20260611-12Ehime_datasets/nakaaze",
        "review_dir": "review",
        "version": "v1",
    },
    "selection": {
        "scope": "per_session",
        "increments": [10, 30, 60, 100],
        "seed": 42,
    },
    "materialize": {
        "mode": "hardlink",
        "fallback_to_copy": True,
    },
    "labels": {
        "format": "yolo_segmentation",
        "extension": "txt",
        "source_root": None,
        "required": False,
        "target_view": "left",
        "classes": ["nakaaze"],
    },
    "review": {
        "host": "127.0.0.1",
        "port": 8765,
        "page_size": 24,
    },
}

CANDIDATE_FIELDS = [
    "candidate_order",
    "image_id",
    "session_id",
    "source_manifest",
    "source_svo",
    "requested_frame_index",
    "actual_frame_index",
    "timestamp_ns",
    "left_source",
    "right_source",
    "depth_source",
    "depth_preview_source",
    "width",
    "height",
]

REVIEW_FIELDS = [
    "image_id",
    "decision",
    "reject_reason",
    "note",
    "reviewer",
    "reviewed_at",
]

SELECTION_FIELDS = ["sample_order", "image_id", "session_id", "increment_group"]

DATASET_FIELDS = [
    "sample_order",
    "image_id",
    "session_id",
    "increment_group",
    "source_manifest",
    "source_svo",
    "requested_frame_index",
    "actual_frame_index",
    "timestamp_ns",
    "left_path",
    "right_path",
    "depth_path",
    "depth_preview_path",
    "label_path",
    "label_status",
    "width",
    "height",
    "review_decision",
]

DECISIONS = {"", "keep", "reject", "hold"}
REJECT_REASONS = {
    "",
    "blur",
    "too_dark",
    "overexposed",
    "no_target",
    "occlusion",
    "duplicate",
    "depth_invalid",
    "other",
}
MODALITY_TO_SOURCE = {
    "left": "left_source",
    "right": "right_source",
    "depth": "depth_source",
    "depth_preview": "depth_preview_source",
}
DISPLAY_MODALITIES = ("left", "right", "depth_preview")


class DatasetError(RuntimeError):
    """Expected user-facing dataset preparation error."""


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
        raise DatasetError("PyYAMLが必要です: pip install PyYAML") from exc
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        raise DatasetError(f"設定ファイルを読み込めません: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DatasetError("設定ファイルのルートはYAMLマッピングである必要があります")
    config = deep_merge(DEFAULT_CONFIG, raw)
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    if config["selection"].get("scope") != "per_session":
        raise DatasetError("selection.scopeはper_sessionにしてください")
    increments = config["selection"]["increments"]
    if not isinstance(increments, list) or not increments:
        raise DatasetError("selection.incrementsには1つ以上の枚数を指定してください")
    try:
        increment_values = [int(value) for value in increments]
    except (TypeError, ValueError) as exc:
        raise DatasetError("selection.incrementsは整数で指定してください") from exc
    if any(value < 1 for value in increment_values):
        raise DatasetError("selection.incrementsはすべて1以上にしてください")
    if len(set(increment_values)) != len(increments):
        raise DatasetError("selection.incrementsに重複があります")
    modalities = config["input"]["required_modalities"]
    if not isinstance(modalities, list) or "left" not in modalities:
        raise DatasetError("input.required_modalitiesにはleftが必要です")
    unknown = set(modalities) - set(MODALITY_TO_SOURCE)
    if unknown:
        raise DatasetError(f"未対応のrequired_modalitiesです: {sorted(unknown)}")
    if config["materialize"]["mode"] not in {"hardlink", "copy"}:
        raise DatasetError("materialize.modeはhardlinkまたはcopyにしてください")
    extension = str(config["labels"]["extension"]).lstrip(".")
    if not extension or "/" in extension or "\\" in extension:
        raise DatasetError("labels.extensionが不正です")
    version = str(config["output"]["version"])
    if not version or version in {".", ".."} or "/" in version or "\\" in version:
        raise DatasetError("output.versionには単一のディレクトリ名を指定してください")
    review_name = str(config["output"]["review_dir"])
    if not review_name or review_name in {".", ".."} or "/" in review_name or "\\" in review_name:
        raise DatasetError("output.review_dirには単一のディレクトリ名を指定してください")
    try:
        port = int(config["review"]["port"])
        page_size = int(config["review"]["page_size"])
    except (TypeError, ValueError) as exc:
        raise DatasetError("review.portとreview.page_sizeは整数で指定してください") from exc
    if port not in range(1, 65536):
        raise DatasetError("review.portは1～65535にしてください")
    if page_size not in range(1, 101):
        raise DatasetError("review.page_sizeは1～100にしてください")


def input_root(config: dict[str, Any]) -> Path:
    return Path(config["input"]["root"]).expanduser().resolve()


def output_root(config: dict[str, Any]) -> Path:
    return Path(config["output"]["root"]).expanduser().resolve()


def review_dir(config: dict[str, Any]) -> Path:
    return output_root(config) / str(config["output"]["review_dir"])


def dataset_dir(config: dict[str, Any]) -> Path:
    return output_root(config) / str(config["output"]["version"])


def atomic_write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def read_csv(path: Path, expected_fields: list[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != expected_fields:
                raise DatasetError(f"CSVの列が期待値と異なります: {path}")
            return list(reader)
    except OSError as exc:
        raise DatasetError(f"CSVを読み込めません: {path}: {exc}") from exc


def relative_path(path: Path, base: Path) -> str:
    return os.path.relpath(path.resolve(), base.resolve())


def resolve_recorded_path(value: str, base: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def stable_seed(seed: int, namespace: str) -> int:
    digest = hashlib.sha256(f"{seed}:{namespace}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def session_id_for(row: dict[str, str]) -> str:
    image_id = row.get("image_id", "")
    if "_" not in image_id or Path(image_id).name != image_id:
        raise DatasetError(f"image_idからセッションIDを取得できません: {image_id}")
    return image_id.rsplit("_", 1)[0]


def discover_manifests(config: dict[str, Any]) -> list[Path]:
    root = input_root(config)
    pattern = str(config["input"]["manifest_pattern"])
    return sorted(path.resolve() for path in root.glob(pattern) if path.is_file())


def load_extractor_manifest(path: Path) -> list[dict[str, str]]:
    required = {
        "image_id", "source_svo", "requested_frame_index", "actual_frame_index",
        "timestamp_ns", "left_path", "right_path", "depth_path",
        "depth_preview_path", "width", "height", "status",
    }
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = set(reader.fieldnames or [])
            missing = required - fields
            if missing:
                raise DatasetError(f"抽出manifestに必要な列がありません: {path}: {sorted(missing)}")
            return list(reader)
    except OSError as exc:
        raise DatasetError(f"抽出manifestを読み込めません: {path}: {exc}") from exc


def interleave_by_session(rows: list[dict[str, str]], seed: int) -> list[dict[str, str]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row["session_id"]].append(row)
    sessions = sorted(groups)
    random.Random(stable_seed(seed, "session-order")).shuffle(sessions)
    for session in sessions:
        random.Random(stable_seed(seed, f"session:{session}")).shuffle(groups[session])
    ordered: list[dict[str, str]] = []
    index = 0
    while True:
        added = False
        for session in sessions:
            if index < len(groups[session]):
                ordered.append(groups[session][index])
                added = True
        if not added:
            break
        index += 1
    return ordered


def collect_candidates(config: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, int]]:
    manifests = discover_manifests(config)
    if not manifests:
        raise DatasetError(
            f"抽出manifestが見つかりません: root={input_root(config)}, "
            f"pattern={config['input']['manifest_pattern']}"
        )
    destination = review_dir(config)
    required_modalities = list(config["input"]["required_modalities"])
    candidates_by_id: dict[str, dict[str, str]] = {}
    stats = {"manifests": len(manifests), "rows": 0, "incomplete": 0, "missing": 0, "duplicates": 0}
    for manifest in manifests:
        for source in load_extractor_manifest(manifest):
            stats["rows"] += 1
            if source["status"] != "completed":
                stats["incomplete"] += 1
                continue
            sources: dict[str, Path | None] = {}
            missing = False
            for modality, source_field in MODALITY_TO_SOURCE.items():
                manifest_field = f"{modality}_path"
                value = source.get(manifest_field, "")
                path = (manifest.parent / value).resolve() if value else None
                sources[source_field] = path
                if modality in required_modalities and (
                    path is None or not path.is_file() or path.stat().st_size == 0
                ):
                    missing = True
            if missing:
                stats["missing"] += 1
                continue
            image_id = source["image_id"]
            source_svo = resolve_recorded_path(source["source_svo"], manifest.parent)
            candidate = {
                "candidate_order": "",
                "image_id": image_id,
                "session_id": session_id_for(source),
                "source_manifest": relative_path(manifest, destination),
                "source_svo": relative_path(source_svo, destination),
                "requested_frame_index": source["requested_frame_index"],
                "actual_frame_index": source["actual_frame_index"],
                "timestamp_ns": source["timestamp_ns"],
                "left_source": relative_path(sources["left_source"], destination) if sources["left_source"] else "",
                "right_source": relative_path(sources["right_source"], destination) if sources["right_source"] else "",
                "depth_source": relative_path(sources["depth_source"], destination) if sources["depth_source"] else "",
                "depth_preview_source": relative_path(sources["depth_preview_source"], destination) if sources["depth_preview_source"] else "",
                "width": source["width"],
                "height": source["height"],
            }
            if image_id in candidates_by_id:
                stats["duplicates"] += 1
                continue
            candidates_by_id[image_id] = candidate
    candidates = interleave_by_session(
        list(candidates_by_id.values()), int(config["selection"]["seed"])
    )
    for index, candidate in enumerate(candidates, 1):
        candidate["candidate_order"] = str(index)
    return candidates, stats


def empty_review(image_id: str) -> dict[str, str]:
    return {field: image_id if field == "image_id" else "" for field in REVIEW_FIELDS}


def plan_review(config: dict[str, Any], overwrite: bool = False) -> tuple[int, dict[str, int]]:
    directory = review_dir(config)
    candidates_path = directory / "candidates.csv"
    reviews_path = directory / "review.csv"
    if candidates_path.exists() and not overwrite:
        raise DatasetError(f"既存のレビュー計画があります: {candidates_path}")
    existing_reviews: dict[str, dict[str, str]] = {}
    if reviews_path.exists():
        existing_reviews = {row["image_id"]: row for row in read_csv(reviews_path, REVIEW_FIELDS)}
    candidates, stats = collect_candidates(config)
    if not candidates:
        raise DatasetError("使用可能な候補画像がありません")
    reviews = [existing_reviews.get(row["image_id"], empty_review(row["image_id"])) for row in candidates]
    atomic_write_csv(candidates_path, CANDIDATE_FIELDS, candidates)
    atomic_write_csv(reviews_path, REVIEW_FIELDS, reviews)
    write_yaml(directory / "review_config.yaml", config_snapshot(config, "review-plan"))
    return len(candidates), stats


def read_review_workspace(config: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    directory = review_dir(config)
    candidates_path = directory / "candidates.csv"
    reviews_path = directory / "review.csv"
    if not candidates_path.exists() or not reviews_path.exists():
        raise DatasetError("先にplanを実行してください")
    return read_csv(candidates_path, CANDIDATE_FIELDS), read_csv(reviews_path, REVIEW_FIELDS)


def review_counts(reviews: Iterable[dict[str, str]]) -> dict[str, int]:
    counts = {"unreviewed": 0, "keep": 0, "reject": 0, "hold": 0}
    for row in reviews:
        decision = row["decision"] or "unreviewed"
        counts[decision] = counts.get(decision, 0) + 1
    return counts


def session_keep_counts(
    candidates: Iterable[dict[str, str]], reviews: Iterable[dict[str, str]]
) -> dict[str, int]:
    session_by_id = {row["image_id"]: row["session_id"] for row in candidates}
    counts = {session: 0 for session in sorted(set(session_by_id.values()))}
    for review in reviews:
        if review["decision"] == "keep" and review["image_id"] in session_by_id:
            counts[session_by_id[review["image_id"]]] += 1
    return counts


def review_values(
    image_id: str, decision: str, reject_reason: str, note: str, reviewer: str
) -> dict[str, str]:
    if decision == "reject" and not reject_reason:
        reject_reason = "other"
    if decision not in DECISIONS:
        raise DatasetError(f"未対応のdecisionです: {decision}")
    if reject_reason not in REJECT_REASONS:
        raise DatasetError(f"未対応のreject_reasonです: {reject_reason}")
    if decision != "reject" and reject_reason:
        raise DatasetError("reject以外にはreject_reasonを指定できません")
    return {
        "image_id": image_id,
        "decision": decision,
        "reject_reason": reject_reason,
        "note": note.replace("\r", " ").replace("\n", " ")[:1000],
        "reviewer": reviewer[:200],
        "reviewed_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def update_review(
    config: dict[str, Any], image_id: str, decision: str, reject_reason: str,
    note: str, reviewer: str,
) -> dict[str, str]:
    values = review_values(image_id, decision, reject_reason, note, reviewer)
    candidates, reviews = read_review_workspace(config)
    candidate_ids = {row["image_id"] for row in candidates}
    if image_id not in candidate_ids:
        raise DatasetError(f"候補にないimage_idです: {image_id}")
    found = False
    for row in reviews:
        if row["image_id"] == image_id:
            row.update(values)
            found = True
            updated = dict(row)
            break
    if not found:
        raise DatasetError(f"review.csvにimage_idがありません: {image_id}")
    atomic_write_csv(review_dir(config) / "review.csv", REVIEW_FIELDS, reviews)
    return updated


def create_selection(config: dict[str, Any], overwrite: bool = False) -> list[dict[str, str]]:
    directory = review_dir(config)
    selection_path = directory / "selection.csv"
    if selection_path.exists() and not overwrite:
        raise DatasetError(f"既存の選択結果があります: {selection_path}")
    candidates, reviews = read_review_workspace(config)
    decisions = {row["image_id"]: row["decision"] for row in reviews}
    increments = [int(value) for value in config["selection"]["increments"]]
    required_per_session = sum(increments)
    seed = int(config["selection"]["seed"])
    sessions = sorted({row["session_id"] for row in candidates})
    random.Random(stable_seed(seed, "selection-session-order")).shuffle(sessions)
    kept_by_session: dict[str, list[dict[str, str]]] = defaultdict(list)
    for candidate in candidates:
        if decisions.get(candidate["image_id"]) == "keep":
            kept_by_session[candidate["session_id"]].append(candidate)
    shortages = [
        (session, len(kept_by_session[session]))
        for session in sessions
        if len(kept_by_session[session]) < required_per_session
    ]
    if shortages:
        detail = ", ".join(
            f"{session}={count}/{required_per_session}" for session, count in shortages[:10]
        )
        suffix = f" ほか{len(shortages) - 10}セッション" if len(shortages) > 10 else ""
        raise DatasetError(f"セッションごとのkeepが不足しています: {detail}{suffix}")
    selected_by_session: dict[str, list[dict[str, str]]] = {}
    for session in sessions:
        values = list(kept_by_session[session])
        random.Random(stable_seed(seed, f"selection:{session}")).shuffle(values)
        selected_by_session[session] = values[:required_per_session]
    rows: list[dict[str, str]] = []
    cursor = 0
    for increment in increments:
        group = f"add_{increment:04d}"
        # Interleave sessions so every increment is balanced throughout its list.
        for offset in range(increment):
            for session in sessions:
                candidate = selected_by_session[session][cursor + offset]
                rows.append({
                    "sample_order": str(len(rows) + 1),
                    "image_id": candidate["image_id"],
                    "session_id": session,
                    "increment_group": group,
                })
        cursor += increment
    atomic_write_csv(selection_path, SELECTION_FIELDS, rows)
    return rows


def write_yaml(path: Path, value: dict[str, Any]) -> None:
    try:
        import yaml
    except ImportError as exc:
        raise DatasetError("PyYAMLが必要です: pip install PyYAML") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    os.replace(temporary, path)


def config_snapshot(config: dict[str, Any], action: str) -> dict[str, Any]:
    snapshot = deepcopy(config)
    snapshot["dataset_builder"] = {
        "version": BUILDER_VERSION,
        "action": action,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    return snapshot


def materialize_file(source: Path, destination: Path, config: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise DatasetError(f"出力ファイルが既に存在します: {destination}")
    mode = config["materialize"]["mode"]
    if mode == "copy":
        shutil.copy2(source, destination)
        return
    try:
        os.link(source, destination)
    except OSError:
        if not bool(config["materialize"]["fallback_to_copy"]):
            raise
        shutil.copy2(source, destination)


def label_source_for(image_id: str, config: dict[str, Any]) -> Path | None:
    source_root = config["labels"].get("source_root")
    if source_root in {None, ""}:
        return None
    extension = str(config["labels"]["extension"]).lstrip(".")
    return Path(source_root).expanduser().resolve() / f"{image_id}.{extension}"


def build_dataset(config: dict[str, Any]) -> tuple[Path, int]:
    destination = dataset_dir(config)
    if destination.exists():
        raise DatasetError(
            f"出力バージョンが既に存在します。別のoutput.versionを指定してください: {destination}"
        )
    candidates, reviews = read_review_workspace(config)
    selection_path = review_dir(config) / "selection.csv"
    if not selection_path.exists():
        raise DatasetError("先にselectを実行してください")
    selections = read_csv(selection_path, SELECTION_FIELDS)
    candidate_by_id = {row["image_id"]: row for row in candidates}
    decision_by_id = {row["image_id"]: row["decision"] for row in reviews}
    session_count = len({row["session_id"] for row in candidates})
    required = sum(int(value) for value in config["selection"]["increments"]) * session_count
    if len(selections) != required:
        raise DatasetError(f"selection.csvの件数が不正です: expected={required}, actual={len(selections)}")
    selection_counts: dict[tuple[str, str], int] = defaultdict(int)
    for selected in selections:
        selection_counts[(selected["session_id"], selected["increment_group"])] += 1
    candidate_sessions = sorted({row["session_id"] for row in candidates})
    for session in candidate_sessions:
        for increment in config["selection"]["increments"]:
            group = f"add_{int(increment):04d}"
            actual = selection_counts[(session, group)]
            if actual != int(increment):
                raise DatasetError(
                    f"selection.csvのセッション別件数が不正です: "
                    f"{session}: {group}: expected={increment}, actual={actual}"
                )
    staging = destination.with_name(f".{destination.name}.building")
    if staging.exists():
        raise DatasetError(f"前回の構築途中ディレクトリがあります: {staging}")
    # Validate all selected inputs before creating output files.
    for selected in selections:
        image_id = selected["image_id"]
        candidate = candidate_by_id.get(image_id)
        if candidate is None or decision_by_id.get(image_id) != "keep":
            raise DatasetError(f"選択画像がkeep状態ではありません: {image_id}")
        if selected["session_id"] != candidate["session_id"]:
            raise DatasetError(f"選択画像のセッションIDが一致しません: {image_id}")
        for source_field in MODALITY_TO_SOURCE.values():
            source_value = candidate[source_field]
            if source_value and not resolve_recorded_path(source_value, review_dir(config)).is_file():
                raise DatasetError(f"選択画像の元ファイルがありません: {image_id}: {source_value}")
        label_source = label_source_for(image_id, config)
        if bool(config["labels"]["required"]) and (
            label_source is None or not label_source.is_file()
        ):
            raise DatasetError(f"必須ラベルがありません: {image_id}")
    staging.mkdir(parents=True)
    for name in ("left", "right", "depth", "depth_preview", "labels", "subsets"):
        (staging / name).mkdir()
    manifest_rows: list[dict[str, str]] = []
    try:
        for selected in selections:
            image_id = selected["image_id"]
            candidate = candidate_by_id.get(image_id)
            if candidate is None or decision_by_id.get(image_id) != "keep":
                raise DatasetError(f"選択画像がkeep状態ではありません: {image_id}")
            output_paths: dict[str, str] = {}
            for modality, source_field in MODALITY_TO_SOURCE.items():
                source_value = candidate[source_field]
                if not source_value:
                    output_paths[f"{modality}_path"] = ""
                    continue
                source = resolve_recorded_path(source_value, review_dir(config))
                relative = f"{modality}/{image_id}{source.suffix.lower()}"
                materialize_file(source, staging / relative, config)
                output_paths[f"{modality}_path"] = relative
            label_source = label_source_for(image_id, config)
            label_path = ""
            label_status = "unlabeled"
            if label_source is not None and label_source.is_file():
                extension = str(config["labels"]["extension"]).lstrip(".")
                label_path = f"labels/{image_id}.{extension}"
                materialize_file(label_source, staging / label_path, config)
                label_status = "completed"
            elif bool(config["labels"]["required"]):
                raise DatasetError(f"必須ラベルがありません: {image_id}")
            manifest_rows.append({
                "sample_order": selected["sample_order"],
                "image_id": image_id,
                "session_id": candidate["session_id"],
                "increment_group": selected["increment_group"],
                "source_manifest": relative_path(
                    resolve_recorded_path(candidate["source_manifest"], review_dir(config)),
                    destination,
                ),
                "source_svo": relative_path(
                    resolve_recorded_path(candidate["source_svo"], review_dir(config)),
                    destination,
                ),
                "requested_frame_index": candidate["requested_frame_index"],
                "actual_frame_index": candidate["actual_frame_index"],
                "timestamp_ns": candidate["timestamp_ns"],
                **output_paths,
                "label_path": label_path,
                "label_status": label_status,
                "width": candidate["width"],
                "height": candidate["height"],
                "review_decision": "keep",
            })
        atomic_write_csv(staging / "manifest.csv", DATASET_FIELDS, manifest_rows)
        write_subsets(staging / "subsets", manifest_rows, config)
        write_yaml(staging / "dataset.yaml", config_snapshot(config, "build"))
        os.replace(staging, destination)
    except Exception:
        # Keep a partial directory for diagnosis; a new version name is required for retry.
        raise
    return destination, len(manifest_rows)


def write_id_list(path: Path, image_ids: Iterable[str]) -> None:
    values = list(image_ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")


def write_subsets(directory: Path, rows: list[dict[str, str]], config: dict[str, Any]) -> None:
    increments = [int(value) for value in config["selection"]["increments"]]
    sessions = sorted({row["session_id"] for row in rows})
    cumulative_all: list[str] = []
    cumulative_by_session: dict[str, list[str]] = {session: [] for session in sessions}
    cumulative_count = 0
    for increment in increments:
        group = f"add_{increment:04d}"
        values = [row["image_id"] for row in rows if row["increment_group"] == group]
        if len(values) != increment * len(sessions):
            raise DatasetError(f"増分グループの件数が不正です: {group}")
        write_id_list(directory / f"{group}.txt", values)
        cumulative_all.extend(values)
        cumulative_count += increment
        write_id_list(directory / f"dataset_{cumulative_count:04d}.txt", cumulative_all)
        for session in sessions:
            session_values = [
                row["image_id"] for row in rows
                if row["increment_group"] == group and row["session_id"] == session
            ]
            if len(session_values) != increment:
                raise DatasetError(f"セッション増分の件数が不正です: {session}: {group}")
            session_directory = directory / "by_session" / session
            write_id_list(session_directory / f"{group}.txt", session_values)
            cumulative_by_session[session].extend(session_values)
            write_id_list(
                session_directory / f"dataset_{cumulative_count:04d}.txt",
                cumulative_by_session[session],
            )


def read_id_list(path: Path) -> list[str]:
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        raise DatasetError(f"subsetを読み込めません: {path}: {exc}") from exc


def verify_dataset(path: Path) -> tuple[int, list[str]]:
    errors: list[str] = []
    manifest_path = path / "manifest.csv"
    if not manifest_path.exists():
        return 0, [f"欠損: {manifest_path}"]
    rows = read_csv(manifest_path, DATASET_FIELDS)
    ids = [row["image_id"] for row in rows]
    if len(ids) != len(set(ids)):
        errors.append("manifest.csvにimage_idの重複があります")
    for row in rows:
        for field in ("left_path", "right_path", "depth_path", "depth_preview_path"):
            value = row[field]
            if value and not (path / value).is_file():
                errors.append(f"{row['image_id']}: 欠損: {value}")
        label_path = row["label_path"]
        if row["label_status"] == "completed" and (
            not label_path or not (path / label_path).is_file()
        ):
            errors.append(f"{row['image_id']}: completedラベルがありません")
    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        groups[row["increment_group"]].append(row["image_id"])
    sessions = sorted({row["session_id"] for row in rows})
    seen: set[str] = set()
    cumulative: list[str] = []
    cumulative_by_session: dict[str, list[str]] = {session: [] for session in sessions}
    cumulative_count = 0
    for group in sorted(groups, key=lambda value: min(int(row["sample_order"]) for row in rows if row["increment_group"] == value)):
        add_path = path / "subsets" / f"{group}.txt"
        if not add_path.is_file():
            errors.append(f"欠損: subsets/{group}.txt")
            continue
        values = read_id_list(add_path)
        try:
            expected_count = int(group.removeprefix("add_"))
        except ValueError:
            expected_count = -1
        if not group.startswith("add_") or len(values) != expected_count * len(sessions):
            errors.append(f"{group}の名称と件数が一致しません")
        if values != groups[group]:
            errors.append(f"subsets/{group}.txtがmanifestと一致しません")
        overlap = seen.intersection(values)
        if overlap:
            errors.append(f"{group}に他の増分との重複があります")
        seen.update(values)
        cumulative.extend(values)
        cumulative_count += expected_count
        cumulative_path = path / "subsets" / f"dataset_{cumulative_count:04d}.txt"
        if not cumulative_path.is_file() or read_id_list(cumulative_path) != cumulative:
            errors.append(f"累積subsetが不正です: {cumulative_path.name}")
        for session in sessions:
            session_values = [
                row["image_id"] for row in rows
                if row["increment_group"] == group and row["session_id"] == session
            ]
            session_path = path / "subsets" / "by_session" / session / f"{group}.txt"
            if len(session_values) != expected_count:
                errors.append(f"{session}: {group}の件数が不正です")
            if not session_path.is_file() or read_id_list(session_path) != session_values:
                errors.append(f"セッション別subsetが不正です: {session}: {group}")
            cumulative_by_session[session].extend(session_values)
            session_cumulative_path = (
                path / "subsets" / "by_session" / session
                / f"dataset_{cumulative_count:04d}.txt"
            )
            if (
                not session_cumulative_path.is_file()
                or read_id_list(session_cumulative_path) != cumulative_by_session[session]
            ):
                errors.append(
                    f"セッション別累積subsetが不正です: {session}: "
                    f"{session_cumulative_path.name}"
                )
    if set(ids) != seen:
        errors.append("subsetとmanifestのimage_id集合が一致しません")
    return len(rows), errors


class ReviewApplication:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.candidates, self.reviews = read_review_workspace(config)
        self.candidate_by_id = {row["image_id"]: row for row in self.candidates}
        self.review_by_id = {row["image_id"]: row for row in self.reviews}
        self.lock = threading.Lock()
        self.target_per_session = sum(
            int(value) for value in config["selection"]["increments"]
        )
        self.sessions = sorted({row["session_id"] for row in self.candidates})
        self.target = self.target_per_session * len(self.sessions)

    def state(
        self, offset: int, limit: int, decision: str, session: str = ""
    ) -> dict[str, Any]:
        items = []
        filtered_total = 0
        for candidate in self.candidates:
            if session and candidate["session_id"] != session:
                continue
            review = self.review_by_id[candidate["image_id"]]
            actual = review["decision"] or "unreviewed"
            if decision and actual != decision:
                continue
            if offset <= filtered_total < offset + limit:
                items.append({
                    "image_id": candidate["image_id"],
                    "candidate_order": int(candidate["candidate_order"]),
                    "session_id": candidate["session_id"],
                    "decision": review["decision"],
                    "reject_reason": review["reject_reason"],
                    "note": review["note"],
                    "reviewer": review["reviewer"],
                    "thumbnail": f"/thumbnail/{quote(candidate['image_id'])}",
                    "assets": {
                        modality: f"/asset/{modality}/{quote(candidate['image_id'])}"
                        for modality in DISPLAY_MODALITIES
                        if candidate[MODALITY_TO_SOURCE[modality]]
                    },
                })
            filtered_total += 1
        keep_by_session = session_keep_counts(
            self.candidates, self.review_by_id.values()
        )
        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "page_size": int(self.config["review"]["page_size"]),
            "filtered_total": filtered_total,
            "counts": review_counts(self.review_by_id.values()),
            "target": self.target,
            "target_per_session": self.target_per_session,
            "session_count": len(self.sessions),
            "sessions_achieved": sum(
                count >= self.target_per_session for count in keep_by_session.values()
            ),
            "keep_by_session": keep_by_session,
            "reject_reasons": sorted(reason for reason in REJECT_REASONS if reason),
        }

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            image_id = str(payload.get("image_id", ""))
            if image_id not in self.candidate_by_id:
                raise DatasetError(f"候補にないimage_idです: {image_id}")
            values = review_values(
                image_id,
                str(payload.get("decision", "")),
                str(payload.get("reject_reason", "")),
                str(payload.get("note", "")),
                str(payload.get("reviewer", "")),
            )
            self.review_by_id[image_id].update(values)
            atomic_write_csv(
                review_dir(self.config) / "review.csv", REVIEW_FIELDS, self.reviews
            )
            updated = dict(self.review_by_id[image_id])
            return {"review": updated, "counts": review_counts(self.review_by_id.values())}

    def asset(self, modality: str, image_id: str) -> Path | None:
        candidate = self.candidate_by_id.get(image_id)
        field = MODALITY_TO_SOURCE.get(modality)
        if candidate is None or field is None or not candidate[field]:
            return None
        return resolve_recorded_path(candidate[field], review_dir(self.config))

    def thumbnail(self, image_id: str) -> Path | None:
        candidate = self.candidate_by_id.get(image_id)
        if candidate is None or not candidate["left_source"]:
            return None
        destination = review_dir(self.config) / "thumbnails" / f"{image_id}.jpg"
        if destination.is_file():
            return destination
        try:
            from PIL import Image, ImageOps

            source = resolve_recorded_path(candidate["left_source"], review_dir(self.config))
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(
                f".{destination.name}.{threading.get_ident()}.tmp"
            )
            with Image.open(source) as original:
                preview = ImageOps.exif_transpose(original).convert("RGB")
                preview.thumbnail((480, 270))
                preview.save(temporary, format="JPEG", quality=85, optimize=True)
            os.replace(temporary, destination)
            return destination
        except (OSError, ValueError):
            return None


def make_review_handler(application: ReviewApplication, ui_root: Path):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def send_file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            payload = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                query = parse_qs(parsed.query)
                offset = max(0, int(query.get("offset", ["0"])[0]))
                default_limit = str(application.config["review"]["page_size"])
                limit = min(100, max(1, int(query.get("limit", [default_limit])[0])))
                decision = query.get("decision", [""])[0]
                session = query.get("session", [""])[0]
                self.send_json(application.state(offset, limit, decision, session))
                return
            if parsed.path.startswith("/asset/"):
                parts = parsed.path.split("/", 3)
                if len(parts) != 4:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                modality, image_id = parts[2], unquote(parts[3])
                asset = application.asset(modality, image_id)
                if asset is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_type = "image/jpeg" if asset.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
                self.send_file(asset, content_type)
                return
            if parsed.path.startswith("/thumbnail/"):
                image_id = unquote(parsed.path.removeprefix("/thumbnail/"))
                thumbnail = application.thumbnail(image_id)
                if thumbnail is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_file(thumbnail, "image/jpeg")
                return
            static_files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            if parsed.path in static_files:
                filename, content_type = static_files[parsed.path]
                self.send_file(ui_root / filename, content_type)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if urlparse(self.path).path != "/api/review":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self.send_json(application.save(payload))
            except (DatasetError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def log_message(self, format_string: str, *args: Any) -> None:
            print(f"review-ui: {format_string % args}")

    return Handler


def command_plan(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    count, stats = plan_review(config, overwrite=args.overwrite)
    print(f"レビュー計画: candidates={count}, manifests={stats['manifests']}")
    print(
        f"除外: incomplete={stats['incomplete']}, missing={stats['missing']}, "
        f"duplicate={stats['duplicates']}"
    )
    print(f"出力: {review_dir(config)}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    candidates, reviews = read_review_workspace(config)
    counts = review_counts(reviews)
    target = sum(int(value) for value in config["selection"]["increments"])
    keep_by_session = session_keep_counts(candidates, reviews)
    achieved = sum(count >= target for count in keep_by_session.values())
    print(
        f"候補={len(candidates)}, セッション={len(keep_by_session)}, "
        f"各セッション目標keep={target}, 達成={achieved}/{len(keep_by_session)}"
    )
    print(", ".join(f"{key}={value}" for key, value in counts.items()))
    shortages = [(session, count) for session, count in keep_by_session.items() if count < target]
    if shortages:
        print("未達セッション:")
        for session, count in shortages:
            print(f"  {session}: keep={count}, remaining={target - count}")
    return 0


def command_review(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    application = ReviewApplication(config)
    ui_root = Path(__file__).resolve().parent.parent / "review_ui"
    missing = [name for name in ("index.html", "app.js", "style.css") if not (ui_root / name).is_file()]
    if missing:
        raise DatasetError(f"レビューUIファイルがありません: {missing}")
    host = args.host or str(config["review"]["host"])
    port = args.port or int(config["review"]["port"])
    server = ThreadingHTTPServer((host, port), make_review_handler(application, ui_root))
    print(f"レビュー画面: http://{host}:{port}")
    print("終了するにはCtrl-Cを押してください")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def command_select(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    rows = create_selection(config, overwrite=args.overwrite)
    print(f"選択完了: {len(rows)} samples -> {review_dir(config) / 'selection.csv'}")
    return 0


def command_build(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    destination, count = build_dataset(config)
    print(f"データセット構築完了: {count} samples -> {destination}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    path = Path(args.dataset_dir).expanduser().resolve()
    count, errors = verify_dataset(path)
    for error in errors:
        print(f"ERROR {error}")
    print(f"検査: {count} samples, errors={len(errors)}")
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="抽出済みZED画像を選別してデータセットを構築します")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="候補を収集してレビュー順を固定")
    plan_parser.add_argument("--config", required=True)
    plan_parser.add_argument("--overwrite", action="store_true", help="判定を保持して候補一覧を再作成")
    plan_parser.set_defaults(handler=command_plan)

    status_parser = subparsers.add_parser("status", help="レビュー件数を表示")
    status_parser.add_argument("--config", required=True)
    status_parser.set_defaults(handler=command_status)

    review_parser = subparsers.add_parser("review", help="ローカルのレビュー画面を起動")
    review_parser.add_argument("--config", required=True)
    review_parser.add_argument("--host")
    review_parser.add_argument("--port", type=int)
    review_parser.set_defaults(handler=command_review)

    select_parser = subparsers.add_parser("select", help="keepから増分グループを決定")
    select_parser.add_argument("--config", required=True)
    select_parser.add_argument("--overwrite", action="store_true")
    select_parser.set_defaults(handler=command_select)

    build_command = subparsers.add_parser("build", help="選択結果をデータセットとして構築")
    build_command.add_argument("--config", required=True)
    build_command.set_defaults(handler=command_build)

    verify_parser = subparsers.add_parser("verify", help="完成データセットを検証")
    verify_parser.add_argument("dataset_dir")
    verify_parser.set_defaults(handler=command_verify)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except DatasetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("中断しました。保存済みのレビュー結果から再開できます。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
