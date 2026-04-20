#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

APP_NAME = "komoot-gpx-exporter"
VERSION = "0.1.0"
BASE_API = "https://api.komoot.de/v007"
USER_AGENT = f"Mozilla/5.0 (compatible; {APP_NAME}/{VERSION})"
PAGE_SIZE = 24
MAX_WORKERS = 6
RETRIES = 4
REQUEST_TIMEOUT = 60
GPX_NS = "http://www.topografix.com/GPX/1/1"
COLLECTION_URL_RE = re.compile(r"/collection/(?P<id>\d+)(?:/(?P<slug>[^/?#]+))?", re.IGNORECASE)

ET.register_namespace("", GPX_NS)


def gpx_tag(name: str) -> str:
    return f"{{{GPX_NS}}}{name}"


@dataclass(frozen=True)
class CollectionConfig:
    id: int
    slug: str
    label: str
    url: str


@dataclass(frozen=True)
class CollectionRequest:
    id: int
    url: str
    slug_hint: str | None = None


@dataclass
class StageRecord:
    collection_id: int
    collection_label: str
    collection_slug: str
    collection_url: str
    stage_index: int
    tour_id: str
    tour_name: str
    tour_type: str
    tour_url: str
    distance_km: float
    elevation_up_m: float
    elevation_down_m: float
    point_count: int
    gpx_path: Path
    gpx_filename: str


def slugify(value: str, max_len: int = 80) -> str:
    slug = value.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    if not slug:
        slug = "route"
    return slug[:max_len].rstrip("-")


