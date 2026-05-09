#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple


def parse_bounds(value: str) -> Optional[Tuple[float, float, float, float]]:
    try:
        parts = [float(x.strip()) for x in value.split(",")]
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]


def merge_bounds(values: List[str]) -> Optional[str]:
    parsed = [p for p in (parse_bounds(v) for v in values) if p is not None]
    if not parsed:
        return None
    min_lon = min(p[0] for p in parsed)
    min_lat = min(p[1] for p in parsed)
    max_lon = max(p[2] for p in parsed)
    max_lat = max(p[3] for p in parsed)
    return f"{min_lon},{min_lat},{max_lon},{max_lat}"


def safe_int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def merge_json_metadata(values: List[str]) -> Optional[str]:
    layers_by_id: Dict[str, dict] = {}
    for raw in values:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        layers = parsed.get("vector_layers", [])
        if not isinstance(layers, list):
            continue
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            layer_id = layer.get("id")
            if isinstance(layer_id, str) and layer_id not in layers_by_id:
                layers_by_id[layer_id] = layer

    if not layers_by_id:
        return None
    return json.dumps({"vector_layers": list(layers_by_id.values())}, separators=(",", ":"))


def read_metadata(path: str) -> Dict[str, str]:
    con = sqlite3.connect(path)
    try:
        rows = con.execute("SELECT name, value FROM metadata").fetchall()
    finally:
        con.close()
    out: Dict[str, str] = {}
    for k, v in rows:
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    return out


def build_final_metadata(all_meta: List[Dict[str, str]]) -> Dict[str, str]:
    base = dict(all_meta[0]) if all_meta else {}

    minzooms = [safe_int(m.get("minzoom", "")) for m in all_meta]
    maxzooms = [safe_int(m.get("maxzoom", "")) for m in all_meta]
    minzooms = [z for z in minzooms if z is not None]
    maxzooms = [z for z in maxzooms if z is not None]

    if minzooms:
        base["minzoom"] = str(min(minzooms))
    if maxzooms:
        base["maxzoom"] = str(max(maxzooms))

    bounds_values = [m.get("bounds", "") for m in all_meta if m.get("bounds")]
    merged_bounds = merge_bounds(bounds_values)
    if merged_bounds is not None:
        base["bounds"] = merged_bounds

    json_values = [m.get("json", "") for m in all_meta if m.get("json")]
    merged_json = merge_json_metadata(json_values)
    if merged_json is not None:
        base["json"] = merged_json

    return base


def ensure_output_schema(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS metadata")
    con.execute("DROP TABLE IF EXISTS tiles")
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.execute(
        "CREATE TABLE tiles ("
        "zoom_level INTEGER NOT NULL,"
        "tile_column INTEGER NOT NULL,"
        "tile_row INTEGER NOT NULL,"
        "tile_data BLOB NOT NULL,"
        "UNIQUE (zoom_level, tile_column, tile_row)"
        ")"
    )


def merge_tiles(output_path: str, inputs: List[str], metadata: Dict[str, str]) -> None:
    if os.path.exists(output_path):
        os.remove(output_path)

    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    con = sqlite3.connect(output_path)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA locking_mode=EXCLUSIVE")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA cache_size=-262144")
        con.execute("PRAGMA mmap_size=1073741824")

        ensure_output_schema(con)

        for idx, path in enumerate(inputs):
            alias = f"src{idx}"
            abs_path = os.path.abspath(path).replace("'", "''")
            print(f"[merge] {idx + 1}/{len(inputs)}: {os.path.basename(path)}")
            start = time.time()
            con.execute(f"ATTACH DATABASE '{abs_path}' AS {alias}")
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                f"UPDATE tiles "
                f"SET tile_data = tile_data || ("
                f"SELECT s.tile_data FROM {alias}.tiles s "
                f"WHERE s.zoom_level = tiles.zoom_level "
                f"AND s.tile_column = tiles.tile_column "
                f"AND s.tile_row = tiles.tile_row"
                f") "
                f"WHERE EXISTS ("
                f"SELECT 1 FROM {alias}.tiles s "
                f"WHERE s.zoom_level = tiles.zoom_level "
                f"AND s.tile_column = tiles.tile_column "
                f"AND s.tile_row = tiles.tile_row"
                f")"
            )
            con.execute(
                f"INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
                f"SELECT s.zoom_level, s.tile_column, s.tile_row, s.tile_data "
                f"FROM {alias}.tiles s "
                f"WHERE NOT EXISTS ("
                f"SELECT 1 FROM tiles t "
                f"WHERE t.zoom_level = s.zoom_level "
                f"AND t.tile_column = s.tile_column "
                f"AND t.tile_row = s.tile_row"
                f")"
            )
            con.commit()
            con.execute(f"DETACH DATABASE {alias}")
            elapsed = time.time() - start
            print(f"[merge] done in {elapsed:.1f}s")

        con.executemany(
            "INSERT INTO metadata(name, value) VALUES (?, ?)",
            list(metadata.items()),
        )

        con.commit()

        count = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        print(f"[done] output tiles: {count}")
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast MBTiles merger with BLOB concatenation")
    parser.add_argument("output", help="Output .mbtiles path")
    parser.add_argument("inputs", nargs="+", help="Input .mbtiles files")
    args = parser.parse_args()

    for path in args.inputs:
        if not os.path.isfile(path):
            print(f"Missing input file: {path}", file=sys.stderr)
            return 2

    meta_list = [read_metadata(path) for path in args.inputs]
    final_meta = build_final_metadata(meta_list)

    total_start = time.time()
    merge_tiles(args.output, args.inputs, final_meta)
    print(f"[done] total time: {time.time() - total_start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
