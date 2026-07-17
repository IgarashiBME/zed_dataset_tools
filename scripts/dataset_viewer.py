#!/usr/bin/env python3
"""Browse a completed ZED dataset without modifying it."""

from __future__ import annotations

import argparse
import csv
import io
import json
import mimetypes
import re
from dataclasses import dataclass
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse


MANIFEST_REQUIRED_FIELDS = {
    "sample_order",
    "image_id",
    "session_id",
    "site_id",
    "increment_group",
    "actual_frame_index",
    "left_path",
    "right_path",
    "depth_path",
}

MODALITY_FIELDS = {
    "left": "left_path",
    "right": "right_path",
    "depth": "depth_path",
}

COLORMAP_ANCHORS: dict[str, tuple[tuple[float, tuple[int, int, int]], ...]] = {
    "turbo": (
        (0.000, (48, 18, 59)),
        (0.125, (70, 98, 215)),
        (0.250, (53, 171, 248)),
        (0.375, (26, 228, 182)),
        (0.500, (164, 252, 60)),
        (0.625, (249, 186, 56)),
        (0.750, (246, 107, 25)),
        (0.875, (201, 45, 53)),
        (1.000, (122, 4, 3)),
    ),
    "viridis": (
        (0.000, (68, 1, 84)),
        (0.250, (59, 82, 139)),
        (0.500, (33, 145, 140)),
        (0.750, (94, 201, 98)),
        (1.000, (253, 231, 37)),
    ),
    "magma": (
        (0.000, (0, 0, 4)),
        (0.250, (81, 18, 124)),
        (0.500, (183, 55, 121)),
        (0.750, (252, 137, 97)),
        (1.000, (252, 253, 191)),
    ),
    "grayscale": (
        (0.000, (0, 0, 0)),
        (1.000, (255, 255, 255)),
    ),
}


class ViewerError(RuntimeError):
    """Expected user-facing dataset viewer error."""


@dataclass(frozen=True)
class DepthOptions:
    minimum_m: float = 0.5
    maximum_m: float = 10.0
    gamma: float = 1.0
    colormap: str = "turbo"
    invert: bool = True
    auto_range: bool = False

    def validate(self) -> None:
        if not 0 <= self.minimum_m < self.maximum_m <= 65.535:
            raise ViewerError("深度範囲は0～65.535m内で、最小値<最大値にしてください")
        if not 0.2 <= self.gamma <= 3.0:
            raise ViewerError("gammaは0.2～3.0にしてください")
        if self.colormap not in COLORMAP_ANCHORS:
            raise ViewerError(f"未対応のカラーマップです: {self.colormap}")


def increment_value(group: str) -> int:
    match = re.fullmatch(r"add_(\d+)", group)
    if not match or int(match.group(1)) < 1:
        raise ViewerError(f"increment_groupが不正です: {group}")
    return int(match.group(1))


def group_label(group: str) -> str:
    return f"add{increment_value(group):03d}"


def integer_field(row: dict[str, str], field: str) -> int:
    try:
        return int(row[field])
    except (KeyError, ValueError) as exc:
        raise ViewerError(f"manifest.csvの{field}が整数ではありません") from exc


def build_colormap(name: str):
    import numpy as np

    anchors = COLORMAP_ANCHORS[name]
    positions = np.asarray([item[0] for item in anchors], dtype=np.float32)
    colors = np.asarray([item[1] for item in anchors], dtype=np.float32)
    samples = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    channels = [np.interp(samples, positions, colors[:, index]) for index in range(3)]
    return np.rint(np.stack(channels, axis=1)).astype(np.uint8)


@lru_cache(maxsize=len(COLORMAP_ANCHORS))
def colormap_lut(name: str):
    if name not in COLORMAP_ANCHORS:
        raise ViewerError(f"未対応のカラーマップです: {name}")
    return build_colormap(name)


def load_depth_millimeters(path: Path):
    import numpy as np
    from PIL import Image

    if path.suffix.lower() != ".png":
        raise ViewerError("ビューアの動的深度表示は16-bit PNGに対応しています")
    try:
        with Image.open(path) as image:
            depth = np.asarray(image)
    except (OSError, ValueError) as exc:
        raise ViewerError(f"深度画像を読み込めません: {path.name}") from exc
    if depth.ndim != 2 or depth.dtype.kind not in {"u", "i"}:
        raise ViewerError(f"16-bitグレースケール深度ではありません: {path.name}")
    return depth.astype(np.float32, copy=False)


