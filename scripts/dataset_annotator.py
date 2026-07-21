#!/usr/bin/env python3
"""Annotate completed ZED datasets with Ultralytics YOLO segmentation labels."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    from scripts import dataset_viewer
except ModuleNotFoundError:  # Direct execution from scripts/.
    import dataset_viewer  # type: ignore[no-redef]


class AnnotatorError(RuntimeError):
    """Expected user-facing annotation error."""


ANNOTATION_STATUSES = {"unannotated", "positive", "empty"}
SORT_MODES = {"site_frame", "sample_order", "image_id"}
MAX_POLYGON_POINTS = 512
MAX_REQUEST_BYTES = 1_000_000


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _point(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise AnnotatorError(f"{label}は[x, y]形式にしてください")
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise AnnotatorError(f"{label}の座標が数値ではありません") from exc
    if not math.isfinite(x) or not math.isfinite(y):
        raise AnnotatorError(f"{label}の座標が有限値ではありません")
    if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
        raise AnnotatorError(f"{label}の座標は0～1にしてください")
    return [x, y]


def validate_polygon(value: Any) -> list[list[float]]:
    if not isinstance(value, list) or not 3 <= len(value) <= MAX_POLYGON_POINTS:
        raise AnnotatorError(f"ポリゴンは3～{MAX_POLYGON_POINTS}点にしてください")
    points = [_point(item, f"polygon[{index}]") for index, item in enumerate(value)]
    unique = {(round(point[0], 9), round(point[1], 9)) for point in points}
    if len(unique) < 3:
        raise AnnotatorError("ポリゴンには異なる3点以上が必要です")
    twice_area = 0.0
    for index, current in enumerate(points):
        following = points[(index + 1) % len(points)]
        twice_area += current[0] * following[1] - following[0] * current[1]
    if abs(twice_area) < 1e-6:
        raise AnnotatorError("ポリゴンの面積が小さすぎます")
    return points


def validate_edit_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnnotatorError("editはオブジェクトにしてください")
    mode = value.get("mode")
    if mode not in {"line", "curve"}:
        raise AnnotatorError("edit.modeはlineまたはcurveにしてください")

    def points_for(name: str, minimum: int, maximum: int) -> list[list[float]]:
        raw = value.get(name)
        if not isinstance(raw, list) or not minimum <= len(raw) <= maximum:
            raise AnnotatorError(f"edit.{name}は{minimum}～{maximum}点にしてください")
        return [_point(item, f"edit.{name}[{index}]") for index, item in enumerate(raw)]

    left = points_for("left_points", 2, 100)
    right = points_for("right_points", 2, 100)
    if mode == "line" and (len(left) != 2 or len(right) != 2):
        raise AnnotatorError("Lineモードの左右境界は各2点にしてください")
    if left[0] == left[1] or right[0] == right[1]:
        raise AnnotatorError("境界の先頭2点は異なる位置にしてください")
    far_end = value.get("far_end")
    if not isinstance(far_end, dict) or far_end.get("mode") not in {
        "image_boundary", "line"
    }:
        raise AnnotatorError("edit.far_end.modeが不正です")
    end_points: list[list[float]] = []
    if far_end["mode"] == "line":
        raw_end = far_end.get("points")
        if not isinstance(raw_end, list) or len(raw_end) != 2:
            raise AnnotatorError("画像内終端は2点で指定してください")
        end_points = [
            _point(item, f"edit.far_end.points[{index}]")
            for index, item in enumerate(raw_end)
        ]
        if end_points[0] == end_points[1]:
            raise AnnotatorError("終端の2点は異なる位置にしてください")
    return {
        "mode": mode,
        "left_points": left,
        "right_points": right,
        "far_end": {"mode": far_end["mode"], "points": end_points},
    }


class DatasetAnnotator:
    """Manifest-backed annotation state and safe label persistence."""

    def __init__(self, dataset_dir: Path) -> None:
        self.viewer = dataset_viewer.DatasetViewer(dataset_dir, page_size=2)
        self.root = self.viewer.root
        self.rows = self.viewer.rows
        self.row_by_id = self.viewer.row_by_id
        self.groups = self.viewer.groups
        self.sites = self.viewer.sites
        self.classes = self._read_classes()
        self._write_lock = threading.Lock()
        self.status_by_id = {
            row["image_id"]: self._status_from_disk(row) for row in self.rows
        }

    def _read_classes(self) -> list[str]:
        config_path = self.root / "metadata" / "dataset.yaml"
        try:
            import yaml

            content = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            classes = content.get("labels", {}).get("classes", [])
        except (OSError, ValueError, TypeError):
            classes = []
        if not isinstance(classes, list) or not classes:
            return ["nakaaze"]
        return [str(item) for item in classes]

    def _row(self, image_id: str) -> dict[str, str]:
        row = self.row_by_id.get(image_id)
        if row is None:
            raise AnnotatorError("manifestに存在しないimage_idです")
        return row

    def label_path(self, row: dict[str, str]) -> Path:
        relative = Path(row["left_path"])
        if (
            not relative.parts
            or relative.parts[0] != "images"
            or relative.stem != row["image_id"]
            or len(relative.parts) < 3
        ):
            raise AnnotatorError(f"Left画像の配置が不正です: {row['image_id']}")
        path = (self.root / "labels" / Path(*relative.parts[1:])).with_suffix(".txt")
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root / "labels")
        except ValueError as exc:
            raise AnnotatorError("ラベルパスがデータセット外を指しています") from exc
        return resolved

    def sidecar_path(self, image_id: str) -> Path:
        self._row(image_id)
        return self.root / "metadata" / "annotation_state" / f"{image_id}.json"

    def _status_from_disk(self, row: dict[str, str]) -> str:
        path = self.label_path(row)
        if not path.is_file():
            return "unannotated"
        try:
            return "positive" if path.read_text(encoding="utf-8").strip() else "empty"
        except OSError as exc:
            raise AnnotatorError(f"ラベルを読み込めません: {path.name}") from exc

    def _label_polygon(self, row: dict[str, str]) -> list[list[float]]:
        path = self.label_path(row)
        if not path.is_file():
            return []
        try:
            lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
        except OSError as exc:
            raise AnnotatorError(f"ラベルを読み込めません: {path.name}") from exc
        lines = [line for line in lines if line]
        if not lines:
            return []
        if len(lines) > 1:
            raise AnnotatorError("現在のアノテーターは1画像1領域に対応しています")
        parts = lines[0].split()
        if len(parts) < 7 or len(parts) % 2 == 0:
            raise AnnotatorError(f"YOLO-segラベルの形式が不正です: {path.name}")
        try:
            class_id = int(parts[0])
        except ValueError as exc:
            raise AnnotatorError(f"クラスIDが不正です: {path.name}") from exc
        if class_id != 0:
            raise AnnotatorError("現在のアノテーターはクラス0に対応しています")
        try:
            values = [float(value) for value in parts[1:]]
        except ValueError as exc:
            raise AnnotatorError(f"ラベル座標が不正です: {path.name}") from exc
        return validate_polygon([
            [values[index], values[index + 1]] for index in range(0, len(values), 2)
        ])

    def annotation(self, image_id: str) -> dict[str, Any]:
        row = self._row(image_id)
        status = self.status_by_id[image_id]
        edit = None
        sidecar = self.sidecar_path(image_id)
        if sidecar.is_file():
            try:
                content = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(content, dict):
                    edit = content.get("edit")
            except (OSError, json.JSONDecodeError):
                edit = None
        return {
            "image_id": image_id,
            "status": status,
            "polygon": self._label_polygon(row) if status == "positive" else [],
            "edit": edit,
        }

    def _group_metadata(self) -> list[dict[str, Any]]:
        return [
            {
                "id": group,
                "label": dataset_viewer.group_label(group),
                "count": sum(row["increment_group"] == group for row in self.rows),
            }
            for group in self.groups
        ]

    def _site_metadata(self) -> list[dict[str, Any]]:
        return [
            {
                "id": site,
                "session_id": next(
                    row["session_id"] for row in self.rows if row["site_id"] == site
                ),
                "count": sum(row["site_id"] == site for row in self.rows),
            }
            for site in self.sites
        ]

    def _presets(self) -> list[dict[str, Any]]:
        included: list[str] = []
        cumulative = 0
        presets = []
        for group in self.groups:
            included.append(group)
            cumulative += dataset_viewer.increment_value(group)
            presets.append({"label": f"n{cumulative:03d}", "groups": list(included)})
        return presets

    def _readiness(self) -> list[dict[str, Any]]:
        result = []
        for preset in self._presets():
            included = set(preset["groups"])
            ids = [
                row["image_id"] for row in self.rows
                if row["increment_group"] in included
            ]
            completed = sum(self.status_by_id[image_id] != "unannotated" for image_id in ids)
            result.append({
                "label": preset["label"],
                "completed": completed,
                "total": len(ids),
                "ready": completed == len(ids),
            })
        return result

    def state(
        self,
        offset: int = 0,
        groups: Iterable[str] = (),
        sites: Iterable[str] = (),
        statuses: Iterable[str] = (),
        search: str = "",
        sort: str = "site_frame",
    ) -> dict[str, Any]:
        selected_groups = set(groups)
        selected_sites = set(sites)
        selected_statuses = set(statuses)
        if unknown := selected_groups - set(self.groups):
            raise AnnotatorError(f"不明なaddグループです: {sorted(unknown)}")
        if unknown := selected_sites - set(self.sites):
            raise AnnotatorError(f"不明なsiteです: {sorted(unknown)}")
        if unknown := selected_statuses - ANNOTATION_STATUSES:
            raise AnnotatorError(f"不明な状態です: {sorted(unknown)}")
        if sort not in SORT_MODES:
            raise AnnotatorError(f"不明な並び順です: {sort}")
        query = search.strip().lower()
        scope_rows = [
            row for row in self.rows
            if (not selected_groups or row["increment_group"] in selected_groups)
            and (not selected_sites or row["site_id"] in selected_sites)
            and (not query or query in row["image_id"].lower())
        ]
        counts = {
            status: sum(
                self.status_by_id[row["image_id"]] == status for row in scope_rows
            )
            for status in sorted(ANNOTATION_STATUSES)
        }
        filtered = [
            row for row in scope_rows
            if not selected_statuses
            or self.status_by_id[row["image_id"]] in selected_statuses
        ]
        if sort == "sample_order":
            filtered.sort(key=lambda row: dataset_viewer.integer_field(row, "sample_order"))
        elif sort == "image_id":
            filtered.sort(key=lambda row: row["image_id"])
        else:
            site_order = {site: index for index, site in enumerate(self.sites)}
            group_order = {group: index for index, group in enumerate(self.groups)}
            filtered.sort(key=lambda row: (
                site_order[row["site_id"]],
                group_order[row["increment_group"]],
                dataset_viewer.integer_field(row, "actual_frame_index"),
            ))
        page_offset = max(0, offset)
        page_rows = filtered[page_offset:page_offset + 2]
        items = []
        for row in page_rows:
            image_id = row["image_id"]
            assets = {}
            for modality in ("left", "right", "depth"):
                if self.viewer.resolve_asset(row, modality) is not None:
                    assets[modality] = (
                        f"/depth/{quote(image_id)}" if modality == "depth"
                        else f"/asset/{modality}/{quote(image_id)}"
                    )
            items.append({
                "image_id": image_id,
                "sample_order": dataset_viewer.integer_field(row, "sample_order"),
                "actual_frame_index": dataset_viewer.integer_field(
                    row, "actual_frame_index"
                ),
                "site_id": row["site_id"],
                "session_id": row["session_id"],
                "increment_group": row["increment_group"],
                "increment_label": dataset_viewer.group_label(row["increment_group"]),
                "status": self.status_by_id[image_id],
                "assets": assets,
            })
        return {
            "dataset_name": self.root.name,
            "classes": self.classes,
            "total": len(self.rows),
            "scope_total": len(scope_rows),
            "filtered_total": len(filtered),
            "offset": page_offset,
            "items": items,
            "counts": counts,
            "groups": self._group_metadata(),
            "sites": self._site_metadata(),
            "presets": self._presets(),
            "readiness": self._readiness(),
        }

    def save_positive(self, image_id: str, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise AnnotatorError("JSONオブジェクトを送信してください")
        polygon = validate_polygon(body.get("polygon"))
        edit = validate_edit_state(body.get("edit"))
        row = self._row(image_id)
        label = "0 " + " ".join(
            f"{coordinate:.6f}" for point in polygon for coordinate in point
        ) + "\n"
        sidecar = {
            "version": 1,
            "image_id": image_id,
            "status": "positive",
            "class_id": 0,
            "class_name": self.classes[0],
            "edit": edit,
        }
        with self._write_lock:
            _atomic_write(self.label_path(row), label.encode("utf-8"))
            _atomic_write(
                self.sidecar_path(image_id),
                (json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
            self.status_by_id[image_id] = "positive"
        return {"ok": True, "status": "positive"}

    def save_empty(self, image_id: str) -> dict[str, Any]:
        row = self._row(image_id)
        sidecar = {
            "version": 1,
            "image_id": image_id,
            "status": "empty",
            "class_id": 0,
            "class_name": self.classes[0],
            "edit": None,
        }
        with self._write_lock:
            _atomic_write(self.label_path(row), b"")
            _atomic_write(
                self.sidecar_path(image_id),
                (json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
            self.status_by_id[image_id] = "empty"
        return {"ok": True, "status": "empty"}

    def delete(self, image_id: str) -> dict[str, Any]:
        row = self._row(image_id)
        with self._write_lock:
            for path in (self.label_path(row), self.sidecar_path(image_id)):
                if path.is_file():
                    path.unlink()
            self.status_by_id[image_id] = "unannotated"
        return {"ok": True, "status": "unannotated"}


def _list_query(query: dict[str, list[str]], name: str) -> list[str]:
    values = []
    for value in query.get(name, []):
        values.extend(item for item in value.split(",") if item)
    return values


def make_handler(application: DatasetAnnotator, ui_root: Path):
    class Handler(BaseHTTPRequestHandler):
        def send_bytes(
            self,
            payload: bytes,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
            cache: str = "no-store",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass

        def send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_bytes(
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def send_file(
            self,
            path: Path,
            content_type: str | None = None,
            cache: str = "private, max-age=3600",
        ) -> None:
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_bytes(
                path.read_bytes(),
                content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                cache=cache,
            )

        def read_json(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise AnnotatorError("Content-Lengthが不正です") from exc
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise AnnotatorError("リクエストサイズが不正です")
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AnnotatorError("JSONを読み込めません") from exc

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/state":
                    self.send_json(application.state(
                        offset=max(0, int(query.get("offset", ["0"])[0])),
                        groups=_list_query(query, "groups"),
                        sites=_list_query(query, "sites"),
                        statuses=_list_query(query, "statuses"),
                        search=query.get("search", [""])[0],
                        sort=query.get("sort", ["site_frame"])[0],
                    ))
                    return
                if parsed.path.startswith("/api/annotation/"):
                    image_id = unquote(parsed.path.removeprefix("/api/annotation/"))
                    self.send_json(application.annotation(image_id))
                    return
                if parsed.path.startswith("/asset/"):
                    parts = parsed.path.split("/", 3)
                    if len(parts) != 4 or parts[2] not in {"left", "right"}:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    path = application.viewer.asset(unquote(parts[3]), parts[2])
                    if path is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                    else:
                        self.send_file(path)
                    return
                if parsed.path.startswith("/depth/"):
                    image_id = unquote(parsed.path.removeprefix("/depth/"))
                    path = application.viewer.asset(image_id, "depth")
                    if path is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                    else:
                        self.send_bytes(
                            dataset_viewer.encode_png(dataset_viewer.depth_to_image(
                                path, dataset_viewer.DepthOptions()
                            )),
                            "image/png",
                            cache="private, max-age=3600",
                        )
                    return
                static_files = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8"),
                }
                if parsed.path in static_files:
                    filename, content_type = static_files[parsed.path]
                    self.send_file(ui_root / filename, content_type, cache="no-store")
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except (AnnotatorError, dataset_viewer.ViewerError, ValueError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def do_PUT(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if not parsed.path.startswith("/api/annotation/"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                image_id = unquote(parsed.path.removeprefix("/api/annotation/"))
                self.send_json(application.save_positive(image_id, self.read_json()))
            except (AnnotatorError, dataset_viewer.ViewerError, ValueError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                prefix = "/api/annotation/"
                suffix = "/empty"
                if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                image_id = unquote(parsed.path[len(prefix):-len(suffix)])
                self.send_json(application.save_empty(image_id))
            except (AnnotatorError, dataset_viewer.ViewerError, ValueError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if not parsed.path.startswith("/api/annotation/"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                image_id = unquote(parsed.path.removeprefix("/api/annotation/"))
                self.send_json(application.delete(image_id))
            except (AnnotatorError, dataset_viewer.ViewerError, ValueError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def log_message(self, format_string: str, *args: Any) -> None:
            print(f"dataset-annotator: {format_string % args}")

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="完成済みZEDデータセットへYOLO-segラベルを作成します"
    )
    parser.add_argument("dataset_dir", help="metadata/manifest.csvを含むデータセット")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.port not in range(1, 65536):
            raise AnnotatorError("portは1～65535にしてください")
        application = DatasetAnnotator(Path(args.dataset_dir))
        ui_root = Path(__file__).resolve().parent.parent / "annotator_ui"
        missing = [
            name for name in ("index.html", "app.js", "style.css")
            if not (ui_root / name).is_file()
        ]
        if missing:
            raise AnnotatorError(f"アノテーターUIファイルがありません: {missing}")
        server = ThreadingHTTPServer(
            (args.host, args.port), make_handler(application, ui_root)
        )
        print(f"データセット: {application.root}")
        print(f"画像: {len(application.rows)}枚")
        print(f"クラス: 0 = {application.classes[0]}")
        print(f"アノテーター: http://{args.host}:{args.port}")
        print("終了するにはCtrl-Cを押してください")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (AnnotatorError, dataset_viewer.ViewerError, OSError) as exc:
        print(f"エラー: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
