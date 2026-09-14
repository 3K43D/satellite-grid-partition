"""读取 Grid 输出，为一个专员格生成高德地图 HTML 预览。"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
from urllib.parse import quote_plus

import numpy as np
import pandas as pd

from satellite_grid_partition_v1 import wgs84_to_gcj02


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 Grid 输出表生成一个专员格的高德地图页面。",
    )
    parser.add_argument(
        "--grid-file",
        required=True,
        help="Grid Parquet/CSV/XLSX 路径",
    )
    parser.add_argument("--grid-id", help="要展示的 grid_id")
    parser.add_argument(
        "--list-grids",
        action="store_true",
        help="列出可用 grid_id 后退出",
    )
    parser.add_argument(
        "--grid-id-column",
        default="grid_id",
        help="Grid ID 列名，默认 grid_id",
    )
    parser.add_argument(
        "--geometry-column",
        help="Geometry 列名；默认优先 grid_geometry_geojson_gcj02",
    )
    parser.add_argument(
        "--geometry-coordinate-system",
        choices=["GCJ02", "WGS84"],
        help="自定义 Geometry 列的坐标系",
    )
    parser.add_argument(
        "--output",
        default="amap_grid_preview.html",
        help="HTML 输出路径，默认 amap_grid_preview.html",
    )
    return parser.parse_args()


def load_grid_table(path: str) -> pd.DataFrame:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(source)
    if suffix == ".parquet":
        return pd.read_parquet(source)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(source)
    raise ValueError("地图模块目前支持 Parquet、CSV、XLSX 或 XLS。")


def convert_geojson_wgs84_to_gcj02(geometry: dict) -> dict:
    """递归转换 Polygon/MultiPolygon GeoJSON 坐标。"""
    def convert_coordinates(value):
        if (
            isinstance(value, list)
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            lng, lat = wgs84_to_gcj02(value[0], value[1])
            return [lng, lat] + list(value[2:])
        if isinstance(value, list):
            return [convert_coordinates(item) for item in value]
        return value

    converted = dict(geometry)
    converted["coordinates"] = convert_coordinates(
        geometry["coordinates"]
    )
    return converted


def json_value(value):
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def choose_geometry_column(
    table: pd.DataFrame,
    requested_column: str | None,
    requested_crs: str | None,
) -> tuple[str, str]:
    if requested_column:
        if requested_column not in table.columns:
            raise ValueError(
                f"找不到 Geometry 列 {requested_column!r}。"
                f"当前列：{list(table.columns)}"
            )
        if not requested_crs:
            raise ValueError(
                "使用 --geometry-column 时必须同时指定 "
                "--geometry-coordinate-system GCJ02 或 WGS84。"
            )
        return requested_column, requested_crs

    if "grid_geometry_geojson_gcj02" in table.columns:
        return "grid_geometry_geojson_gcj02", "GCJ02"
    if "grid_geometry_geojson_wgs84" in table.columns:
        return "grid_geometry_geojson_wgs84", "WGS84"
    if "grid_geometry_geojson" in table.columns:
        return "grid_geometry_geojson", "WGS84"

    raise ValueError(
        "Grid 表没有可用的 Geometry 列。运行算法时请勿使用 "
        "--no-grid-geometry。"
    )


def build_feature(row: pd.Series, geometry_column: str, geometry_crs: str) -> dict:
    raw_geometry = row[geometry_column]
    if pd.isna(raw_geometry) or not str(raw_geometry).strip():
        raise ValueError("所选专员格的 Geometry 为空。")

    geometry = json.loads(str(raw_geometry))
    if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError(
            f"仅支持 Polygon/MultiPolygon，实际={geometry.get('type')!r}。"
        )
    if geometry_crs == "WGS84":
        geometry = convert_geojson_wgs84_to_gcj02(geometry)

    property_columns = [
        "grid_id",
        "city",
        "admin_code",
        "admin_name",
        "grid_fyp",
        "target_expected_fyp",
        "grid_customer_count",
        "min_customer_count",
        "h3_count",
    ]
    properties = {
        column: json_value(row[column])
        for column in property_columns
        if column in row.index
    }
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": geometry,
    }


def build_html(feature: dict, amap_key: str, security_code: str) -> str:
    feature_json = json.dumps(
        {"type": "FeatureCollection", "features": [feature]},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    properties_json = json.dumps(
        feature["properties"],
        ensure_ascii=False,
        indent=2,
    )
    key_for_url = quote_plus(amap_key)
    security_json = json.dumps(security_code)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>专员格高德地图预览</title>
  <style>
    html, body {{ width: 100%; height: 100%; margin: 0; font-family: sans-serif; }}
    #map {{ width: 100%; height: 100%; }}
    #panel {{
      position: absolute; z-index: 10; top: 16px; left: 16px;
      max-width: 360px; padding: 14px 16px; border-radius: 8px;
      background: rgba(255,255,255,.94); box-shadow: 0 2px 12px rgba(0,0,0,.18);
      white-space: pre-wrap; font-size: 13px;
    }}
  </style>
  <script>window._AMapSecurityConfig = {{ securityJsCode: {security_json} }};</script>
  <script src="https://webapi.amap.com/maps?v=2.0&key={key_for_url}&plugin=AMap.GeoJSON"></script>
</head>
<body>
  <div id="panel">{html.escape(properties_json)}</div>
  <div id="map"></div>
  <script>
    const featureCollection = {feature_json};
    const map = new AMap.Map("map", {{ zoom: 12, viewMode: "2D" }});
    const layer = new AMap.GeoJSON({{
      geoJSON: featureCollection,
      getPolygon: function (geojson, lnglats) {{
        return new AMap.Polygon({{
          path: lnglats,
          fillColor: "#1677ff",
          fillOpacity: 0.35,
          strokeColor: "#0958d9",
          strokeWeight: 2,
          extData: geojson.properties
        }});
      }}
    }});
    map.add(layer);
    map.setFitView(layer.getOverlays());
  </script>
</body>
</html>
"""