def depth_to_image(path: Path, options: DepthOptions, thumbnail: bool = False):
    import numpy as np
    from PIL import Image

    options.validate()
    depth_mm = load_depth_millimeters(path)
    valid = np.isfinite(depth_mm) & (depth_mm > 0)
    minimum_m = options.minimum_m
    maximum_m = options.maximum_m
    if options.auto_range and np.any(valid):
        valid_m = depth_mm[valid] / 1000.0
        minimum_m, maximum_m = np.percentile(valid_m, (2.0, 98.0))
        if maximum_m - minimum_m < 0.001:
            maximum_m = minimum_m + 0.001

    normalized = np.zeros(depth_mm.shape, dtype=np.float32)
    normalized[valid] = np.clip(
        (depth_mm[valid] / 1000.0 - minimum_m) / (maximum_m - minimum_m),
        0.0,
        1.0,
    )
    if options.invert:
        normalized[valid] = 1.0 - normalized[valid]
    normalized[valid] = np.power(normalized[valid], options.gamma)
    indices = np.rint(normalized * 255.0).astype(np.uint8)
    rgb = colormap_lut(options.colormap)[indices]
    rgb[~valid] = 0
    image = Image.fromarray(rgb, mode="RGB")
    if thumbnail:
        image.thumbnail((480, 270), Image.Resampling.LANCZOS)
    return image


def encode_png(image: Any) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=3)
    return output.getvalue()


