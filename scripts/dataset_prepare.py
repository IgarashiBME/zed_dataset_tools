#!/usr/bin/env python3
"""Review extracted ZED frames and build reproducible dataset releases."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
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
        "roots": [
            "../20260611-12Ehime_images",
            "../20260611-12Ehime_images_outer",
        ],
        "manifest_pattern": "*_train/*/manifest.csv",
        "required_modalities": ["left", "right", "depth", "depth_preview"],
    },
    "output": {
        "root": "../ridge_data",
        "dataset_name": "dataset01_20260611_ehime",
        "review_dir": ".review",
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
    "yaml": {
        "validation": None,
        "test": None,
    },
    "review": {
        "host": "127.0.0.1",
        "port": 8765,
        "page_size": 24,
        "import_from": "../20260611-12Ehime_datasets/review",
    },
}

CANDIDATE_FIELDS = [
    "candidate_order",
    "image_id",
    "session_id",
    "site_id",
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

SELECTION_FIELDS = [
    "sample_order", "image_id", "session_id", "site_id", "increment_group"
]

SITE_MAP_FIELDS = ["site_id", "session_id"]

DATASET_FIELDS = [
    "sample_order",
    "image_id",
    "session_id",
    "site_id",
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
    roots = config["input"].get("roots")
    if not isinstance(roots, list) or not roots:
        raise DatasetError("input.rootsには1つ以上の入力ディレクトリを指定してください")
    if config["materialize"]["mode"] not in {"hardlink", "copy"}:
        raise DatasetError("materialize.modeはhardlinkまたはcopyにしてください")
    extension = str(config["labels"]["extension"]).lstrip(".")
    if not extension or "/" in extension or "\\" in extension:
        raise DatasetError("labels.extensionが不正です")
    dataset_name = str(config["output"]["dataset_name"])
    if (
        not dataset_name or dataset_name in {".", ".."}
        or "/" in dataset_name or "\\" in dataset_name
    ):
        raise DatasetError("output.dataset_nameには単一のディレクトリ名を指定してください")
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


def input_roots(config: dict[str, Any]) -> list[Path]:
    return [Path(value).expanduser().resolve() for value in config["input"]["roots"]]


def output_root(config: dict[str, Any]) -> Path:
    return Path(config["output"]["root"]).expanduser().resolve()


def review_dir(config: dict[str, Any]) -> Path:
    return (
        output_root(config) / str(config["output"]["review_dir"])
        / str(config["output"]["dataset_name"])
    )


def dataset_dir(config: dict[str, Any]) -> Path:
    return output_root(config) / str(config["output"]["dataset_name"])


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


def assign_site_ids(
    sessions: Iterable[str], existing_rows: Iterable[dict[str, str]] = ()
) -> list[dict[str, str]]:
    current_sessions = set(sessions)
    mapping: dict[str, str] = {}
    used_numbers: set[int] = set()
    used_site_ids: set[str] = set()
    for row in existing_rows:
        site_id = row["site_id"]
        session_id = row["session_id"]
        match = re.fullmatch(r"site(\d+)", site_id)
        if not match or session_id in mapping or site_id in used_site_ids:
            raise DatasetError("site_map.csvに不正または重複したsite割当があります")
        mapping[session_id] = site_id
        used_site_ids.add(site_id)
        used_numbers.add(int(match.group(1)))
    next_number = 1
    for session in sorted(current_sessions):
        if session in mapping:
            continue
        while next_number in used_numbers:
            next_number += 1
        site_id = f"site{next_number:02d}"
        mapping[session] = site_id
        used_site_ids.add(site_id)
        used_numbers.add(next_number)
    return sorted(
        (
            {"site_id": site_id, "session_id": session_id}
            for session_id, site_id in mapping.items()
            if session_id in current_sessions
        ),
        key=lambda row: int(row["site_id"].removeprefix("site")),
    )


def session_id_for(row: dict[str, str]) -> str:
    image_id = row.get("image_id", "")
    if "_" not in image_id or Path(image_id).name != image_id:
        raise DatasetError(f"image_idからセッションIDを取得できません: {image_id}")
    return image_id.rsplit("_", 1)[0]


def discover_manifests(config: dict[str, Any]) -> list[Path]:
    pattern = str(config["input"]["manifest_pattern"])
    paths = {
        path.resolve()
        for root in input_roots(config)
        for path in root.glob(pattern)
        if path.is_file()
    }
    return sorted(paths)


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
            f"抽出manifestが見つかりません: roots={input_roots(config)}, "
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
                "site_id": "",
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
    site_map_path = directory / "site_map.csv"
    if candidates_path.exists() and not overwrite:
        raise DatasetError(f"既存のレビュー計画があります: {candidates_path}")
    existing_reviews: dict[str, dict[str, str]] = {}
    import_value = config["review"].get("import_from")
    import_directory = (
        Path(import_value).expanduser().resolve() if import_value not in {None, ""} else None
    )
    review_source = reviews_path
    if not review_source.exists() and import_directory is not None:
        imported_reviews = import_directory / "review.csv"
        if imported_reviews.exists():
            review_source = imported_reviews
    if review_source.exists():
        existing_reviews = {
            row["image_id"]: row for row in read_csv(review_source, REVIEW_FIELDS)
        }
    candidates, stats = collect_candidates(config)
    if not candidates:
        raise DatasetError("使用可能な候補画像がありません")
    site_map_source = site_map_path
    if not site_map_source.exists() and import_directory is not None:
        imported_site_map = import_directory / "site_map.csv"
        if imported_site_map.exists():
            site_map_source = imported_site_map
    existing_site_rows = (
        read_csv(site_map_source, SITE_MAP_FIELDS) if site_map_source.exists() else []
    )
    site_rows = assign_site_ids(
        (row["session_id"] for row in candidates), existing_site_rows
    )
    site_by_session = {row["session_id"]: row["site_id"] for row in site_rows}
    for candidate in candidates:
        candidate["site_id"] = site_by_session[candidate["session_id"]]
    reviews = [existing_reviews.get(row["image_id"], empty_review(row["image_id"])) for row in candidates]
    stats["preserved_reviews"] = sum(bool(row["decision"]) for row in reviews)
    atomic_write_csv(candidates_path, CANDIDATE_FIELDS, candidates)
    atomic_write_csv(reviews_path, REVIEW_FIELDS, reviews)
    atomic_write_csv(site_map_path, SITE_MAP_FIELDS, site_rows)
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
                    "site_id": candidate["site_id"],
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


def increment_from_group(group: str) -> int:
    if not group.startswith("add_"):
        raise DatasetError(f"増分グループ名が不正です: {group}")
    try:
        value = int(group.removeprefix("add_"))
    except ValueError as exc:
        raise DatasetError(f"増分グループ名が不正です: {group}") from exc
    if value < 1:
        raise DatasetError(f"増分グループ名が不正です: {group}")
    return value


def site_group_dir(site_id: str, group: str) -> str:
    return f"{site_id}_add{increment_from_group(group):03d}"


def ordered_site_ids(rows: Iterable[dict[str, str]]) -> list[str]:
    return sorted(
        {row["site_id"] for row in rows},
        key=lambda value: int(value.removeprefix("site")),
    )


def write_dataset_yamls(
    directory: Path, rows: list[dict[str, str]], config: dict[str, Any]
) -> None:
    sites = ordered_site_ids(rows)
    increments = [int(value) for value in config["selection"]["increments"]]
    included: list[int] = []
    cumulative = 0
    names = {index: name for index, name in enumerate(config["labels"]["classes"])}
    for increment in increments:
        included.append(increment)
        cumulative += increment
        train = [
            f"images/{site_id}_add{value:03d}"
            for site_id in sites
            for value in included
        ]
        content: dict[str, Any] = {"path": "..", "train": train, "names": names}
        if config["yaml"].get("validation") is not None:
            content["val"] = config["yaml"]["validation"]
        if config["yaml"].get("test") is not None:
            content["test"] = config["yaml"]["test"]
        write_yaml(directory / f"dataset_n{cumulative:03d}.yaml", content)


def build_dataset(config: dict[str, Any]) -> tuple[Path, int]:
    destination = dataset_dir(config)
    if destination.exists():
        raise DatasetError(
            f"出力データセットが既に存在します。output.dataset_nameを変更してください: "
            f"{destination}"
        )
    candidates, reviews = read_review_workspace(config)
    workspace = review_dir(config)
    selection_path = workspace / "selection.csv"
    site_map_path = workspace / "site_map.csv"
    if not selection_path.exists():
        raise DatasetError("先にselectを実行してください")
    if not site_map_path.exists():
        raise DatasetError("site_map.csvがありません。planを再実行してください")
    selections = read_csv(selection_path, SELECTION_FIELDS)
    site_rows = read_csv(site_map_path, SITE_MAP_FIELDS)
    candidate_by_id = {row["image_id"]: row for row in candidates}
    decision_by_id = {row["image_id"]: row["decision"] for row in reviews}
    site_by_session = {row["session_id"]: row["site_id"] for row in site_rows}
    session_count = len(site_by_session)
    required = sum(int(value) for value in config["selection"]["increments"]) * session_count
    if len(selections) != required:
        raise DatasetError(
            f"selection.csvの件数が不正です: expected={required}, actual={len(selections)}"
        )
    selection_counts: dict[tuple[str, str], int] = defaultdict(int)
    for selected in selections:
        selection_counts[(selected["session_id"], selected["increment_group"])] += 1
    for session, site_id in site_by_session.items():
        for increment in config["selection"]["increments"]:
            group = f"add_{int(increment):04d}"
            actual = selection_counts[(session, group)]
            if actual != int(increment):
                raise DatasetError(
                    f"selection.csvのセッション別件数が不正です: "
                    f"{site_id}/{session}: {group}: expected={increment}, actual={actual}"
                )
    staging = destination.with_name(f".{destination.name}.building")
    if staging.exists():
        raise DatasetError(f"前回の構築途中ディレクトリがあります: {staging}")
    for selected in selections:
        image_id = selected["image_id"]
        candidate = candidate_by_id.get(image_id)
        if candidate is None or decision_by_id.get(image_id) != "keep":
            raise DatasetError(f"選択画像がkeep状態ではありません: {image_id}")
        if (
            selected["session_id"] != candidate["session_id"]
            or selected["site_id"] != candidate["site_id"]
            or site_by_session.get(candidate["session_id"]) != candidate["site_id"]
        ):
            raise DatasetError(f"選択画像のsiteまたはセッションが一致しません: {image_id}")
        for source_field in MODALITY_TO_SOURCE.values():
            source_value = candidate[source_field]
            if source_value and not resolve_recorded_path(source_value, workspace).is_file():
                raise DatasetError(f"選択画像の元ファイルがありません: {image_id}: {source_value}")
        label_source = label_source_for(image_id, config)
        if bool(config["labels"]["required"]) and (
            label_source is None or not label_source.is_file()
        ):
            raise DatasetError(f"必須ラベルがありません: {image_id}")

    staging.mkdir(parents=True)
    for name in ("images", "right", "depth", "depth_preview", "labels", "yaml", "metadata"):
        (staging / name).mkdir()
    for selected in selections:
        group_dir = site_group_dir(selected["site_id"], selected["increment_group"])
        for name in ("images", "right", "depth", "depth_preview", "labels"):
            (staging / name / group_dir).mkdir(parents=True, exist_ok=True)

    output_roots = {
        "left": "images", "right": "right", "depth": "depth",
        "depth_preview": "depth_preview",
    }
    manifest_rows: list[dict[str, str]] = []
    for selected in selections:
        image_id = selected["image_id"]
        candidate = candidate_by_id[image_id]
        group_dir = site_group_dir(candidate["site_id"], selected["increment_group"])
        output_paths: dict[str, str] = {}
        for modality, source_field in MODALITY_TO_SOURCE.items():
            source_value = candidate[source_field]
            if not source_value:
                output_paths[f"{modality}_path"] = ""
                continue
            source = resolve_recorded_path(source_value, workspace)
            relative = f"{output_roots[modality]}/{group_dir}/{image_id}{source.suffix.lower()}"
            materialize_file(source, staging / relative, config)
            output_paths[f"{modality}_path"] = relative
        label_source = label_source_for(image_id, config)
        label_path = ""
        label_status = "unlabeled"
        if label_source is not None and label_source.is_file():
            extension = str(config["labels"]["extension"]).lstrip(".")
            label_path = f"labels/{group_dir}/{image_id}.{extension}"
            materialize_file(label_source, staging / label_path, config)
            label_status = "completed"
        manifest_rows.append({
            "sample_order": selected["sample_order"],
            "image_id": image_id,
            "session_id": candidate["session_id"],
            "site_id": candidate["site_id"],
            "increment_group": selected["increment_group"],
            "source_manifest": relative_path(
                resolve_recorded_path(candidate["source_manifest"], workspace), destination
            ),
            "source_svo": relative_path(
                resolve_recorded_path(candidate["source_svo"], workspace), destination
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

    metadata = staging / "metadata"
    atomic_write_csv(metadata / "manifest.csv", DATASET_FIELDS, manifest_rows)
    atomic_write_csv(metadata / "selection.csv", SELECTION_FIELDS, selections)
    atomic_write_csv(metadata / "site_map.csv", SITE_MAP_FIELDS, site_rows)
    write_yaml(metadata / "dataset.yaml", config_snapshot(config, "build"))
    write_dataset_yamls(staging / "yaml", manifest_rows, config)
    os.replace(staging, destination)
    return destination, len(manifest_rows)


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        raise DatasetError(f"YAMLを読み込めません: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DatasetError(f"YAMLのルートがマッピングではありません: {path}")
    return value


def verify_dataset(path: Path) -> tuple[int, list[str]]:
    errors: list[str] = []
    manifest_path = path / "metadata" / "manifest.csv"
    if not manifest_path.exists():
        return 0, [f"欠損: {manifest_path}"]
    rows = read_csv(manifest_path, DATASET_FIELDS)
    ids = [row["image_id"] for row in rows]
    if len(ids) != len(set(ids)):
        errors.append("manifest.csvにimage_idの重複があります")
    for row in rows:
        expected_dir = site_group_dir(row["site_id"], row["increment_group"])
        expected_roots = {
            "left_path": "images", "right_path": "right", "depth_path": "depth",
            "depth_preview_path": "depth_preview",
        }
        for field, root in expected_roots.items():
            value = row[field]
            if value and not value.startswith(f"{root}/{expected_dir}/"):
                errors.append(f"{row['image_id']}: 配置先が不正です: {value}")
            if value and not (path / value).is_file():
                errors.append(f"{row['image_id']}: 欠損: {value}")
        label_path = row["label_path"]
        if label_path and not label_path.startswith(f"labels/{expected_dir}/"):
            errors.append(f"{row['image_id']}: ラベル配置先が不正です: {label_path}")
        if row["label_status"] == "completed" and (
            not label_path or not (path / label_path).is_file()
        ):
            errors.append(f"{row['image_id']}: completedラベルがありません")

    sites = ordered_site_ids(rows)
    groups = sorted(
        {row["increment_group"] for row in rows}, key=increment_from_group
    )
    cumulative_groups: list[str] = []
    cumulative_count = 0
    for group in groups:
        increment = increment_from_group(group)
        cumulative_groups.append(group)
        cumulative_count += increment
        for site_id in sites:
            actual = sum(
                row["site_id"] == site_id and row["increment_group"] == group
                for row in rows
            )
            if actual != increment:
                errors.append(
                    f"{site_id}: {group}の件数が不正です: expected={increment}, actual={actual}"
                )
            group_dir = site_group_dir(site_id, group)
            for root in ("images", "right", "depth", "depth_preview", "labels"):
                if not (path / root / group_dir).is_dir():
                    errors.append(f"ディレクトリ欠損: {root}/{group_dir}")
        yaml_path = path / "yaml" / f"dataset_n{cumulative_count:03d}.yaml"
        if not yaml_path.is_file():
            errors.append(f"YAML欠損: {yaml_path.name}")
            continue
        content = load_yaml(yaml_path)
        expected_train = [
            f"images/{site_id}_add{increment_from_group(value):03d}"
            for site_id in sites
            for value in cumulative_groups
        ]
        if content.get("path") != ".." or content.get("train") != expected_train:
            errors.append(f"YAMLのtrain参照が不正です: {yaml_path.name}")

    selection_path = path / "metadata" / "selection.csv"
    if not selection_path.is_file():
        errors.append("metadata欠損: selection.csv")
    else:
        selections = read_csv(selection_path, SELECTION_FIELDS)
        expected_selections = [
            {field: row[field] for field in SELECTION_FIELDS} for row in rows
        ]
        if selections != expected_selections:
            errors.append("metadata/selection.csvがmanifestと一致しません")
    site_map_path = path / "metadata" / "site_map.csv"
    if not site_map_path.is_file():
        errors.append("metadata欠損: site_map.csv")
    else:
        site_rows = read_csv(site_map_path, SITE_MAP_FIELDS)
        expected_sites = {
            (row["site_id"], row["session_id"]) for row in rows
        }
        actual_sites = {
            (row["site_id"], row["session_id"]) for row in site_rows
        }
        if actual_sites != expected_sites or len(site_rows) != len(actual_sites):
            errors.append("metadata/site_map.csvがmanifestと一致しません")
    if not (path / "metadata" / "dataset.yaml").is_file():
        errors.append("metadata欠損: dataset.yaml")
    return len(rows), errors


class ReviewApplication:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.candidates, self.reviews = read_review_workspace(config)
        self.candidate_by_id = {row["image_id"]: row for row in self.candidates}
        self.review_by_id = {row["image_id"]: row for row in self.reviews}
        self.site_by_session = {
            row["session_id"]: row["site_id"] for row in self.candidates
        }
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
                    "site_id": candidate["site_id"],
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
            "site_by_session": self.site_by_session,
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
    print(f"引継ぎ済みレビュー: {stats['preserved_reviews']}")
    print(f"出力: {review_dir(config)}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    candidates, reviews = read_review_workspace(config)
    counts = review_counts(reviews)
    target = sum(int(value) for value in config["selection"]["increments"])
    keep_by_session = session_keep_counts(candidates, reviews)
    site_by_session = {row["session_id"]: row["site_id"] for row in candidates}
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
            print(
                f"  {site_by_session[session]}/{session}: "
                f"keep={count}, remaining={target - count}"
            )
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