def request_json(url: str) -> Any:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    }
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                data = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    data = gzip.decompress(data)
                return json.loads(data.decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == RETRIES:
                break
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


def fetch_collection_meta(collection_id: int) -> dict[str, Any]:
    return request_json(f"{BASE_API}/collections/{collection_id}")


def fetch_collection_items(collection_id: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 0
    total_pages = 1
    while page < total_pages:
        data = request_json(
            f"{BASE_API}/collections/{collection_id}/compilation/?page={page}&limit={PAGE_SIZE}"
        )
        page_info = data["page"]
        total_pages = int(page_info["totalPages"])
        items.extend(data.get("_embedded", {}).get("items", []))
        page += 1
    return items


def fetch_coordinates(tour_id: str) -> list[dict[str, Any]]:
    data = request_json(f"{BASE_API}/tours/{tour_id}/coordinates")
    return data.get("items", [])


def normalize_url(raw: str, collection_id: int) -> str:
    if raw.isdigit():
        return f"https://www.komoot.com/collection/{collection_id}"
    candidate = raw.strip()
    if candidate.startswith("/"):
        candidate = f"https://www.komoot.com{candidate}"
    elif not re.match(r"^[a-z][a-z0-9+.-]*://", candidate, re.IGNORECASE):
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    return urlunparse(parsed._replace(query="", fragment=""))


def parse_collection_argument(raw: str) -> CollectionRequest:
    candidate = raw.strip()
    if not candidate:
        raise ValueError("Collection argument must not be empty.")
    if candidate.isdigit():
        collection_id = int(candidate)
        return CollectionRequest(id=collection_id, url=normalize_url(candidate, collection_id))

    match = COLLECTION_URL_RE.search(candidate)
    if match is None:
        raise ValueError(f"Could not extract a Komoot collection ID from '{raw}'.")

    collection_id = int(match.group("id"))
    slug_hint = match.group("slug")
    if slug_hint:
        slug_hint = slugify(slug_hint.lstrip("-"))
    return CollectionRequest(
        id=collection_id,
        url=normalize_url(candidate, collection_id),
        slug_hint=slug_hint or None,
    )


def resolve_collection_config(request: CollectionRequest, meta: dict[str, Any]) -> CollectionConfig:
    label = str(meta.get("name") or meta.get("title") or f"Komoot collection {request.id}")
    slug = request.slug_hint or slugify(label)
    return CollectionConfig(
        id=request.id,
        slug=slug,
        label=label,
        url=request.url,
    )


def is_tour_item(item: dict[str, Any]) -> bool:
    item_type = str(item.get("type", ""))
    return (item_type == "tour" or item_type.startswith("tour_")) and "id" in item


def append_link(parent: ET.Element, href: str, text: str | None = None) -> None:
    link = ET.SubElement(parent, gpx_tag("link"), {"href": href})
    if text:
        ET.SubElement(link, gpx_tag("text")).text = text


def build_track(collection_label: str, item: dict[str, Any], points: list[dict[str, Any]]) -> ET.Element:
    trk = ET.Element(gpx_tag("trk"))
    ET.SubElement(trk, gpx_tag("name")).text = item["name"]
    ET.SubElement(trk, gpx_tag("type")).text = collection_label
    desc = (
        f"{collection_label} | Komoot tour {item['id']} | "
        f"{item.get('distance', 0) / 1000:.1f} km | +{item.get('elevation_up', 0):.0f} m"
    )
    ET.SubElement(trk, gpx_tag("desc")).text = desc
    append_link(trk, f"https://www.komoot.com/tour/{item['id']}", "Komoot tour")
    seg = ET.SubElement(trk, gpx_tag("trkseg"))
    for point in points:
        attrs = {
            "lat": f"{point['lat']:.6f}",
            "lon": f"{point['lng']:.6f}",
        }
        trkpt = ET.SubElement(seg, gpx_tag("trkpt"), attrs)
        if "alt" in point and point["alt"] is not None:
            ET.SubElement(trkpt, gpx_tag("ele")).text = f"{point['alt']:.1f}"
    return trk


def build_gpx_root(name: str, description: str, links: list[tuple[str, str | None]]) -> ET.Element:
    root = ET.Element(
        gpx_tag("gpx"),
        {
            "version": "1.1",
            "creator": f"{APP_NAME}/{VERSION}",
        },
    )
    metadata = ET.SubElement(root, gpx_tag("metadata"))
    ET.SubElement(metadata, gpx_tag("name")).text = name
    ET.SubElement(metadata, gpx_tag("desc")).text = description
    for href, text in links:
        append_link(metadata, href, text)
    return root


def write_xml(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode"),
        encoding="utf-8",
    )


def write_stage_gpx(
    output_path: Path,
    collection_cfg: CollectionConfig,
    item: dict[str, Any],
    points: list[dict[str, Any]],
) -> None:
    root = build_gpx_root(
        name=item["name"],
        description=f"{collection_cfg.label} | exported from public Komoot API coordinates",
        links=[
            (collection_cfg.url, collection_cfg.label),
            (f"https://www.komoot.com/tour/{item['id']}", item["name"]),
        ],
    )
    root.append(build_track(collection_cfg.label, item, points))
    write_xml(output_path, root)


def collection_manifest_row(record: StageRecord) -> dict[str, Any]:
    return {
        "collection_id": record.collection_id,
        "collection_label": record.collection_label,
        "collection_slug": record.collection_slug,
        "collection_url": record.collection_url,
        "stage_index": record.stage_index,
        "tour_id": record.tour_id,
        "tour_name": record.tour_name,
        "tour_type": record.tour_type,
        "tour_url": record.tour_url,
        "distance_km": f"{record.distance_km:.3f}",
        "elevation_up_m": f"{record.elevation_up_m:.1f}",
        "elevation_down_m": f"{record.elevation_down_m:.1f}",
        "point_count": record.point_count,
        "gpx_filename": record.gpx_filename,
        "gpx_path": str(record.gpx_path),
    }


def export_stage(
    base_output_dir: Path,
    collection_cfg: CollectionConfig,
    item: dict[str, Any],
    stage_index: int,
) -> StageRecord:
    tour_id = str(item["id"])
    points = fetch_coordinates(tour_id)
    filename = f"{stage_index:02d}-{slugify(item['name'])}-{tour_id}.gpx"
    collection_dir = base_output_dir / collection_cfg.slug
    collection_dir.mkdir(parents=True, exist_ok=True)
    output_path = collection_dir / filename
    write_stage_gpx(output_path, collection_cfg, item, points)
    return StageRecord(
        collection_id=collection_cfg.id,
        collection_label=collection_cfg.label,
        collection_slug=collection_cfg.slug,
        collection_url=collection_cfg.url,
        stage_index=stage_index,
        tour_id=tour_id,
        tour_name=item["name"],
        tour_type=str(item.get("type", "")),
        tour_url=f"https://www.komoot.com/tour/{tour_id}",
        distance_km=item.get("distance", 0.0) / 1000.0,
        elevation_up_m=float(item.get("elevation_up", 0.0) or 0.0),
        elevation_down_m=float(item.get("elevation_down", 0.0) or 0.0),
        point_count=len(points),
        gpx_path=output_path.resolve(),
        gpx_filename=filename,
    )


def write_manifest(path: Path, records: list[StageRecord]) -> None:
    fieldnames = [
        "collection_id",
        "collection_label",
        "collection_slug",
        "collection_url",
        "stage_index",
        "tour_id",
        "tour_name",
        "tour_type",
        "tour_url",
        "distance_km",
        "elevation_up_m",
        "elevation_down_m",
        "point_count",
        "gpx_filename",
        "gpx_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow(collection_manifest_row(record))


def load_track(path: Path) -> ET.Element:
    stage_root = ET.parse(path).getroot()
    track = stage_root.find("gpx:trk", {"gpx": GPX_NS})
    if track is None:
        raise RuntimeError(f"No track found in {path}")
    return track


def build_merged_gpx(
    output_path: Path,
    name: str,
    description: str,
    links: list[tuple[str, str | None]],
    records: list[StageRecord],
) -> None:
    root = build_gpx_root(name=name, description=description, links=links)
    for record in records:
        root.append(load_track(record.gpx_path))
    write_xml(output_path, root)


def build_summary(
    path: Path,
    collection_meta: dict[int, dict[str, Any]],
    collections: list[CollectionConfig],
    records: list[StageRecord],
    combined_gpx_path: Path | None,
) -> None:
    by_collection: dict[int, list[StageRecord]] = {}
    for record in records:
        by_collection.setdefault(record.collection_id, []).append(record)

    lines: list[str] = []
    lines.append("# Komoot GPX Export Summary")
    lines.append("")
    lines.append("These GPX files were reconstructed from Komoot's public collection, tour, and coordinates APIs.")
    lines.append("Komoot's direct `.gpx` download endpoints returned `403`, so the tracks were exported from the public coordinate streams instead.")
    lines.append("")
    lines.append("## Collections")
    lines.append("")

    total_tracks = 0
    total_distance = 0.0
    for cfg in collections:
        meta = collection_meta[cfg.id]
        stages = by_collection.get(cfg.id, [])
        collection_distance = sum(stage.distance_km for stage in stages)
        total_tracks += len(stages)
        total_distance += collection_distance
        compilation_url = (
            meta.get("_links", {})
            .get("compilation", {})
            .get("href", f"{BASE_API}/collections/{cfg.id}/compilation/")
        )
        lines.append(f"- {cfg.label}: {len(stages)} tracks, {collection_distance:.1f} km")
        lines.append(f"  Source: {cfg.url}")
        lines.append(f"  API: {compilation_url}")

    lines.append("")
    lines.append(f"Total tracks: {total_tracks}")
    lines.append(f"Total distance: {total_distance:.1f} km")
    lines.append("")
    lines.append("## Outputs")
    lines.append("")
    if combined_gpx_path is not None:
        lines.append(f"- Combined GPX: `{combined_gpx_path.name}`")
    if len(collections) == 1:
        lines.append(f"- Merged GPX: `merged/{collections[0].slug}.gpx`")
    else:
        lines.append("- One merged GPX per collection: `merged/<collection-slug>.gpx`")
    lines.append("- Manifest CSV: `manifest.csv`")
    lines.append("- Per-stage GPX files: `stages/<collection-slug>/...`")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export tours from one or more Komoot collections into per-stage GPX files, "
            "merged GPX files, and a CSV manifest."
        ),
    )
    parser.add_argument(
        "collections",
        nargs="+",
        metavar="COLLECTION",
        help="Komoot collection URL(s) or numeric collection ID(s).",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="output",
        help="Directory where GPX files and manifests are written. Default: %(default)s",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=MAX_WORKERS,
        help="Maximum concurrent GPX stage exports. Default: %(default)s",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser.parse_args(argv)


def resolve_requested_collections(raw_values: list[str]) -> list[CollectionRequest]:
    requests: list[CollectionRequest] = []
    seen: set[int] = set()
    for raw in raw_values:
        request = parse_collection_argument(raw)
        if request.id in seen:
            continue
        seen.add(request.id)
        requests.append(request)
    return requests


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.max_workers < 1:
        raise SystemExit("--max-workers must be at least 1.")

    output_dir = Path(args.output_dir).expanduser().resolve()
    stages_dir = output_dir / "stages"
    merged_dir = output_dir / "merged"
    stages_dir.mkdir(parents=True, exist_ok=True)
    merged_dir.mkdir(parents=True, exist_ok=True)

    requested = resolve_requested_collections(args.collections)
    collection_meta: dict[int, dict[str, Any]] = {}
    collection_items: list[tuple[CollectionConfig, list[dict[str, Any]]]] = []

    print("Fetching collection metadata and stage lists...", file=sys.stderr)
    for request in requested:
        meta = fetch_collection_meta(request.id)
        cfg = resolve_collection_config(request, meta)
        items = fetch_collection_items(cfg.id)
        collection_meta[cfg.id] = meta
        collection_items.append((cfg, items))
        print(f"  {cfg.label}: {len(items)} items", file=sys.stderr)

    work_items: list[tuple[CollectionConfig, dict[str, Any], int]] = []
    for cfg, items in collection_items:
        tour_items = [item for item in items if is_tour_item(item)]
        for index, item in enumerate(tour_items, start=1):
            work_items.append((cfg, item, index))

    if not work_items:
        raise RuntimeError("No tour stages were found in the requested collection list.")

    print(f"Exporting {len(work_items)} GPX tracks...", file=sys.stderr)
    records: list[StageRecord] = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        future_map = {
            executor.submit(export_stage, stages_dir, cfg, item, index): (cfg, item, index)
            for cfg, item, index in work_items
        }
        for future in as_completed(future_map):
            cfg, item, _index = future_map[future]
            record = future.result()
            records.append(record)
            print(
                f"  wrote {cfg.slug}/{record.gpx_filename} ({record.point_count} points)",
                file=sys.stderr,
            )

    collection_order = {cfg.id: index for index, (cfg, _items) in enumerate(collection_items)}
    records.sort(
        key=lambda record: (
            collection_order[record.collection_id],
            record.stage_index,
            record.tour_name,
        )
    )

    manifest_path = output_dir / "manifest.csv"
    summary_path = output_dir / "README.md"
    combined_gpx_path: Path | None = None

    print("Writing manifest and merged GPX files...", file=sys.stderr)
    write_manifest(manifest_path, records)
    for cfg, _items in collection_items:
        collection_records = [record for record in records if record.collection_id == cfg.id]
        collection_gpx_path = merged_dir / f"{cfg.slug}.gpx"
        build_merged_gpx(
            output_path=collection_gpx_path,
            name=cfg.label,
            description=f"Merged GPX containing all tracks from {cfg.label}.",
            links=[(cfg.url, cfg.label)],
            records=collection_records,
        )

    if len(collection_items) > 1:
        combined_gpx_path = output_dir / "combined.gpx"
        build_merged_gpx(
            output_path=combined_gpx_path,
            name="Komoot collection bundle",
            description="Merged GPX containing all tracks from the requested Komoot collections.",
            links=[(cfg.url, cfg.label) for cfg, _items in collection_items],
            records=records,
        )

    build_summary(
        path=summary_path,
        collection_meta=collection_meta,
        collections=[cfg for cfg, _items in collection_items],
        records=records,
        combined_gpx_path=combined_gpx_path,
    )

    if len(collection_items) == 1:
        target = collection_items[0][0]
        print(f"Done. Merged GPX: {merged_dir / f'{target.slug}.gpx'}", file=sys.stderr)
    else:
        print(f"Done. Output directory: {output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