class DatasetViewer:
    def __init__(self, dataset_dir: Path, page_size: int = 48) -> None:
        self.root = dataset_dir.expanduser().resolve()
        if not self.root.is_dir():
            raise ViewerError(f"データセットディレクトリがありません: {self.root}")
        self.manifest_path = self.root / "metadata" / "manifest.csv"
        self.rows = self._read_manifest()
        self.row_by_id = {row["image_id"]: row for row in self.rows}
        self.page_size = page_size
        if not 1 <= self.page_size <= 96:
            raise ViewerError("page-sizeは1～96にしてください")
        self.groups = sorted(
            {row["increment_group"] for row in self.rows}, key=increment_value
        )
        self.sites = sorted({row["site_id"] for row in self.rows}, key=self._site_key)

    @staticmethod
    def _site_key(value: str) -> tuple[int, int | str]:
        match = re.fullmatch(r"site(\d+)", value)
        return (0, int(match.group(1))) if match else (1, value)

    def _read_manifest(self) -> list[dict[str, str]]:
        try:
            with self.manifest_path.open("r", encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                fields = set(reader.fieldnames or [])
                missing = sorted(MANIFEST_REQUIRED_FIELDS - fields)
                if missing:
                    raise ViewerError(f"manifest.csvの必須列がありません: {missing}")
                rows = list(reader)
        except OSError as exc:
            raise ViewerError(f"manifest.csvを読み込めません: {self.manifest_path}") from exc
        if not rows:
            raise ViewerError("manifest.csvに画像がありません")
        ids = [row["image_id"] for row in rows]
        if any(not image_id or Path(image_id).name != image_id for image_id in ids):
            raise ViewerError("manifest.csvに不正なimage_idがあります")
        if len(ids) != len(set(ids)):
            raise ViewerError("manifest.csvにimage_idの重複があります")
        for row in rows:
            integer_field(row, "sample_order")
            increment_value(row["increment_group"])
        return rows

    def resolve_asset(self, row: dict[str, str], modality: str) -> Path | None:
        field = MODALITY_FIELDS.get(modality)
        if field is None:
            raise ViewerError(f"未対応のモダリティです: {modality}")
        value = row.get(field, "")
        if not value:
            return None
        candidate = (self.root / value).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ViewerError(f"データセット外を参照するパスです: {value}") from exc
        return candidate if candidate.is_file() else None

    def asset(self, image_id: str, modality: str) -> Path | None:
        row = self.row_by_id.get(image_id)
        return None if row is None else self.resolve_asset(row, modality)

    def _group_metadata(self) -> list[dict[str, Any]]:
        return [
            {
                "id": group,
                "label": group_label(group),
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
            cumulative += increment_value(group)
            presets.append({"label": f"n{cumulative:03d}", "groups": list(included)})
        return presets

    def state(
        self,
        offset: int = 0,
        limit: int | None = None,
        groups: Iterable[str] = (),
        sites: Iterable[str] = (),
        search: str = "",
        sort: str = "sample_order",
    ) -> dict[str, Any]:
        selected_groups = set(groups)
        selected_sites = set(sites)
        no_groups = "__none__" in selected_groups
        no_sites = "__none__" in selected_sites
        selected_groups.discard("__none__")
        selected_sites.discard("__none__")
        unknown_groups = selected_groups - set(self.groups)
        unknown_sites = selected_sites - set(self.sites)
        if unknown_groups:
            raise ViewerError(f"不明なaddグループです: {sorted(unknown_groups)}")
        if unknown_sites:
            raise ViewerError(f"不明なsiteです: {sorted(unknown_sites)}")
        if sort not in {"sample_order", "site_group", "image_id"}:
            raise ViewerError(f"不明な並び順です: {sort}")
        query = search.strip().lower()
        filtered = [
            row for row in self.rows
            if not no_groups
            and not no_sites
            and (not selected_groups or row["increment_group"] in selected_groups)
            and (not selected_sites or row["site_id"] in selected_sites)
            and (not query or query in row["image_id"].lower())
        ]
        if sort == "sample_order":
            filtered.sort(key=lambda row: integer_field(row, "sample_order"))
        elif sort == "image_id":
            filtered.sort(key=lambda row: row["image_id"])
        else:
            site_order = {site: index for index, site in enumerate(self.sites)}
            group_order = {group: index for index, group in enumerate(self.groups)}
            filtered.sort(key=lambda row: (
                site_order[row["site_id"]],
                group_order[row["increment_group"]],
                row["image_id"],
            ))

        page_limit = self.page_size if limit is None else min(96, max(1, limit))
        page_offset = max(0, offset)
        page_rows = filtered[page_offset:page_offset + page_limit]
        items = []
        for row in page_rows:
            image_id = row["image_id"]
            available = [
                modality for modality in MODALITY_FIELDS
                if self.resolve_asset(row, modality) is not None
            ]
            items.append({
                "sample_order": integer_field(row, "sample_order"),
                "image_id": image_id,
                "session_id": row["session_id"],
                "site_id": row["site_id"],
                "increment_group": row["increment_group"],
                "increment_label": group_label(row["increment_group"]),
                "actual_frame_index": row["actual_frame_index"],
                "available_modalities": available,
                "assets": {
                    modality: (
                        f"/depth/{quote(image_id)}" if modality == "depth"
                        else f"/asset/{modality}/{quote(image_id)}"
                    )
                    for modality in available
                },
                "thumbnails": {
                    modality: f"/thumbnail/{modality}/{quote(image_id)}"
                    for modality in available
                },
            })
        return {
            "dataset_name": self.root.name,
            "total": len(self.rows),
            "filtered_total": len(filtered),
            "offset": page_offset,
            "limit": page_limit,
            "page_size": self.page_size,
            "items": items,
            "groups": self._group_metadata(),
            "sites": self._site_metadata(),
            "presets": self._presets(),
            "modalities": list(MODALITY_FIELDS),
            "colormaps": list(COLORMAP_ANCHORS),
        }

    @lru_cache(maxsize=256)
    def regular_thumbnail(self, modality: str, image_id: str) -> bytes:
        from PIL import Image, ImageOps

        path = self.asset(image_id, modality)
        if path is None:
            raise ViewerError("画像がありません")
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((480, 270), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=85, optimize=True)
                return output.getvalue()
        except (OSError, ValueError) as exc:
            raise ViewerError(f"画像を読み込めません: {path.name}") from exc

    @lru_cache(maxsize=256)
    def depth_thumbnail(self, image_id: str, options: DepthOptions) -> bytes:
        path = self.asset(image_id, "depth")
        if path is None:
            raise ViewerError("深度画像がありません")
        return encode_png(depth_to_image(path, options, thumbnail=True))

    def depth_image(self, image_id: str, options: DepthOptions) -> bytes:
        path = self.asset(image_id, "depth")
        if path is None:
            raise ViewerError("深度画像がありません")
        return encode_png(depth_to_image(path, options, thumbnail=False))


def boolean_query(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def depth_options(query: dict[str, list[str]]) -> DepthOptions:
    try:
        options = DepthOptions(
            minimum_m=float(query.get("min", ["0.5"])[0]),
            maximum_m=float(query.get("max", ["10.0"])[0]),
            gamma=float(query.get("gamma", ["1.0"])[0]),
            colormap=query.get("colormap", ["turbo"])[0],
            invert=boolean_query(query.get("invert", ["1"])[0]),
            auto_range=boolean_query(query.get("auto", ["0"])[0]),
        )
    except ValueError as exc:
        raise ViewerError("深度表示パラメーターが数値ではありません") from exc
    options.validate()
    return options


def list_query(query: dict[str, list[str]], name: str) -> list[str]:
    values = []
    for raw in query.get(name, []):
        values.extend(value for value in raw.split(",") if value)
    return values


def make_handler(application: DatasetViewer, ui_root: Path):
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

        def send_file(self, path: Path, content_type: str | None = None) -> None:
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_bytes(
                path.read_bytes(),
                content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                cache="private, max-age=3600",
            )

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/state":
                    self.send_json(application.state(
                        offset=max(0, int(query.get("offset", ["0"])[0])),
                        limit=int(query.get("limit", [str(application.page_size)])[0]),
                        groups=list_query(query, "groups"),
                        sites=list_query(query, "sites"),
                        search=query.get("search", [""])[0],
                        sort=query.get("sort", ["sample_order"])[0],
                    ))
                    return
                if parsed.path.startswith("/asset/"):
                    parts = parsed.path.split("/", 3)
                    if len(parts) != 4 or parts[2] not in {"left", "right"}:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    path = application.asset(unquote(parts[3]), parts[2])
                    if path is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                    else:
                        self.send_file(path)
                    return
                if parsed.path.startswith("/depth/"):
                    image_id = unquote(parsed.path.removeprefix("/depth/"))
                    self.send_bytes(
                        application.depth_image(image_id, depth_options(query)),
                        "image/png",
                        cache="private, max-age=3600",
                    )
                    return
                if parsed.path.startswith("/thumbnail/"):
                    parts = parsed.path.split("/", 3)
                    if len(parts) != 4 or parts[2] not in MODALITY_FIELDS:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    modality, image_id = parts[2], unquote(parts[3])
                    if modality == "depth":
                        payload = application.depth_thumbnail(image_id, depth_options(query))
                        content_type = "image/png"
                    else:
                        payload = application.regular_thumbnail(modality, image_id)
                        content_type = "image/jpeg"
                    self.send_bytes(
                        payload, content_type, cache="private, max-age=3600"
                    )
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
            except (ViewerError, ValueError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def log_message(self, format_string: str, *args: Any) -> None:
            print(f"dataset-viewer: {format_string % args}")

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="完成済みZEDデータセットを読み取り専用で表示します"
    )
    parser.add_argument("dataset_dir", help="metadata/manifest.csvを含むデータセット")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--page-size", type=int, default=48)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.port not in range(1, 65536):
            raise ViewerError("portは1～65535にしてください")
        application = DatasetViewer(Path(args.dataset_dir), page_size=args.page_size)
        ui_root = Path(__file__).resolve().parent.parent / "viewer_ui"
        missing = [
            name for name in ("index.html", "app.js", "style.css")
            if not (ui_root / name).is_file()
        ]
        if missing:
            raise ViewerError(f"ビューアUIファイルがありません: {missing}")
        server = ThreadingHTTPServer(
            (args.host, args.port), make_handler(application, ui_root)
        )
        print(f"データセット: {application.root}")
        print(f"画像: {len(application.rows)}枚")
        print(f"ビューア: http://{args.host}:{args.port}")
        print("終了するにはCtrl-Cを押してください")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except ViewerError as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