def main() -> None:
    args = parse_args()
    table = load_grid_table(args.grid_file)

    if args.grid_id_column not in table.columns:
        raise ValueError(
            f"找不到 Grid ID 列 {args.grid_id_column!r}。"
            f"当前列：{list(table.columns)}"
        )

    available_ids = table[args.grid_id_column].dropna().astype(str)
    if args.list_grids:
        print("\n".join(available_ids.tolist()))
        return
    if not args.grid_id:
        sample = available_ids.head(20).tolist()
        raise ValueError(
            "请使用 --grid-id 选择一个专员格。"
            f"前 20 个可用值：{sample}"
        )

    selected = table[
        table[args.grid_id_column].astype(str) == str(args.grid_id)
    ]
    if selected.empty:
        raise ValueError(f"找不到 grid_id={args.grid_id!r}。")
    if len(selected) > 1:
        raise ValueError(
            f"grid_id={args.grid_id!r} 出现 {len(selected)} 行，应当唯一。"
        )

    geometry_column, geometry_crs = choose_geometry_column(
        table,
        args.geometry_column,
        args.geometry_coordinate_system,
    )
    feature = build_feature(selected.iloc[0], geometry_column, geometry_crs)

    amap_key = os.environ.get("AMAP_JS_API_KEY", "").strip()
    security_code = os.environ.get("AMAP_SECURITY_JS_CODE", "").strip()
    if not amap_key or not security_code:
        raise ValueError(
            "请先设置环境变量 AMAP_JS_API_KEY 和 AMAP_SECURITY_JS_CODE。"
        )

    output = Path(args.output)
    output.write_text(
        build_html(feature, amap_key, security_code),
        encoding="utf-8",
    )
    print(f"已生成：{output.resolve()}")
    print("请通过本地 HTTP 服务打开，不要将该 HTML 提交到 Git。")


if __name__ == "__main__":
    main()
