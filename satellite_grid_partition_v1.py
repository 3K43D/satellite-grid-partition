# -*- coding: utf-8 -*-
"""
城市专员网格自动化划分算法 V1.2
================================

算法定位
--------
本文件实现当前已经锁定的 V1.2 逻辑：

1. 使用行政街道 Polygon / MultiPolygon 在地图上生成 H3 Resolution 9 全量空间；
2. 使用 H3 官方 Cell Center（中心点）判断：
   - H3 属于哪个行政街道；
   - H3 是否落入已有基础网格占用区域；
3. 将目标客户经纬度映射到 H3，并在 H3 级聚合 expected_fyp；
4. 对没有客户的合法 H3 保留记录并赋值 0，保证空间拓扑连续；
5. 在每个“城市 × 行政街道”内部独立运行：
   - 高引力种子选择；
   - 事务式 BFS 分层扩张；
   - 同层 expected_fyp 降序；
   - 城市距离（KM）按“直径”解释，实际 Seed 最大覆盖半径 = distance_km * 1000 / 2；
   - 同时达到城市 target_expected_fyp 与最低客户数后立即提交；
   - 失败事务回滚，失败 Seed 仅禁止再次作为 Seed，但仍可被其他网格吸收；
6. 主循环结束后，将剩余 H3 作为孤岛进行单向吸附；
7. 输出 Grid、H3 明细、客户—专员格明细、失败 Seed、废弃 H3、
   覆盖率漏斗、H3 中间池等结果。

当前明确锁死的业务口径
--------------------
A. H3 Resolution = 9。
B. 行政街道边界采用 H3 Center Point-in-Polygon。
C. 已有基础网格排除采用 H3 Center Point-in-Polygon。
D. 城市距离字段单位为 KM，业务含义为“一个专员格的最大跨度/直径”；
   因此算法实际 Seed 最大覆盖半径 = distance_km * 1000 / 2。
E. 行政街道 Polygon / MultiPolygon 不要求连续；
   MultiPolygon 会作为同一个行政街道的多个空间部分处理。
F. 不允许跨行政街道生长。
G. 无客户 H3 必须保留，FYP = 0。
H. 引力分数只用于选 Seed：
      gravity = 自身 H3 FYP + 当前未分配池内一阶合法邻居 H3 FYP
I. BFS 内部排序：
      1) H3 FYP 降序
      2) 到 Seed 距离升序
      3) H3 ID 字典序升序
J. Geometry 当前按最可能的情况处理：
   - 优先支持 GeoJSON / Python dict-like 字符串：
       {"type": "Polygon", "coordinates": ...}
       {"type": "MultiPolygon", "coordinates": ...}
   - 同时兼容 WKT；
   - 同时兼容 WKB / EWKB Hex 字符串。
K. 当前业务输入坐标系已确认为 GCJ-02，坐标顺序为 [longitude, latitude]。
   程序会先将客户点、行政街道和已有网格反算为 WGS84，再调用 H3；
   最终 Grid 同时输出 WGS84 与高德可直接使用的 GCJ-02 GeoJSON。
L. 每个成功专员格必须同时满足最低 FYP 与最低去重客户数；
   最低客户数默认为 50，可通过 AlgorithmConfig 调整。
M. 客户数按非空 customer_id 去重计算；expected_fyp=0 仍计入客户数，
   expected_fyp 为空的合法客户也计入客户数，但不贡献 FYP。

建议安装
--------
pip install pandas numpy shapely h3 openpyxl pyarrow

H3 版本
-------
代码同时兼容 h3-py v4 常见接口：
    latlng_to_cell
    cell_to_latlng
    grid_disk
    geo_to_cells / polygon_to_cells
以及 h3-py v3 常见接口：
    geo_to_h3
    h3_to_geo
    k_ring
    polyfill

推荐使用 h3-py 4.x。

使用方式一：Notebook / 其他 Python 文件
--------------------------------------
from satellite_grid_partition_v1 import (
    run_satellite_grid_algorithm,
    ColumnConfig,
    AlgorithmConfig,
)

config = AlgorithmConfig(
    min_customer_count=50,
)

result = run_satellite_grid_algorithm(
    customer_df=customer_df,
    admin_df=admin_df,
    existing_grid_df=existing_grid_df,
    fyp_threshold_df=fyp_threshold_df,
    distance_df=distance_df,
    config=config,
)

result.grids
result.h3_detail
result.grid_customer_detail
result.failed_seeds
result.abandoned_h3
result.coverage_metrics
result.h3_pool
result.customer_diagnostic

使用方式二：直接修改文件末尾的 INPUT_FILES，然后运行
----------------------------------------------------
python satellite_grid_partition_v1.py
"""

from __future__ import annotations

import ast
import json
import math
import re
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

try:
    import h3
except ImportError as exc:
    raise ImportError(
        "未安装 h3。请先执行：pip install h3"
    ) from exc

try:
    from shapely import wkb, wkt
    from shapely.geometry import Point, Polygon, MultiPolygon, mapping, shape
    from shapely.ops import transform as shapely_transform
    from shapely.ops import unary_union
    from shapely.prepared import prep
except ImportError as exc:
    raise ImportError(
        "未安装 shapely。请先执行：pip install shapely"
    ) from exc


# ============================================================
# 0. 配置
# ============================================================

@dataclass(frozen=True)
class ColumnConfig:
    """
    所有输入表字段名都集中在这里配置。
    如果研发底表字段名与默认值不同，只改这里，不改算法代码。
    """

    # ---------- 客户表 ----------
    customer_id: str = "customer_id"
    customer_city: str = "city"
    customer_lng: str = "lng"
    customer_lat: str = "lat"
    customer_admin_code: str = "area_admin_code"
    customer_expected_fyp: str = "expected_fyp"

    # ---------- 行政街道表 ----------
    admin_city: str = "city"
    admin_code: str = "area_code"
    admin_name: str = "area_name"
    admin_geometry: str = "area_geometry"

    # ---------- 已有基础网格表 ----------
    existing_city: str = "city"
    existing_agent_grid_id: str = "agent_net_id"
    existing_basic_grid_id: str = "basic_net_id"
    existing_geometry: str = "basic_net_geom"

    # ---------- 城市达标 FYP 表 ----------
    threshold_city: str = "city"
    threshold_fyp: str = "target_expected_fyp"

    # ---------- 城市距离表 ----------
    distance_city: str = "city"
    distance_km: str = "distance_km"


@dataclass(frozen=True)
class AlgorithmConfig:
    """
    算法级固定参数。
    """

    h3_resolution: int = 9

    # 当前业务输入（客户点、行政街道、已有网格）均为 GCJ-02。
    # H3 官方使用 WGS84 经纬度，因此算法会先转换为 WGS84 再计算 H3；
    # 最终 Grid 同时输出 WGS84 与高德可直接展示的 GCJ-02 GeoJSON。
    input_coordinate_system: str = "GCJ02"

    # 每个成功专员格的最低去重客户数，全城市默认使用同一参数。
    min_customer_count: int = 50

    # Haversine 地球平均半径，单位米
    earth_radius_m: float = 6_371_008.8

    # 是否要求客户表中的行政街道名称与 H3 Center 判定出的行政街道一致。
    # 建议保持 True，防止行政边界附近客户被错误跨街道计入。
    require_customer_admin_match: bool = True

    # expected_fyp 是否允许负数。正常业务下应为 False。
    allow_negative_fyp: bool = False

    # 是否计算最终 Grid 的 GeoJSON Geometry。
    # 数据量很大时可设 False 提速。
    build_grid_geometry: bool = True

    # 当一个 H3 Center 同时落入多个行政街道时：
    # False = 直接报错（推荐，便于发现行政边界数据问题）
    # True  = 按 admin_code 字典序选最小值，保证确定性。
    allow_admin_overlap_tiebreak: bool = False

    # 如果所有剩余可用 Seed 的最大引力都为 0 且 V_MIN > 0，
    # 继续尝试已无意义，直接结束该行政街道主循环。
    stop_when_max_gravity_zero: bool = True

    # 数值比较容差
    epsilon: float = 1e-9


@dataclass
class AlgorithmResult:
    """
    算法所有输出。
    """
    grids: pd.DataFrame
    h3_detail: pd.DataFrame
    grid_customer_detail: pd.DataFrame
    failed_seeds: pd.DataFrame
    abandoned_h3: pd.DataFrame
    coverage_metrics: pd.DataFrame
    h3_pool: pd.DataFrame
    customer_diagnostic: pd.DataFrame


# ============================================================
# 1. 基础校验工具
# ============================================================

def _require_columns(df: pd.DataFrame, required: Sequence[str], table_name: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"{table_name} 缺少必须字段：{missing}\n"
            f"当前字段：{list(df.columns)}"
        )


def _normalize_city_series(s: pd.Series) -> pd.Series:
    """
    城市作为跨表 Join Key，统一做 strip。
    不主动删除“市”字，避免把业务编码口径偷偷改掉。
    """
    return s.astype(str).str.strip()


def _normalize_code(value: Any) -> str:
    """
    行政街道编码 / H3 ID 等用于确定性排序时统一转字符串。
    """
    if pd.isna(value):
        return ""
    return str(value).strip()


def _safe_rate(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return np.nan
    return numerator / denominator


def _sanitize_id(text: Any) -> str:
    """
    生成 grid_id 时去除空格及特殊字符。
    """
    s = str(text)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "_", s)
    return s


# ============================================================
# 2. Geometry 解析
# ============================================================

def parse_geometry(value: Any):
    """
    将输入 Geometry 统一解析为 Shapely Geometry。

    支持：
    1. Shapely Geometry
    2. GeoJSON dict
    3. JSON 字符串
    4. Python dict-like 字符串（单引号也支持）
    5. WKT
    6. WKB / EWKB Hex

    当前截图最像：
        {"type": "MultiPolygon", "coordinates": [...]}
    """

    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None

    # 已经是 Shapely geometry
    if hasattr(value, "geom_type") and hasattr(value, "is_valid"):
        geom = value

    # dict / Mapping -> GeoJSON
    elif isinstance(value, Mapping):
        geom = shape(value)

    elif isinstance(value, (bytes, bytearray, memoryview)):
        geom = wkb.loads(bytes(value))

    elif isinstance(value, str):
        text = value.strip()

        if not text:
            return None

        geom = None

        # 1) JSON / GeoJSON
        if text.startswith("{") or text.startswith("["):
            try:
                obj = json.loads(text)
                if isinstance(obj, Mapping) and "type" in obj:
                    geom = shape(obj)
            except Exception:
                pass

            # 2) Python dict-like（例如单引号）
            if geom is None:
                try:
                    obj = ast.literal_eval(text)
                    if isinstance(obj, Mapping) and "type" in obj:
                        geom = shape(obj)
                except Exception:
                    pass

        # 3) WKT
        if geom is None and re.match(
            r"^\s*(MULTIPOLYGON|POLYGON|GEOMETRYCOLLECTION)\s*",
            text,
            flags=re.IGNORECASE,
        ):
            try:
                geom = wkt.loads(text)
            except Exception:
                pass

        # 4) WKB / EWKB HEX
        if geom is None and re.fullmatch(r"[0-9A-Fa-f]+", text) and len(text) % 2 == 0:
            try:
                geom = wkb.loads(bytes.fromhex(text))
            except Exception:
                pass

        if geom is None:
            raise ValueError(
                "无法识别 Geometry 格式。"
                "当前代码支持 GeoJSON / dict-like / WKT / WKB Hex。\n"
                f"样例开头：{text[:120]}"
            )

    else:
        raise TypeError(f"不支持的 Geometry 类型：{type(value)}")

    if geom is None or geom.is_empty:
        return None

    # 尝试修复轻微 invalid geometry
    if not geom.is_valid:
        try:
            repaired = geom.buffer(0)
            if repaired is not None and not repaired.is_empty and repaired.is_valid:
                geom = repaired
            else:
                warnings.warn("检测到 invalid geometry，且 buffer(0) 修复失败。")
        except Exception:
            warnings.warn("检测到 invalid geometry，自动修复失败。")

    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(
            f"当前算法只接受 Polygon / MultiPolygon，实际为：{geom.geom_type}"
        )

    return geom


def validate_lonlat_geometry(geom, name: str = "") -> None:
    """
    基本 CRS / 坐标顺序体检。

    注意：
    这只能发现“明显不是经纬度”的情况，
    不能自动证明数据一定是 EPSG:4326。
    """
    if geom is None or geom.is_empty:
        return

    minx, miny, maxx, maxy = geom.bounds

    if not (-180 <= minx <= 180 and -180 <= maxx <= 180):
        raise ValueError(
            f"{name} 的 X 范围不像经度：[{minx}, {maxx}]。"
            "请确认数据是否为经纬度坐标（WGS84 或 GCJ-02）。"
        )

    if not (-90 <= miny <= 90 and -90 <= maxy <= 90):
        raise ValueError(
            f"{name} 的 Y 范围不像纬度：[{miny}, {maxy}]。"
            "请确认数据是否为经纬度坐标（WGS84 或 GCJ-02）。"
        )


# ============================================================
# 3. WGS84 / GCJ-02 坐标转换
# ============================================================

_GCJ_PI = math.pi
_GCJ_A = 6378245.0
_GCJ_EE = 0.00669342162296594323


def normalize_coordinate_system(value: str) -> str:
    """将坐标系配置统一为 WGS84 或 GCJ02。"""
    normalized = str(value).strip().upper().replace("-", "")
    aliases = {
        "WGS84": "WGS84",
        "EPSG:4326": "WGS84",
        "EPSG4326": "WGS84",
        "GCJ02": "GCJ02",
        "AMAP": "GCJ02",
        "GAODE": "GCJ02",
    }
    if normalized not in aliases:
        raise ValueError(
            "input_coordinate_system 仅支持 GCJ02 或 WGS84/EPSG:4326，"
            f"实际值={value!r}。"
        )
    return aliases[normalized]


def _outside_gcj02_area(lng: float, lat: float) -> bool:
    """GCJ-02 加偏移算法的常用中国范围判断。"""
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _gcj_transform_lat(lng_offset: float, lat_offset: float) -> float:
    result = (
        -100.0
        + 2.0 * lng_offset
        + 3.0 * lat_offset
        + 0.2 * lat_offset * lat_offset
        + 0.1 * lng_offset * lat_offset
        + 0.2 * math.sqrt(abs(lng_offset))
    )
    result += (
        20.0 * math.sin(6.0 * lng_offset * _GCJ_PI)
        + 20.0 * math.sin(2.0 * lng_offset * _GCJ_PI)
    ) * 2.0 / 3.0
    result += (
        20.0 * math.sin(lat_offset * _GCJ_PI)
        + 40.0 * math.sin(lat_offset / 3.0 * _GCJ_PI)
    ) * 2.0 / 3.0
    result += (
        160.0 * math.sin(lat_offset / 12.0 * _GCJ_PI)
        + 320.0 * math.sin(lat_offset * _GCJ_PI / 30.0)
    ) * 2.0 / 3.0
    return result


def _gcj_transform_lng(lng_offset: float, lat_offset: float) -> float:
    result = (
        300.0
        + lng_offset
        + 2.0 * lat_offset
        + 0.1 * lng_offset * lng_offset
        + 0.1 * lng_offset * lat_offset
        + 0.1 * math.sqrt(abs(lng_offset))
    )
    result += (
        20.0 * math.sin(6.0 * lng_offset * _GCJ_PI)
        + 20.0 * math.sin(2.0 * lng_offset * _GCJ_PI)
    ) * 2.0 / 3.0
    result += (
        20.0 * math.sin(lng_offset * _GCJ_PI)
        + 40.0 * math.sin(lng_offset / 3.0 * _GCJ_PI)
    ) * 2.0 / 3.0
    result += (
        150.0 * math.sin(lng_offset / 12.0 * _GCJ_PI)
        + 300.0 * math.sin(lng_offset / 30.0 * _GCJ_PI)
    ) * 2.0 / 3.0
    return result


def wgs84_to_gcj02(lng: float, lat: float) -> Tuple[float, float]:
    """WGS84 -> GCJ-02。中国范围外保持不变。"""
    lng = float(lng)
    lat = float(lat)
    if _outside_gcj02_area(lng, lat):
        return lng, lat

    dlat = _gcj_transform_lat(lng - 105.0, lat - 35.0)
    dlng = _gcj_transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _GCJ_PI
    magic = math.sin(radlat)
    magic = 1.0 - _GCJ_EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (
        dlat * 180.0
        / ((_GCJ_A * (1.0 - _GCJ_EE)) / (magic * sqrt_magic) * _GCJ_PI)
    )
    dlng = (
        dlng * 180.0
        / (_GCJ_A / sqrt_magic * math.cos(radlat) * _GCJ_PI)
    )
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(
    lng: float,
    lat: float,
    tolerance: float = 1e-7,
    max_iterations: int = 10,
) -> Tuple[float, float]:
    """
    GCJ-02 -> WGS84 的迭代反算。

    高德官方只提供其他坐标转高德坐标，没有官方 GCJ-02 反算接口；
    本函数用于让 H3 在其要求的 WGS84 坐标上工作，并通过正向转换迭代收敛。
    """
    lng = float(lng)
    lat = float(lat)
    if _outside_gcj02_area(lng, lat):
        return lng, lat

    wgs_lng = lng
    wgs_lat = lat
    for _ in range(max_iterations):
        converted_lng, converted_lat = wgs84_to_gcj02(wgs_lng, wgs_lat)
        delta_lng = lng - converted_lng
        delta_lat = lat - converted_lat
        wgs_lng += delta_lng
        wgs_lat += delta_lat
        if abs(delta_lng) <= tolerance and abs(delta_lat) <= tolerance:
            break
    return wgs_lng, wgs_lat


def wgs84_to_gcj02_array(
    lng: np.ndarray,
    lat: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """NumPy 向量化的 WGS84 -> GCJ-02。"""
    lng_array, lat_array = np.broadcast_arrays(
        np.asarray(lng, dtype=np.float64),
        np.asarray(lat, dtype=np.float64),
    )
    output_lng = lng_array.copy()
    output_lat = lat_array.copy()
    inside = (
        np.isfinite(lng_array)
        & np.isfinite(lat_array)
        & (lng_array >= 72.004)
        & (lng_array <= 137.8347)
        & (lat_array >= 0.8293)
        & (lat_array <= 55.8271)
    )
    if not np.any(inside):
        return output_lng, output_lat

    current_lng = lng_array[inside]
    current_lat = lat_array[inside]
    lng_offset = current_lng - 105.0
    lat_offset = current_lat - 35.0

    dlat = (
        -100.0
        + 2.0 * lng_offset
        + 3.0 * lat_offset
        + 0.2 * lat_offset * lat_offset
        + 0.1 * lng_offset * lat_offset
        + 0.2 * np.sqrt(np.abs(lng_offset))
    )
    dlat += (
        20.0 * np.sin(6.0 * lng_offset * _GCJ_PI)
        + 20.0 * np.sin(2.0 * lng_offset * _GCJ_PI)
    ) * 2.0 / 3.0
    dlat += (
        20.0 * np.sin(lat_offset * _GCJ_PI)
        + 40.0 * np.sin(lat_offset / 3.0 * _GCJ_PI)
    ) * 2.0 / 3.0
    dlat += (
        160.0 * np.sin(lat_offset / 12.0 * _GCJ_PI)
        + 320.0 * np.sin(lat_offset * _GCJ_PI / 30.0)
    ) * 2.0 / 3.0

    dlng = (
        300.0
        + lng_offset
        + 2.0 * lat_offset
        + 0.1 * lng_offset * lng_offset
        + 0.1 * lng_offset * lat_offset
        + 0.1 * np.sqrt(np.abs(lng_offset))
    )
    dlng += (
        20.0 * np.sin(6.0 * lng_offset * _GCJ_PI)
        + 20.0 * np.sin(2.0 * lng_offset * _GCJ_PI)
    ) * 2.0 / 3.0
    dlng += (
        20.0 * np.sin(lng_offset * _GCJ_PI)
        + 40.0 * np.sin(lng_offset / 3.0 * _GCJ_PI)
    ) * 2.0 / 3.0
    dlng += (
        150.0 * np.sin(lng_offset / 12.0 * _GCJ_PI)
        + 300.0 * np.sin(lng_offset / 30.0 * _GCJ_PI)
    ) * 2.0 / 3.0

    radlat = np.deg2rad(current_lat)
    magic = np.sin(radlat)
    magic = 1.0 - _GCJ_EE * magic * magic
    sqrt_magic = np.sqrt(magic)
    dlat = (
        dlat
        * 180.0
        / (
            (_GCJ_A * (1.0 - _GCJ_EE))
            / (magic * sqrt_magic)
            * _GCJ_PI
        )
    )
    dlng = (
        dlng
        * 180.0
        / (_GCJ_A / sqrt_magic * np.cos(radlat) * _GCJ_PI)
    )
    output_lng[inside] = current_lng + dlng
    output_lat[inside] = current_lat + dlat
    return output_lng, output_lat


def gcj02_to_wgs84_array(
    lng: np.ndarray,
    lat: np.ndarray,
    tolerance: float = 1e-7,
    max_iterations: int = 10,
) -> Tuple[np.ndarray, np.ndarray]:
    """NumPy 向量化的 GCJ-02 -> WGS84 固定点迭代反算。"""
    original_lng, original_lat = np.broadcast_arrays(
        np.asarray(lng, dtype=np.float64),
        np.asarray(lat, dtype=np.float64),
    )
    wgs_lng = original_lng.copy()
    wgs_lat = original_lat.copy()
    active = (
        np.isfinite(original_lng)
        & np.isfinite(original_lat)
        & (original_lng >= 72.004)
        & (original_lng <= 137.8347)
        & (original_lat >= 0.8293)
        & (original_lat <= 55.8271)
    )

    for _ in range(max_iterations):
        active_positions = np.flatnonzero(active)
        if active_positions.size == 0:
            break
        converted_lng, converted_lat = wgs84_to_gcj02_array(
            wgs_lng[active_positions],
            wgs_lat[active_positions],
        )
        delta_lng = original_lng[active_positions] - converted_lng
        delta_lat = original_lat[active_positions] - converted_lat
        wgs_lng[active_positions] += delta_lng
        wgs_lat[active_positions] += delta_lat
        converged = (
            (np.abs(delta_lng) <= tolerance)
            & (np.abs(delta_lat) <= tolerance)
        )
        active[active_positions[converged]] = False

    return wgs_lng, wgs_lat


def _transform_geometry_coordinates(geom, vectorized_converter):
    """向量化转换 Shapely Geometry 的所有顶点。"""
    def transform_xy(x, y, z=None):
        x_array = np.asarray(x, dtype=np.float64)
        y_array = np.asarray(y, dtype=np.float64)
        scalar_input = x_array.ndim == 0
        new_x, new_y = vectorized_converter(
            np.atleast_1d(x_array),
            np.atleast_1d(y_array),
        )
        if scalar_input:
            converted_xy = (float(new_x[0]), float(new_y[0]))
        else:
            converted_xy = (new_x, new_y)
        return converted_xy if z is None else (*converted_xy, z)

    return shapely_transform(transform_xy, geom)


def geometry_to_wgs84(geom, source_coordinate_system: str):
    """将输入 Geometry 转为 H3 使用的 WGS84。"""
    source = normalize_coordinate_system(source_coordinate_system)
    if geom is None or geom.is_empty or source == "WGS84":
        return geom
    return _transform_geometry_coordinates(geom, gcj02_to_wgs84_array)


def geometry_to_gcj02(geom):
    """将 WGS84 Geometry 转为高德地图使用的 GCJ-02。"""
    if geom is None or geom.is_empty:
        return geom
    return _transform_geometry_coordinates(geom, wgs84_to_gcj02_array)


# ============================================================
# 4. H3 兼容层
# ============================================================

def h3_latlng_to_cell(lat: float, lng: float, resolution: int) -> str:
    """
    经纬度 -> H3。
    兼容 h3-py v4 / v3。
    """
    if hasattr(h3, "latlng_to_cell"):
        return h3.latlng_to_cell(float(lat), float(lng), resolution)
    if hasattr(h3, "geo_to_h3"):
        return h3.geo_to_h3(float(lat), float(lng), resolution)
    raise AttributeError(
        "当前 h3 包既没有 latlng_to_cell，也没有 geo_to_h3。"
        "请确认安装的是官方 h3-py。"
    )


def h3_cell_to_latlng(cell: str) -> Tuple[float, float]:
    """
    H3 -> (lat, lng)
    """
    if hasattr(h3, "cell_to_latlng"):
        lat, lng = h3.cell_to_latlng(cell)
        return float(lat), float(lng)
    if hasattr(h3, "h3_to_geo"):
        lat, lng = h3.h3_to_geo(cell)
        return float(lat), float(lng)
    raise AttributeError(
        "当前 h3 包既没有 cell_to_latlng，也没有 h3_to_geo。"
    )


def h3_neighbors(cell: str) -> Set[str]:
    """
    取 H3 一阶物理邻居。
    v4 grid_disk(cell, 1) / v3 k_ring(cell, 1) 都包含自身，因此剔除自身。

    五边形附近理论邻居数可能少于 6，这是 H3 正常拓扑现象。
    """
    if hasattr(h3, "grid_disk"):
        cells = set(h3.grid_disk(cell, 1))
    elif hasattr(h3, "k_ring"):
        cells = set(h3.k_ring(cell, 1))
    else:
        raise AttributeError(
            "当前 h3 包既没有 grid_disk，也没有 k_ring。"
        )

    cells.discard(cell)
    return cells


def _polygon_to_h3_v4_latlngpoly(poly: Polygon, resolution: int) -> Set[str]:
    """
    v4 polygon_to_cells 的兜底实现。
    Shapely 是 (lng, lat)，H3 LatLngPoly 需要 (lat, lng)。
    """
    if not hasattr(h3, "LatLngPoly") or not hasattr(h3, "polygon_to_cells"):
        raise AttributeError("缺少 v4 LatLngPoly / polygon_to_cells 接口。")

    exterior = [(lat, lng) for lng, lat in list(poly.exterior.coords)[:-1]]

    holes = []
    for ring in poly.interiors:
        holes.append([(lat, lng) for lng, lat in list(ring.coords)[:-1]])

    h3_poly = h3.LatLngPoly(exterior, *holes)
    return set(h3.polygon_to_cells(h3_poly, resolution))


def h3_geometry_to_cells(geom, resolution: int) -> Set[str]:
    """
    Polygon / MultiPolygon -> H3 cells。

    优先使用 v4 geo_to_cells(GeoJSON, res)。
    否则使用 v4 LatLngPoly + polygon_to_cells。
    v3 使用 polyfill(..., geo_json_conformant=True)。

    最终仍会额外执行 H3 Center Point-in-Polygon，
    因此此处只负责生成候选 H3。
    """

    if geom is None or geom.is_empty:
        return set()

    polygons: List[Polygon]
    if geom.geom_type == "Polygon":
        polygons = [geom]
    elif geom.geom_type == "MultiPolygon":
        polygons = list(geom.geoms)
    else:
        raise ValueError(f"不支持的 Geometry 类型：{geom.geom_type}")

    result: Set[str] = set()

    for poly in polygons:
        geojson = mapping(poly)

        # h3-py v4 最方便接口
        if hasattr(h3, "geo_to_cells"):
            try:
                result.update(h3.geo_to_cells(geojson, resolution))
                continue
            except Exception:
                pass

        # h3-py v4 polygon_to_cells
        if hasattr(h3, "polygon_to_cells") and hasattr(h3, "LatLngPoly"):
            try:
                result.update(_polygon_to_h3_v4_latlngpoly(poly, resolution))
                continue
            except Exception:
                pass

        # h3-py v3
        if hasattr(h3, "polyfill"):
            try:
                result.update(
                    h3.polyfill(
                        geojson,
                        resolution,
                        geo_json_conformant=True,
                    )
                )
                continue
            except TypeError:
                # 某些旧版签名不接受 geo_json_conformant
                result.update(h3.polyfill(geojson, resolution))
                continue

        raise AttributeError(
            "无法找到可用的 Polygon -> H3 API。"
            "建议安装官方 h3-py 4.x：pip install -U h3"
        )

    return result


def h3_cell_boundary_polygon(cell: str) -> Polygon:
    """
    H3 -> Shapely Polygon。
    仅用于最终 Grid Geometry 输出，不参与归属判定。
    """
    if hasattr(h3, "cell_to_boundary"):
        boundary = h3.cell_to_boundary(cell)
    elif hasattr(h3, "h3_to_geo_boundary"):
        boundary = h3.h3_to_geo_boundary(cell)
    else:
        raise AttributeError(
            "当前 h3 包既没有 cell_to_boundary，也没有 h3_to_geo_boundary。"
        )

    # H3 返回 (lat, lng)，Shapely 需要 (lng, lat)
    coords = [(float(lng), float(lat)) for lat, lng in boundary]
    return Polygon(coords)


# ============================================================
# 5. 距离
# ============================================================

def haversine_distance_m(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
    earth_radius_m: float = 6_371_008.8,
) -> float:
    """
    Haversine 大圆距离，单位米。
    """
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lng2) - float(lng1))

    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(dlambda / 2.0) ** 2
    )
    a = min(1.0, max(0.0, a))

    return 2.0 * earth_radius_m * math.atan2(
        math.sqrt(a),
        math.sqrt(1.0 - a),
    )


# ============================================================
# 5. 输入数据标准化
# ============================================================

def prepare_admin_boundaries(
    admin_df: pd.DataFrame,
    cols: ColumnConfig,
    config: Optional[AlgorithmConfig] = None,
) -> pd.DataFrame:
    """
    解析行政街道 Polygon / MultiPolygon。

    当前数据契约要求每个 city + area_code 恰好一行。
    输入 Geometry 按配置从 GCJ-02 转为 H3 使用的 WGS84。
    """
    config = config or AlgorithmConfig()
    _require_columns(
        admin_df,
        [
            cols.admin_city,
            cols.admin_code,
            cols.admin_name,
            cols.admin_geometry,
        ],
        "行政街道表",
    )

    work = admin_df[
        [
            cols.admin_city,
            cols.admin_code,
            cols.admin_name,
            cols.admin_geometry,
        ]
    ].copy()

    work[cols.admin_city] = _normalize_city_series(work[cols.admin_city])
    work[cols.admin_code] = work[cols.admin_code].map(_normalize_code)

    duplicated = work.duplicated(
        [cols.admin_city, cols.admin_code],
        keep=False,
    )
    if duplicated.any():
        examples = work.loc[
            duplicated,
            [cols.admin_city, cols.admin_code, cols.admin_name],
        ].head(20)
        raise ValueError(
            "行政街道表要求每个 city + area_code 只有一行，"
            "但检测到重复记录。请检查取数逻辑。\n"
            f"示例：\n{examples}"
        )

    parsed = []
    for idx, value in work[cols.admin_geometry].items():
        try:
            geom = parse_geometry(value)
            validate_lonlat_geometry(
                geom,
                name=f"行政街道表 row={idx}",
            )
            geom = geometry_to_wgs84(
                geom,
                config.input_coordinate_system,
            )
            parsed.append(geom)
        except Exception as exc:
            raise ValueError(
                f"行政街道 Geometry 解析失败，row={idx}：{exc}"
            ) from exc

    work["_geometry"] = parsed
    work = work[work["_geometry"].notna()].copy()

    rows = []
    for (city, area_code), g in work.groupby(
        [cols.admin_city, cols.admin_code],
        sort=True,
        dropna=False,
    ):
        geoms = [x for x in g["_geometry"] if x is not None and not x.is_empty]
        if not geoms:
            continue

        geom = unary_union(geoms)
        name_values = [
            str(x).strip()
            for x in g[cols.admin_name].dropna().tolist()
        ]
        area_name = name_values[0] if name_values else ""

        rows.append(
            {
                "city": city,
                "admin_code": str(area_code),
                "admin_name": area_name,
                "geometry": geom,
            }
        )

    result = pd.DataFrame(rows)

    if result.empty:
        raise ValueError("行政街道表解析后为空。")

    return result.sort_values(
        ["city", "admin_code"],
        kind="mergesort",
    ).reset_index(drop=True)


def prepare_existing_occupied_area(
    existing_grid_df: pd.DataFrame,
    cols: ColumnConfig,
    config: Optional[AlgorithmConfig] = None,
) -> Dict[str, Any]:
    """
    按城市将所有已有基础网格 Geometry 做 Union。

    算法并不关心已有基础网格属于哪个专员格；
    只关心“哪些空间已经被占用”。
    """
    config = config or AlgorithmConfig()

    _require_columns(
        existing_grid_df,
        [
            cols.existing_city,
            cols.existing_geometry,
        ],
        "已有基础网格表",
    )

    work = existing_grid_df[
        [
            cols.existing_city,
            cols.existing_geometry,
        ]
    ].copy()

    work[cols.existing_city] = _normalize_city_series(
        work[cols.existing_city]
    )

    parsed = []
    for idx, value in work[cols.existing_geometry].items():
        try:
            geom = parse_geometry(value)
            validate_lonlat_geometry(
                geom,
                name=f"已有基础网格表 row={idx}",
            )
            geom = geometry_to_wgs84(
                geom,
                config.input_coordinate_system,
            )
            parsed.append(geom)
        except Exception as exc:
            raise ValueError(
                f"已有基础网格 Geometry 解析失败，row={idx}：{exc}"
            ) from exc

    work["_geometry"] = parsed
    work = work[work["_geometry"].notna()].copy()

    result: Dict[str, Any] = {}

    for city, g in work.groupby(cols.existing_city, sort=True):
        geoms = [
            x for x in g["_geometry"]
            if x is not None and not x.is_empty
        ]
        result[city] = unary_union(geoms) if geoms else None

    return result


def prepare_city_parameters(
    fyp_threshold_df: pd.DataFrame,
    distance_df: pd.DataFrame,
    cols: ColumnConfig,
) -> pd.DataFrame:
    """
    合并城市 V_MIN 与城市距离。

    重要：
    distance_km 的业务含义已锁死为“最大跨度 / 直径”，
    因此：
        max_seed_radius_m = distance_km * 1000 / 2
    """
    _require_columns(
        fyp_threshold_df,
        [cols.threshold_city, cols.threshold_fyp],
        "城市达标 FYP 表",
    )
    _require_columns(
        distance_df,
        [cols.distance_city, cols.distance_km],
        "城市距离表",
    )

    fyp = fyp_threshold_df[
        [cols.threshold_city, cols.threshold_fyp]
    ].copy()
    dist = distance_df[
        [cols.distance_city, cols.distance_km]
    ].copy()

    fyp[cols.threshold_city] = _normalize_city_series(
        fyp[cols.threshold_city]
    )
    dist[cols.distance_city] = _normalize_city_series(
        dist[cols.distance_city]
    )

    fyp[cols.threshold_fyp] = pd.to_numeric(
        fyp[cols.threshold_fyp],
        errors="coerce",
    )
    dist[cols.distance_km] = pd.to_numeric(
        dist[cols.distance_km],
        errors="coerce",
    )

    if fyp[cols.threshold_fyp].isna().any():
        raise ValueError("城市达标 FYP 表存在无法解析的 target_expected_fyp。")

    if dist[cols.distance_km].isna().any():
        raise ValueError("城市距离表存在无法解析的 distance_km。")

    # 每个城市只允许一个参数值
    fyp_dup = fyp.groupby(cols.threshold_city)[cols.threshold_fyp].nunique()
    bad_fyp = fyp_dup[fyp_dup > 1]
    if not bad_fyp.empty:
        raise ValueError(
            f"同一城市存在多个 target_expected_fyp：{bad_fyp.to_dict()}"
        )

    dist_dup = dist.groupby(cols.distance_city)[cols.distance_km].nunique()
    bad_dist = dist_dup[dist_dup > 1]
    if not bad_dist.empty:
        raise ValueError(
            f"同一城市存在多个 distance_km：{bad_dist.to_dict()}"
        )

    fyp = fyp.drop_duplicates(cols.threshold_city, keep="first")
    dist = dist.drop_duplicates(cols.distance_city, keep="first")

    merged = fyp.merge(
        dist,
        left_on=cols.threshold_city,
        right_on=cols.distance_city,
        how="outer",
        validate="one_to_one",
    )

    merged["city"] = merged[cols.threshold_city].fillna(
        merged[cols.distance_city]
    )

    if merged[cols.threshold_fyp].isna().any():
        cities = merged.loc[
            merged[cols.threshold_fyp].isna(),
            "city",
        ].tolist()
        raise ValueError(f"以下城市缺少 target_expected_fyp：{cities}")

    if merged[cols.distance_km].isna().any():
        cities = merged.loc[
            merged[cols.distance_km].isna(),
            "city",
        ].tolist()
        raise ValueError(f"以下城市缺少 distance_km：{cities}")

    if (merged[cols.threshold_fyp] <= 0).any():
        raise ValueError("target_expected_fyp 必须 > 0。")

    if (merged[cols.distance_km] <= 0).any():
        raise ValueError("distance_km 必须 > 0。")

    merged["target_expected_fyp"] = merged[cols.threshold_fyp].astype(float)
    merged["distance_km"] = merged[cols.distance_km].astype(float)

    # 业务已经明确：distance 是直径 / 最大跨度
    merged["max_seed_radius_m"] = merged["distance_km"] * 1000.0 / 2.0

    return merged[
        [
            "city",
            "target_expected_fyp",
            "distance_km",
            "max_seed_radius_m",
        ]
    ].sort_values("city").reset_index(drop=True)


# ============================================================
# 6. 构建完整行政街道 H3 空间
# ============================================================

def build_admin_h3_pool(
    admin_boundaries: pd.DataFrame,
    occupied_by_city: Dict[str, Any],
    config: AlgorithmConfig,
) -> pd.DataFrame:
    """
    行政街道 Geometry -> H3 Resolution 9。

    每个 H3 最终具有：
        h3_id
        city
        admin_code
        admin_name
        center_lat
        center_lng
        is_existing_occupied

    行政归属与已有网格排除均使用：
        H3 Center Point-in-Polygon
    """

    records: List[Dict[str, Any]] = []

    # 用于检测同一 H3 Center 被多个行政街道认领
    owner_map: Dict[Tuple[str, str], Tuple[str, str]] = {}

    prepared_occupied: Dict[str, Any] = {}
    for city, geom in occupied_by_city.items():
        prepared_occupied[city] = (
            prep(geom)
            if geom is not None and not geom.is_empty
            else None
        )

    for row in admin_boundaries.itertuples(index=False):
        city = str(row.city)
        admin_code = str(row.admin_code)
        admin_name = str(row.admin_name)
        geom = row.geometry

        prepared_admin = prep(geom)

        candidate_cells = h3_geometry_to_cells(
            geom,
            config.h3_resolution,
        )

        for cell in sorted(candidate_cells):
            lat, lng = h3_cell_to_latlng(cell)
            point = Point(lng, lat)
            gcj_lng, gcj_lat = wgs84_to_gcj02(lng, lat)

            # 再次显式执行中心点归属，锁死业务口径
            if not prepared_admin.covers(point):
                continue

            key = (city, cell)

            if key in owner_map:
                previous_code, previous_name = owner_map[key]

                if previous_code != admin_code:
                    if not config.allow_admin_overlap_tiebreak:
                        raise ValueError(
                            "检测到同一 H3 Center 同时落入多个行政街道。\n"
                            f"city={city}, h3={cell}, "
                            f"admin1={previous_code}/{previous_name}, "
                            f"admin2={admin_code}/{admin_name}\n"
                            "请检查行政街道 Geometry 是否重叠；"
                            "如业务确认允许重叠，可将 "
                            "allow_admin_overlap_tiebreak=True。"
                        )

                    # deterministic tie-break：保留较小 admin_code
                    if admin_code >= previous_code:
                        continue

                    # 如果新 admin_code 更小，需要删除旧记录
                    records = [
                        r for r in records
                        if not (r["city"] == city and r["h3_id"] == cell)
                    ]

            owner_map[key] = (admin_code, admin_name)

            occupied = False
            prepared_existing = prepared_occupied.get(city)
            if prepared_existing is not None:
                occupied = bool(prepared_existing.covers(point))

            records.append(
                {
                    "city": city,
                    "admin_code": admin_code,
                    "admin_name": admin_name,
                    "h3_id": cell,
                    "center_lat": lat,
                    "center_lng": lng,
                    "center_wgs84_lat": lat,
                    "center_wgs84_lng": lng,
                    "center_gcj02_lat": gcj_lat,
                    "center_gcj02_lng": gcj_lng,
                    "is_existing_occupied": occupied,
                }
            )

    pool = pd.DataFrame(records)

    if pool.empty:
        raise ValueError(
            "行政街道 Geometry 未生成任何 H3。"
            "请重点检查 Geometry 格式、坐标系、经纬度顺序与 h3 版本。"
        )

    pool = pool.sort_values(
        ["city", "admin_code", "h3_id"],
        kind="mergesort",
    ).reset_index(drop=True)

    return pool


# ============================================================
# 7. 客户映射、覆盖率漏斗、H3 FYP 聚合
# ============================================================

def prepare_customers_and_attach_fyp(
    customer_df: pd.DataFrame,
    h3_pool: pd.DataFrame,
    cols: ColumnConfig,
    config: AlgorithmConfig,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    客户 -> H3，并将 expected_fyp 聚合回完整 H3 池。

    重要：
    - H3 的行政归属来自行政街道 Geometry；
    - 客户表 customer_admin_code 配置当前指向中文行政街道名称，
      只作为一致性校验；
    - 默认 require_customer_admin_match=True：
      客户行政街道名称与 H3 Center 所属行政街道名称不一致时，
      不将该客户 FYP 计入网格算法，并在诊断表中标记。
    """

    required = [
        cols.customer_id,
        cols.customer_city,
        cols.customer_lng,
        cols.customer_lat,
        cols.customer_expected_fyp,
    ]

    # 客户行政街道是强烈建议字段。
    # 如果配置要求行政一致性，则必须存在。
    if config.require_customer_admin_match:
        required.append(cols.customer_admin_code)

    _require_columns(customer_df, required, "客户表")

    cust = customer_df.copy()

    cust[cols.customer_city] = _normalize_city_series(
        cust[cols.customer_city]
    )

    cust["_customer_id_norm"] = cust[
        cols.customer_id
    ].map(_normalize_code)
    cust["_customer_id_available"] = (
        cust["_customer_id_norm"] != ""
    )

    cust["_lng"] = pd.to_numeric(
        cust[cols.customer_lng],
        errors="coerce",
    )
    cust["_lat"] = pd.to_numeric(
        cust[cols.customer_lat],
        errors="coerce",
    )
    cust["_fyp"] = pd.to_numeric(
        cust[cols.customer_expected_fyp],
        errors="coerce",
    )

    if cols.customer_admin_code in cust.columns:
        cust["_customer_admin_name_norm"] = cust[
            cols.customer_admin_code
        ].map(_normalize_code)
    else:
        cust["_customer_admin_name_norm"] = ""

    # 完全相同的算法输入记录只保留一次。
    duplicate_subset = [
        "_customer_id_norm",
        cols.customer_city,
        "_lng",
        "_lat",
        "_fyp",
        "_customer_admin_name_norm",
    ]
    customer_rows_before_dedup = len(cust)
    cust = cust.drop_duplicates(
        subset=duplicate_subset,
        keep="first",
    ).copy()
    exact_duplicates_removed = customer_rows_before_dedup - len(cust)
    if exact_duplicates_removed > 0:
        warnings.warn(
            "客户表已按客户号、城市、经纬度、街道名称和 FYP "
            f"去除 {exact_duplicates_removed:,} 条完全重复记录。"
        )

    # 去除完全重复记录后，同一客户号仍出现多行，说明关键属性冲突。
    conflicting_duplicate = (
        cust["_customer_id_available"]
        & cust["_customer_id_norm"].duplicated(keep=False)
    )
    if conflicting_duplicate.any():
        example_columns = list(
            dict.fromkeys(
                [
                    cols.customer_id,
                    cols.customer_city,
                    cols.customer_lng,
                    cols.customer_lat,
                    cols.customer_expected_fyp,
                ]
                + (
                    [cols.customer_admin_code]
                    if cols.customer_admin_code in cust.columns
                    else []
                )
            )
        )
        examples = cust.loc[
            conflicting_duplicate,
        ].sort_values(
            "_customer_id_norm",
            kind="mergesort",
        )[example_columns].head(20)
        raise ValueError(
            "检测到同一非空 customer_id 对应多条冲突记录。"
            "同一客户号必须全局唯一；完全重复记录已自动去重，"
            "其余冲突请先在源数据中处理。\n"
            f"示例：\n{examples}"
        )

    cust["_valid_coordinate"] = (
        cust["_lng"].between(-180, 180)
        & cust["_lat"].between(-90, 90)
    )

    source_coordinate_system = normalize_coordinate_system(
        config.input_coordinate_system
    )
    valid_coordinate_mask = cust["_valid_coordinate"].to_numpy(dtype=bool)
    input_lng = cust["_lng"].to_numpy(dtype=np.float64)
    input_lat = cust["_lat"].to_numpy(dtype=np.float64)
    wgs84_lng = np.full(len(cust), np.nan, dtype=np.float64)
    wgs84_lat = np.full(len(cust), np.nan, dtype=np.float64)

    if source_coordinate_system == "GCJ02":
        converted_lng, converted_lat = gcj02_to_wgs84_array(
            input_lng[valid_coordinate_mask],
            input_lat[valid_coordinate_mask],
        )
    else:
        converted_lng = input_lng[valid_coordinate_mask]
        converted_lat = input_lat[valid_coordinate_mask]

    wgs84_lng[valid_coordinate_mask] = converted_lng
    wgs84_lat[valid_coordinate_mask] = converted_lat
    cust["_wgs84_lng"] = wgs84_lng
    cust["_wgs84_lat"] = wgs84_lat
    cust["_input_coordinate_system"] = source_coordinate_system

    cust["_fyp_available"] = cust["_fyp"].notna()

    if not config.allow_negative_fyp:
        negative_mask = cust["_fyp"].notna() & (cust["_fyp"] < 0)
        if negative_mask.any():
            examples = cust.loc[
                negative_mask,
                [
                    cols.customer_id,
                    cols.customer_city,
                    cols.customer_expected_fyp,
                ],
            ].head(10)
            raise ValueError(
                "检测到 expected_fyp < 0。当前配置不允许负值。\n"
                f"示例：\n{examples}"
            )

    # 经纬度有效才做 H3
    h3_ids: List[Optional[str]] = []
    for _, row in cust.iterrows():
        if not bool(row["_valid_coordinate"]):
            h3_ids.append(None)
            continue

        try:
            h3_ids.append(
                h3_latlng_to_cell(
                    row["_wgs84_lat"],
                    row["_wgs84_lng"],
                    config.h3_resolution,
                )
            )
        except Exception:
            h3_ids.append(None)

    cust["_h3_id"] = h3_ids

    pool_lookup = h3_pool[
        [
            "city",
            "h3_id",
            "admin_code",
            "admin_name",
            "is_existing_occupied",
        ]
    ].copy()

    pool_lookup = pool_lookup.rename(
        columns={
            "admin_code": "_h3_admin_code",
            "admin_name": "_h3_admin_name",
            "is_existing_occupied": "_h3_existing_occupied",
        }
    )

    cust = cust.merge(
        pool_lookup,
        left_on=[cols.customer_city, "_h3_id"],
        right_on=["city", "h3_id"],
        how="left",
        validate="many_to_one",
    )

    cust["_h3_in_admin_geometry"] = cust["_h3_admin_code"].notna()

    if cols.customer_admin_code in cust.columns:
        # 与 H3 Center 根据行政区 Polygon 得到的行政街道名称比较
        cust["_h3_admin_name_norm"] = cust[
            "_h3_admin_name"
        ].map(_normalize_code)

        cust["_admin_consistent"] = (
            cust["_h3_in_admin_geometry"]
            & (
                cust["_customer_admin_name_norm"]
                == cust["_h3_admin_name_norm"]
            )
        )

    else:

        cust["_customer_admin_name_norm"] = ""
        cust["_admin_consistent"] = cust["_h3_in_admin_geometry"]

    cust["_excluded_by_existing_grid"] = (
        cust["_h3_existing_occupied"].fillna(False).astype(bool)
    )

    # 客户数口径与 FYP 可用性解耦：只要客户号非空并满足空间条件，
    # 即使 expected_fyp 为空，也计入最低客户数。
    cust["_legal_customer_for_count"] = (
        cust["_customer_id_available"]
        & cust["_valid_coordinate"]
        & cust["_h3_in_admin_geometry"]
        & (~cust["_excluded_by_existing_grid"])
    )

    if config.require_customer_admin_match:
        cust["_legal_customer_for_count"] &= cust[
            "_admin_consistent"
        ]

    # FYP 口径在合法客户基础上额外要求 expected_fyp 可用。
    cust["_legal_h3_candidate"] = (
        cust["_legal_customer_for_count"]
        & cust["_fyp_available"]
    )

    # 合法客户 FYP 与去重客户数分别聚合，再挂回完整 H3 池。
    eligible_fyp = cust[cust["_legal_h3_candidate"]].copy()
    eligible_count = cust[cust["_legal_customer_for_count"]].copy()

    fyp_agg = (
        eligible_fyp.groupby(
            [cols.customer_city, "_h3_id"],
            as_index=False,
        )
        .agg(
            h3_expected_fyp=("_fyp", "sum"),
        )
        .rename(
            columns={
                cols.customer_city: "city",
                "_h3_id": "h3_id",
            }
        )
    )

    count_agg = (
        eligible_count.groupby(
            [cols.customer_city, "_h3_id"],
            as_index=False,
        )
        .agg(
            h3_customer_count=("_customer_id_norm", "nunique"),
        )
        .rename(
            columns={
                cols.customer_city: "city",
                "_h3_id": "h3_id",
            }
        )
    )

    agg = fyp_agg.merge(
        count_agg,
        on=["city", "h3_id"],
        how="outer",
        validate="one_to_one",
    )

    pool = h3_pool.copy()

    pool = pool.merge(
        agg,
        on=["city", "h3_id"],
        how="left",
        validate="one_to_one",
    )

    pool["h3_expected_fyp"] = (
        pool["h3_expected_fyp"].fillna(0.0).astype(float)
    )
    pool["h3_customer_count"] = (
        pool["h3_customer_count"].fillna(0).astype(int)
    )

    # 已有网格 H3 不参与 BFS。
    pool["is_legal_unassigned"] = ~pool["is_existing_occupied"].astype(bool)

    return cust, pool


# ============================================================
# 8. 单个行政街道 BFS 算法
# ============================================================

def _compute_gravity_scores(
    unassigned: Set[str],
    fyp: Dict[str, float],
    neighbor_map: Dict[str, Set[str]],
) -> Dict[str, float]:
    """
    gravity(h) = fyp(h) + 当前未分配池中的一阶合法邻居 fyp 总和
    """
    scores: Dict[str, float] = {}

    for cell in unassigned:
        score = float(fyp.get(cell, 0.0))
        for nb in neighbor_map[cell]:
            if nb in unassigned:
                score += float(fyp.get(nb, 0.0))
        scores[cell] = score

    return scores


def _build_one_admin(
    city: str,
    admin_code: str,
    admin_name: str,
    admin_pool: pd.DataFrame,
    target_fyp: float,
    max_seed_radius_m: float,
    config: AlgorithmConfig,
    global_grid_sequence_start: int,
) -> Tuple[
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    int,
]:
    """
    对一个 city × admin_code 独立运行完整算法。

    返回：
    grid_rows
    h3_detail_rows
    failed_seed_rows
    abandoned_rows
    next_global_grid_sequence
    """

    legal = admin_pool[
        admin_pool["is_legal_unassigned"]
    ].copy()

    if legal.empty:
        return [], [], [], [], global_grid_sequence_start

    h3_ids: Set[str] = set(legal["h3_id"].tolist())

    fyp: Dict[str, float] = dict(
        zip(
            legal["h3_id"],
            legal["h3_expected_fyp"].astype(float),
        )
    )

    customer_count: Dict[str, int] = dict(
        zip(
            legal["h3_id"],
            legal["h3_customer_count"].astype(int),
        )
    )
    min_customer_count = int(config.min_customer_count)

    centers: Dict[str, Tuple[float, float]] = {
        row.h3_id: (float(row.center_lat), float(row.center_lng))
        for row in legal.itertuples(index=False)
    }

    # 邻接关系只保留同一行政街道合法空间内邻居
    neighbor_map: Dict[str, Set[str]] = {}
    for cell in sorted(h3_ids):
        neighbor_map[cell] = h3_neighbors(cell) & h3_ids

    unassigned: Set[str] = set(h3_ids)
    unavailable_seeds: Set[str] = set()

    grids: Dict[str, Dict[str, Any]] = {}
    h3_assignment: Dict[str, Dict[str, Any]] = {}
    failed_rows: List[Dict[str, Any]] = []

    gravity = _compute_gravity_scores(
        unassigned,
        fyp,
        neighbor_map,
    )

    # --------------------------------------------------------
    # 主循环
    # --------------------------------------------------------
    while True:
        available_seeds = unassigned - unavailable_seeds

        if not available_seeds:
            break

        # Seed：gravity ↓, H3 ID ↑
        seed = min(
            available_seeds,
            key=lambda c: (
                -float(gravity.get(c, 0.0)),
                str(c),
            ),
        )

        seed_gravity = float(gravity.get(seed, 0.0))

        # 如果全盘剩余价值已经为 0，不再做无意义事务
        if (
            config.stop_when_max_gravity_zero
            and seed_gravity <= config.epsilon
            and target_fyp > config.epsilon
        ):
            break

        seed_lat, seed_lng = centers[seed]

        # ---------- 开启事务 ----------
        assigned_in_attempt: Set[str] = {seed}
        rejected_in_attempt: Set[str] = set()

        assignment_layer: Dict[str, int] = {seed: 0}

        current_value = float(fyp.get(seed, 0.0))
        current_customer_count = int(customer_count.get(seed, 0))

        current_layer: List[str] = [seed]
        current_layer_no = 0

        success = (
            current_value + config.epsilon >= target_fyp
            and current_customer_count >= min_customer_count
        )

        # ---------- BFS ----------
        while current_layer and not success:
            topology_candidates: Set[str] = set()

            for cell in current_layer:
                for nb in neighbor_map[cell]:
                    if nb not in unassigned:
                        continue
                    if nb in assigned_in_attempt:
                        continue
                    if nb in rejected_in_attempt:
                        continue
                    topology_candidates.add(nb)

            if not topology_candidates:
                current_layer = []
                break

            valid_candidates: List[Tuple[str, float]] = []

            for cell in topology_candidates:
                lat, lng = centers[cell]

                dist = haversine_distance_m(
                    seed_lat,
                    seed_lng,
                    lat,
                    lng,
                    config.earth_radius_m,
                )

                if dist > max_seed_radius_m + config.epsilon:
                    rejected_in_attempt.add(cell)
                else:
                    valid_candidates.append((cell, dist))

            # 同层：FYP ↓, 距离 ↑, H3 ID ↑
            valid_candidates.sort(
                key=lambda x: (
                    -float(fyp.get(x[0], 0.0)),
                    float(x[1]),
                    str(x[0]),
                )
            )

            next_layer: List[str] = []

            for cell, _dist in valid_candidates:
                assigned_in_attempt.add(cell)
                next_layer.append(cell)
                assignment_layer[cell] = current_layer_no + 1

                current_value += float(fyp.get(cell, 0.0))
                current_customer_count += int(
                    customer_count.get(cell, 0)
                )

                if (
                    current_value + config.epsilon >= target_fyp
                    and current_customer_count >= min_customer_count
                ):
                    success = True
                    break

            if success:
                break

            current_layer = next_layer
            current_layer_no += 1

        # ---------- 成功 Commit ----------
        if success:
            grid_seq = global_grid_sequence_start
            global_grid_sequence_start += 1

            grid_id = (
                f"{_sanitize_id(city)}_"
                f"{_sanitize_id(admin_code)}_"
                f"G{grid_seq:06d}"
            )

            grids[grid_id] = {
                "grid_id": grid_id,
                "city": city,
                "admin_code": admin_code,
                "admin_name": admin_name,
                "seed_h3": seed,
                "seed_lat": seed_lat,
                "seed_lng": seed_lng,
                "target_expected_fyp": float(target_fyp),
                "min_customer_count": min_customer_count,
                "max_seed_radius_m": float(max_seed_radius_m),
                "distance_diameter_km": float(max_seed_radius_m * 2 / 1000),
                "main_expansion_layers": int(
                    max(assignment_layer.values())
                ),
                "h3_set": set(assigned_in_attempt),
                "grid_fyp": float(current_value),
                "grid_customer_count": int(current_customer_count),
            }

            for cell in assigned_in_attempt:
                lat, lng = centers[cell]
                dist = haversine_distance_m(
                    seed_lat,
                    seed_lng,
                    lat,
                    lng,
                    config.earth_radius_m,
                )

                method = "SEED" if cell == seed else "MAIN_BFS"

                h3_assignment[cell] = {
                    "grid_id": grid_id,
                    "city": city,
                    "admin_code": admin_code,
                    "admin_name": admin_name,
                    "h3_id": cell,
                    "h3_fyp": float(fyp.get(cell, 0.0)),
                    "h3_customer_count": int(
                        customer_count.get(cell, 0)
                    ),
                    "seed_h3": seed,
                    "distance_to_seed_m": float(dist),
                    "assignment_method": method,
                    "assignment_layer": int(
                        assignment_layer[cell]
                    ),
                }

            unassigned -= assigned_in_attempt

            # 正式空间变化后，重新计算引力
            gravity = _compute_gravity_scores(
                unassigned,
                fyp,
                neighbor_map,
            )

        # ---------- 失败 Rollback ----------
        else:
            # 失败时 BFS 已经自然力竭，
            # 因为没有在双门槛达标前提前截断，
            # assigned_in_attempt 即当前 Seed 在所有硬约束下
            # 实际/理论能够连通触达的完整区域。
            max_reachable_fyp = float(
                sum(fyp.get(c, 0.0) for c in assigned_in_attempt)
            )
            max_reachable_customer_count = int(
                sum(
                    customer_count.get(c, 0)
                    for c in assigned_in_attempt
                )
            )

            fyp_reached = (
                max_reachable_fyp + config.epsilon >= target_fyp
            )
            customer_count_reached = (
                max_reachable_customer_count >= min_customer_count
            )

            if not fyp_reached and not customer_count_reached:
                failure_reason = "FYP_AND_CUSTOMER_COUNT_NOT_REACHED"
            elif not fyp_reached:
                failure_reason = "FYP_NOT_REACHED"
            else:
                failure_reason = "CUSTOMER_COUNT_NOT_REACHED"

            failed_rows.append(
                {
                    "city": city,
                    "admin_code": admin_code,
                    "admin_name": admin_name,
                    "seed_h3": seed,
                    "seed_fyp": float(fyp.get(seed, 0.0)),
                    "seed_customer_count": int(
                        customer_count.get(seed, 0)
                    ),
                    "seed_gravity_score": seed_gravity,
                    "target_vmin": float(target_fyp),
                    "min_customer_count": min_customer_count,
                    "max_seed_radius_m": float(max_seed_radius_m),
                    "max_reachable_fyp": max_reachable_fyp,
                    "max_reachable_customer_count": (
                        max_reachable_customer_count
                    ),
                    "fyp_gap": max(
                        0.0,
                        float(target_fyp) - max_reachable_fyp,
                    ),
                    "customer_count_gap": max(
                        0,
                        min_customer_count
                        - max_reachable_customer_count,
                    ),
                    "reachable_h3_count": int(
                        len(assigned_in_attempt)
                    ),
                    "expansion_layers": int(
                        max(assignment_layer.values())
                    ),
                    "failure_reason": failure_reason,
                }
            )

            # 事务回滚：
            # unassigned 从头到尾都没有被修改，
            # 因此这里只需销毁局部状态。
            unavailable_seeds.add(seed)

            # 待分配池成员未变化，引力无需重算。

    # --------------------------------------------------------
    # 残局：剩余 unassigned -> island pool
    # --------------------------------------------------------
    island_pool: Set[str] = set(unassigned)

    while island_pool and grids:
        merged_this_round = False

        # 每轮固定快照 + H3 ID 升序
        island_snapshot = sorted(island_pool)

        for island in island_snapshot:
            # 该 island 可能已在本轮前面被吸收
            if island not in island_pool:
                continue

            adjacent_grid_ids: Set[str] = set()

            for nb in neighbor_map[island]:
                if nb in h3_assignment:
                    adjacent_grid_ids.add(
                        h3_assignment[nb]["grid_id"]
                    )

            if not adjacent_grid_ids:
                continue

            island_lat, island_lng = centers[island]

            candidate_grids = []

            for grid_id in adjacent_grid_ids:
                grid = grids[grid_id]

                dist = haversine_distance_m(
                    grid["seed_lat"],
                    grid["seed_lng"],
                    island_lat,
                    island_lng,
                    config.earth_radius_m,
                )

                if dist <= max_seed_radius_m + config.epsilon:
                    candidate_grids.append(
                        (
                            float(dist),
                            str(grid["seed_h3"]),
                            str(grid_id),
                        )
                    )

            if not candidate_grids:
                continue

            # 最近 Seed 优先；
            # 同距离 seed_h3 字典序；
            # 最终 grid_id 字典序。
            candidate_grids.sort()
            dist, _seed_sort, winner_grid_id = candidate_grids[0]

            winner = grids[winner_grid_id]

            winner["h3_set"].add(island)
            winner["grid_fyp"] += float(fyp.get(island, 0.0))
            winner["grid_customer_count"] += int(
                customer_count.get(island, 0)
            )

            h3_assignment[island] = {
                "grid_id": winner_grid_id,
                "city": city,
                "admin_code": admin_code,
                "admin_name": admin_name,
                "h3_id": island,
                "h3_fyp": float(fyp.get(island, 0.0)),
                "h3_customer_count": int(
                    customer_count.get(island, 0)
                ),
                "seed_h3": winner["seed_h3"],
                "distance_to_seed_m": float(dist),
                "assignment_method": "ISLAND_MERGE",
                "assignment_layer": -1,
            }

            island_pool.remove(island)
            merged_this_round = True

        if not merged_this_round:
            break

    # --------------------------------------------------------
    # 最终输出组装
    # --------------------------------------------------------
    grid_rows: List[Dict[str, Any]] = []

    for grid_id in sorted(grids):
        grid = grids[grid_id]
        cells = sorted(grid["h3_set"])
        seed_gcj_lng, seed_gcj_lat = wgs84_to_gcj02(
            grid["seed_lng"],
            grid["seed_lat"],
        )

        max_dist = 0.0
        for cell in cells:
            lat, lng = centers[cell]
            d = haversine_distance_m(
                grid["seed_lat"],
                grid["seed_lng"],
                lat,
                lng,
                config.earth_radius_m,
            )
            max_dist = max(max_dist, d)

        row = {
            "grid_id": grid_id,
            "city": city,
            "admin_code": admin_code,
            "admin_name": admin_name,
            "seed_h3": grid["seed_h3"],
            "seed_lat": float(grid["seed_lat"]),
            "seed_lng": float(grid["seed_lng"]),
            "seed_wgs84_lat": float(grid["seed_lat"]),
            "seed_wgs84_lng": float(grid["seed_lng"]),
            "seed_gcj02_lat": float(seed_gcj_lat),
            "seed_gcj02_lng": float(seed_gcj_lng),
            "h3_count": int(len(cells)),
            "grid_fyp": float(grid["grid_fyp"]),
            "target_expected_fyp": float(target_fyp),
            "grid_customer_count": int(
                grid["grid_customer_count"]
            ),
            "min_customer_count": min_customer_count,
            "customer_count_utilization": _safe_rate(
                int(grid["grid_customer_count"]),
                min_customer_count,
            ),
            "value_utilization": _safe_rate(
                float(grid["grid_fyp"]),
                float(target_fyp),
            ),
            "max_seed_distance_m": float(max_dist),
            "max_seed_radius_m": float(max_seed_radius_m),
            "distance_diameter_km": float(
                max_seed_radius_m * 2 / 1000
            ),
            "expansion_layers": int(
                grid["main_expansion_layers"]
            ),
            "grid_status": "SUCCESS",
        }

        if config.build_grid_geometry:
            try:
                cell_polys = [
                    h3_cell_boundary_polygon(c)
                    for c in cells
                ]
                grid_geom = unary_union(cell_polys)
                row["grid_geometry_geojson"] = json.dumps(
                    mapping(grid_geom),
                    ensure_ascii=False,
                )
                row["grid_geometry_geojson_wgs84"] = row[
                    "grid_geometry_geojson"
                ]
                grid_geom_gcj02 = geometry_to_gcj02(grid_geom)
                row["grid_geometry_geojson_gcj02"] = json.dumps(
                    mapping(grid_geom_gcj02),
                    ensure_ascii=False,
                )
            except Exception as exc:
                warnings.warn(
                    f"grid_id={grid_id} Geometry 输出失败：{exc}"
                )
                row["grid_geometry_geojson"] = None
                row["grid_geometry_geojson_wgs84"] = None
                row["grid_geometry_geojson_gcj02"] = None

        grid_rows.append(row)

    h3_detail_rows = [
        h3_assignment[cell]
        for cell in sorted(h3_assignment)
    ]

    abandoned_rows: List[Dict[str, Any]] = []
    for cell in sorted(island_pool):
        lat, lng = centers[cell]
        gcj_lng, gcj_lat = wgs84_to_gcj02(lng, lat)
        abandoned_rows.append(
            {
                "city": city,
                "admin_code": admin_code,
                "admin_name": admin_name,
                "h3_id": cell,
                "h3_fyp": float(fyp.get(cell, 0.0)),
                "h3_customer_count": int(
                    customer_count.get(cell, 0)
                ),
                "center_lat": float(lat),
                "center_lng": float(lng),
                "center_wgs84_lat": float(lat),
                "center_wgs84_lng": float(lng),
                "center_gcj02_lat": float(gcj_lat),
                "center_gcj02_lng": float(gcj_lng),
                "final_status": "ABANDONED",
            }
        )

    return (
        grid_rows,
        h3_detail_rows,
        failed_rows,
        abandoned_rows,
        global_grid_sequence_start,
    )


# ============================================================
# 9. 全城市运行
# ============================================================

def run_partition_on_h3_pool(
    h3_pool: pd.DataFrame,
    city_params: pd.DataFrame,
    config: AlgorithmConfig,
) -> Tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    """
    按 city × admin_code 串行运行。
    行政街道之间完全隔离，因此天然保证不跨行政街道。
    """

    params = city_params.set_index("city").to_dict("index")

    grid_rows: List[Dict[str, Any]] = []
    detail_rows: List[Dict[str, Any]] = []
    failed_rows: List[Dict[str, Any]] = []
    abandoned_rows: List[Dict[str, Any]] = []

    grid_sequence = 1

    legal_pool = h3_pool[
        h3_pool["is_legal_unassigned"]
    ].copy()

    for (city, admin_code), group in legal_pool.groupby(
        ["city", "admin_code"],
        sort=True,
    ):
        if city not in params:
            raise ValueError(
                f"城市 {city} 缺少距离/FYP 参数。"
            )

        p = params[city]

        sub = group.sort_values(
            "h3_id",
            kind="mergesort",
        ).copy()

        admin_name = (
            str(sub["admin_name"].iloc[0])
            if not sub.empty
            else ""
        )

        (
            g_rows,
            d_rows,
            f_rows,
            a_rows,
            grid_sequence,
        ) = _build_one_admin(
            city=str(city),
            admin_code=str(admin_code),
            admin_name=admin_name,
            admin_pool=sub,
            target_fyp=float(
                p["target_expected_fyp"]
            ),
            max_seed_radius_m=float(
                p["max_seed_radius_m"]
            ),
            config=config,
            global_grid_sequence_start=grid_sequence,
        )

        grid_rows.extend(g_rows)
        detail_rows.extend(d_rows)
        failed_rows.extend(f_rows)
        abandoned_rows.extend(a_rows)

    grids = pd.DataFrame(grid_rows)
    details = pd.DataFrame(detail_rows)
    failed = pd.DataFrame(failed_rows)
    abandoned = pd.DataFrame(abandoned_rows)

    return grids, details, failed, abandoned


# ============================================================
# 10. 客户—专员格明细
# ============================================================

def build_grid_customer_detail(
    customer_diagnostic: pd.DataFrame,
    grids: pd.DataFrame,
    h3_detail: pd.DataFrame,
    cols: ColumnConfig,
) -> pd.DataFrame:
    """
    生成客户维度的完整分析底表，一行对应一条去重后的客户记录。

    所有客户都会保留：
    - 客户所在 H3 最终进入成功专员格时，grid_id 有值；
    - 客户仅成功映射到 H3、但该 H3 未进入成功专员格时，grid_id 为空；
    - 原始客户字段和客户诊断字段继续保留，便于后续分析。

    has_successful_grid 是“客户最终有专员格”的正式判断字段，
    不能用“是否成功生成 h3_id”替代。
    """

    detail = customer_diagnostic.copy()
    detail["_customer_row_order"] = np.arange(len(detail))

    assignment_required = [
        "city",
        "admin_code",
        "admin_name",
        "h3_id",
        "h3_fyp",
        "h3_customer_count",
        "grid_id",
        "seed_h3",
        "distance_to_seed_m",
        "assignment_method",
        "assignment_layer",
    ]

    if h3_detail.empty:
        assignment = pd.DataFrame(
            columns=assignment_required
        )
    else:
        _require_columns(
            h3_detail,
            assignment_required,
            "H3 分配明细",
        )
        assignment = h3_detail[assignment_required].copy()

        duplicated = assignment.duplicated(
            ["city", "h3_id"],
            keep=False,
        )
        if duplicated.any():
            examples = assignment.loc[
                duplicated,
                ["city", "h3_id", "grid_id"],
            ].head(10)
            raise AssertionError(
                "同一 city + h3_id 被分配到多条专员格记录。\n"
                f"示例：\n{examples}"
            )

    assignment = assignment.rename(
        columns={
            "city": "_assignment_city_key",
            "admin_code": "grid_admin_code",
            "admin_name": "grid_admin_name",
            "h3_id": "_assignment_h3_key",
            "h3_fyp": "assigned_h3_fyp",
            "h3_customer_count": "assigned_h3_customer_count",
            "seed_h3": "grid_seed_h3",
            "distance_to_seed_m": "distance_to_grid_seed_m",
            "assignment_method": "h3_assignment_method",
            "assignment_layer": "h3_assignment_layer",
        }
    )

    detail = detail.merge(
        assignment,
        left_on=[cols.customer_city, "_h3_id"],
        right_on=["_assignment_city_key", "_assignment_h3_key"],
        how="left",
        validate="many_to_one",
        sort=False,
    )

    detail = detail.drop(
        columns=[
            "_assignment_city_key",
            "_assignment_h3_key",
        ]
    )

    grid_required = [
        "grid_id",
        "h3_count",
        "grid_fyp",
        "target_expected_fyp",
        "grid_customer_count",
        "min_customer_count",
        "customer_count_utilization",
        "value_utilization",
        "max_seed_distance_m",
        "max_seed_radius_m",
        "distance_diameter_km",
        "expansion_layers",
        "grid_status",
    ]

    if grids.empty:
        grid_lookup = pd.DataFrame(columns=grid_required)
    else:
        _require_columns(grids, grid_required, "Grid 主表")
        grid_lookup = grids[grid_required].copy()

        if grid_lookup["grid_id"].duplicated().any():
            examples = grid_lookup.loc[
                grid_lookup["grid_id"].duplicated(keep=False),
                "grid_id",
            ].head(10).tolist()
            raise AssertionError(
                f"Grid 主表存在重复 grid_id，示例：{examples}"
            )

    grid_lookup = grid_lookup.rename(
        columns={
            "expansion_layers": "grid_expansion_layers",
        }
    )

    detail = detail.merge(
        grid_lookup,
        on="grid_id",
        how="left",
        validate="many_to_one",
        sort=False,
    )

    # 提供一组稳定的标准字段，同时继续保留原始中文字段和诊断字段。
    detail["customer_id"] = detail[cols.customer_id]
    detail["city"] = detail[cols.customer_city]
    detail["customer_lng"] = detail["_lng"]
    detail["customer_lat"] = detail["_lat"]
    detail["customer_coordinate_system"] = detail[
        "_input_coordinate_system"
    ]
    detail["customer_wgs84_lng"] = detail["_wgs84_lng"]
    detail["customer_wgs84_lat"] = detail["_wgs84_lat"]
    detail["expected_fyp"] = detail["_fyp"]
    detail["h3_id"] = detail["_h3_id"]

    if cols.customer_admin_code in detail.columns:
        detail["customer_admin_name"] = detail[
            cols.customer_admin_code
        ]
    else:
        detail["customer_admin_name"] = pd.NA

    # h3_assigned_to_grid 表示空间 H3 已进入成功网格；
    # has_successful_grid 进一步要求该客户符合最低人数统计口径。
    detail["h3_assigned_to_grid"] = detail["grid_id"].notna()
    detail["has_successful_grid"] = (
        detail["h3_assigned_to_grid"]
        & detail["_legal_customer_for_count"]
    )
    detail["counts_toward_grid_customer_minimum"] = detail[
        "has_successful_grid"
    ]
    detail["grid_assignment_status"] = np.select(
        [
            detail["has_successful_grid"],
            detail["h3_assigned_to_grid"],
        ],
        [
            "ASSIGNED",
            "H3_ASSIGNED_BUT_CUSTOMER_INELIGIBLE",
        ],
        default="UNASSIGNED",
    )

    detail = detail.sort_values(
        "_customer_row_order",
        kind="mergesort",
    ).drop(columns=["_customer_row_order"])

    # 将业务最常用字段放到最前面，其余原始/诊断字段继续保留。
    preferred_columns = [
        "customer_id",
        "city",
        "customer_lng",
        "customer_lat",
        "customer_coordinate_system",
        "customer_wgs84_lng",
        "customer_wgs84_lat",
        "expected_fyp",
        "customer_admin_name",
        "h3_id",
        "_h3_admin_code",
        "_h3_admin_name",
        "grid_id",
        "grid_admin_code",
        "grid_admin_name",
        "h3_assigned_to_grid",
        "has_successful_grid",
        "counts_toward_grid_customer_minimum",
        "grid_assignment_status",
        "assigned_h3_fyp",
        "assigned_h3_customer_count",
        "grid_seed_h3",
        "distance_to_grid_seed_m",
        "h3_assignment_method",
        "h3_assignment_layer",
        "h3_count",
        "grid_fyp",
        "target_expected_fyp",
        "grid_customer_count",
        "min_customer_count",
        "customer_count_utilization",
        "value_utilization",
        "max_seed_distance_m",
        "max_seed_radius_m",
        "distance_diameter_km",
        "grid_expansion_layers",
        "grid_status",
        "_customer_id_available",
        "_valid_coordinate",
        "_input_coordinate_system",
        "_wgs84_lng",
        "_wgs84_lat",
        "_fyp_available",
        "_h3_in_admin_geometry",
        "_admin_consistent",
        "_excluded_by_existing_grid",
        "_legal_customer_for_count",
        "_legal_h3_candidate",
    ]

    front: List[str] = []
    for column in preferred_columns:
        if column in detail.columns and column not in front:
            front.append(column)

    remaining = [
        column for column in detail.columns
        if column not in front
    ]

    return detail[front + remaining].reset_index(drop=True)


# ============================================================
# 11. 覆盖率漏斗
# ============================================================

def build_coverage_metrics(
    customer_diagnostic: pd.DataFrame,
    h3_detail: pd.DataFrame,
    cols: ColumnConfig,
) -> pd.DataFrame:
    """
    输出业务可以直接看的覆盖率漏斗。

    重点区分：
    1. 数据问题造成的损失；
    2. 已有网格造成的业务排除；
    3. 真正进入算法后仍未形成卫星单元的损失。
    """

    cust = customer_diagnostic.copy()

    assigned_h3_by_city: Set[Tuple[str, str]] = set()

    if not h3_detail.empty:
        assigned_h3_by_city = set(
            zip(
                h3_detail["city"].astype(str),
                h3_detail["h3_id"].astype(str),
            )
        )

    cust["_final_grid_assigned"] = [
        (
            str(city),
            str(cell),
        ) in assigned_h3_by_city
        if pd.notna(cell)
        else False
        for city, cell in zip(
            cust[cols.customer_city],
            cust["_h3_id"],
        )
    ]

    rows = []

    for city, g in cust.groupby(
        cols.customer_city,
        sort=True,
    ):
        target_mask = g["_customer_id_available"]
        total_n = int(target_mask.sum())
        missing_customer_id_n = int((~target_mask).sum())

        valid_coord_n = int(
            (target_mask & g["_valid_coordinate"]).sum()
        )
        fyp_available_n = int(
            (target_mask & g["_fyp_available"]).sum()
        )
        h3_admin_n = int(
            (target_mask & g["_h3_in_admin_geometry"]).sum()
        )
        admin_consistent_n = int(
            (target_mask & g["_admin_consistent"]).sum()
        )
        existing_excluded_n = int(
            (target_mask & g["_excluded_by_existing_grid"]).sum()
        )
        legal_candidate_n = int(
            g["_legal_customer_for_count"].sum()
        )
        final_assigned_n = int(
            (
                g["_final_grid_assigned"]
                & g["_legal_customer_for_count"]
            ).sum()
        )

        fyp_available_mask = target_mask & g["_fyp_available"]
        total_available_fyp = float(
            g.loc[fyp_available_mask, "_fyp"].sum()
        )

        legal_candidate_fyp = float(
            g.loc[
                g["_legal_h3_candidate"],
                "_fyp",
            ].sum()
        )

        final_assigned_fyp = float(
            g.loc[
                g["_final_grid_assigned"]
                & g["_legal_h3_candidate"],
                "_fyp",
            ].sum()
        )

        rows.append(
            {
                "city": city,

                "target_customer_count": total_n,
                "missing_customer_id_record_count": (
                    missing_customer_id_n
                ),

                "valid_coordinate_count": valid_coord_n,
                "valid_coordinate_rate": _safe_rate(
                    valid_coord_n,
                    total_n,
                ),

                "fyp_available_count": fyp_available_n,
                "fyp_available_rate": _safe_rate(
                    fyp_available_n,
                    total_n,
                ),

                "admin_geometry_matched_count": h3_admin_n,
                "admin_geometry_matched_rate": _safe_rate(
                    h3_admin_n,
                    total_n,
                ),

                "customer_admin_consistent_count": admin_consistent_n,
                "customer_admin_consistent_rate": _safe_rate(
                    admin_consistent_n,
                    total_n,
                ),

                "existing_grid_excluded_count": existing_excluded_n,
                "existing_grid_excluded_rate": _safe_rate(
                    existing_excluded_n,
                    total_n,
                ),

                "legal_candidate_customer_count": legal_candidate_n,
                "legal_candidate_customer_rate": _safe_rate(
                    legal_candidate_n,
                    total_n,
                ),

                "final_satellite_grid_customer_count": final_assigned_n,

                # 面向全量目标客群
                "overall_customer_coverage_rate": _safe_rate(
                    final_assigned_n,
                    total_n,
                ),

                # 面向真正进入算法候选池的客户
                "algorithm_candidate_coverage_rate": _safe_rate(
                    final_assigned_n,
                    legal_candidate_n,
                ),

                "available_expected_fyp_total": total_available_fyp,
                "legal_candidate_expected_fyp": legal_candidate_fyp,
                "final_satellite_grid_expected_fyp": final_assigned_fyp,

                # 价值覆盖率分母使用“已有 expected_fyp 的目标客户价值”
                "overall_value_coverage_rate": _safe_rate(
                    final_assigned_fyp,
                    total_available_fyp,
                ),

                # 真正进入算法候选空间后的价值覆盖率
                "algorithm_candidate_value_coverage_rate": _safe_rate(
                    final_assigned_fyp,
                    legal_candidate_fyp,
                ),
            }
        )

    return pd.DataFrame(rows)


# ============================================================
# 12. 最终守恒 / 一致性校验
# ============================================================

def validate_final_results(
    h3_pool: pd.DataFrame,
    grids: pd.DataFrame,
    h3_detail: pd.DataFrame,
    abandoned_h3: pd.DataFrame,
    config: AlgorithmConfig,
) -> None:
    """
    研发验收基准。

    这里只校验“合法待分配 H3”：
        最终已分配 + 最终废弃 = 初始合法待分配 H3
    """

    legal = h3_pool[
        h3_pool["is_legal_unassigned"]
    ].copy()

    legal_keys = set(
        zip(
            legal["city"].astype(str),
            legal["h3_id"].astype(str),
        )
    )

    assigned_keys: Set[Tuple[str, str]] = set()
    if not h3_detail.empty:
        assigned_keys = set(
            zip(
                h3_detail["city"].astype(str),
                h3_detail["h3_id"].astype(str),
            )
        )

    abandoned_keys: Set[Tuple[str, str]] = set()
    if not abandoned_h3.empty:
        abandoned_keys = set(
            zip(
                abandoned_h3["city"].astype(str),
                abandoned_h3["h3_id"].astype(str),
            )
        )

    if assigned_keys & abandoned_keys:
        overlap = list(assigned_keys & abandoned_keys)[:10]
        raise AssertionError(
            f"H3 同时出现在已分配与废弃集合：{overlap}"
        )

    final_keys = assigned_keys | abandoned_keys

    if final_keys != legal_keys:
        missing = list(legal_keys - final_keys)[:10]
        extra = list(final_keys - legal_keys)[:10]
        raise AssertionError(
            "H3 数量守恒失败。\n"
            f"缺失示例={missing}\n"
            f"额外示例={extra}"
        )

    # 价值守恒
    initial_fyp = float(
        legal["h3_expected_fyp"].sum()
    )

    assigned_fyp = (
        float(h3_detail["h3_fyp"].sum())
        if not h3_detail.empty
        else 0.0
    )

    abandoned_fyp = (
        float(abandoned_h3["h3_fyp"].sum())
        if not abandoned_h3.empty
        else 0.0
    )

    if not math.isclose(
        initial_fyp,
        assigned_fyp + abandoned_fyp,
        rel_tol=1e-9,
        abs_tol=1e-6,
    ):
        raise AssertionError(
            "FYP 价值守恒失败："
            f"initial={initial_fyp}, "
            f"assigned={assigned_fyp}, "
            f"abandoned={abandoned_fyp}"
        )

    # 去重客户数守恒。customer_id 已在预处理阶段保证全局唯一，
    # 因此各 H3 客户数可直接求和。
    initial_customer_count = int(
        legal["h3_customer_count"].sum()
    )
    assigned_customer_count = (
        int(h3_detail["h3_customer_count"].sum())
        if not h3_detail.empty
        else 0
    )
    abandoned_customer_count = (
        int(abandoned_h3["h3_customer_count"].sum())
        if not abandoned_h3.empty
        else 0
    )

    if (
        initial_customer_count
        != assigned_customer_count + abandoned_customer_count
    ):
        raise AssertionError(
            "客户数守恒失败："
            f"initial={initial_customer_count}, "
            f"assigned={assigned_customer_count}, "
            f"abandoned={abandoned_customer_count}"
        )

    # 网格距离 / 连通性
    if grids.empty:
        return

    detail_by_grid = {
        grid_id: set(g["h3_id"].tolist())
        for grid_id, g in h3_detail.groupby("grid_id")
    }
    detail_customer_count_by_grid = {
        grid_id: int(g["h3_customer_count"].sum())
        for grid_id, g in h3_detail.groupby("grid_id")
    }
    detail_fyp_by_grid = {
        grid_id: float(g["h3_fyp"].sum())
        for grid_id, g in h3_detail.groupby("grid_id")
    }

    for row in grids.itertuples(index=False):
        grid_id = row.grid_id
        cells = detail_by_grid.get(grid_id, set())

        if not cells:
            raise AssertionError(
                f"grid_id={grid_id} 没有 H3 明细。"
            )

        detail_customer_count = detail_customer_count_by_grid.get(
            grid_id,
            0,
        )
        if int(row.grid_customer_count) != detail_customer_count:
            raise AssertionError(
                f"grid_id={grid_id} 客户数与 H3 明细不一致："
                f"grid={row.grid_customer_count}, "
                f"detail={detail_customer_count}"
            )

        detail_fyp = detail_fyp_by_grid.get(grid_id, 0.0)
        if not math.isclose(
            float(row.grid_fyp),
            detail_fyp,
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise AssertionError(
                f"grid_id={grid_id} FYP 与 H3 明细不一致："
                f"grid={row.grid_fyp}, detail={detail_fyp}"
            )

        if (
            float(row.grid_fyp) + config.epsilon
            < float(row.target_expected_fyp)
        ):
            raise AssertionError(
                f"grid_id={grid_id} 未达到最低 FYP。"
            )

        if (
            int(row.grid_customer_count)
            < int(row.min_customer_count)
        ):
            raise AssertionError(
                f"grid_id={grid_id} 未达到最低客户数。"
            )

        if int(row.min_customer_count) != int(config.min_customer_count):
            raise AssertionError(
                f"grid_id={grid_id} 的最低客户数参数与运行配置不一致。"
            )

        # 距离
        if (
            float(row.max_seed_distance_m)
            > float(row.max_seed_radius_m)
            + config.epsilon
        ):
            raise AssertionError(
                f"grid_id={grid_id} 超出 Seed 最大覆盖半径。"
            )

        # 连通性：最终 Grid 内部 BFS
        seed = row.seed_h3
        if seed not in cells:
            raise AssertionError(
                f"grid_id={grid_id} 的 seed_h3 不在本 Grid 内。"
            )

        visited = {seed}
        queue = [seed]

        while queue:
            cell = queue.pop(0)
            for nb in h3_neighbors(cell):
                if nb in cells and nb not in visited:
                    visited.add(nb)
                    queue.append(nb)

        if visited != cells:
            disconnected = list(cells - visited)[:10]
            raise AssertionError(
                f"grid_id={grid_id} 存在拓扑断裂，示例：{disconnected}"
            )


# ============================================================
# 13. 一键运行入口
# ============================================================

def run_satellite_grid_algorithm(
    customer_df: pd.DataFrame,
    admin_df: pd.DataFrame,
    existing_grid_df: pd.DataFrame,
    fyp_threshold_df: pd.DataFrame,
    distance_df: pd.DataFrame,
    cols: Optional[ColumnConfig] = None,
    config: Optional[AlgorithmConfig] = None,
) -> AlgorithmResult:
    """
    一键运行完整算法。

    输入
    ----
    customer_df
        客户表：
        customer_id, city, lng, lat, area_admin_code, expected_fyp

    admin_df
        行政街道表：
        city, area_code, area_name, area_geometry

    existing_grid_df
        已有基础网格表：
        city, agent_net_id, basic_net_id, basic_net_geom

    fyp_threshold_df
        城市建格最低 FYP：
        city, target_expected_fyp

    distance_df
        城市距离表：
        city, distance_km

        注意：
        distance_km 是“直径/最大跨度”，算法内部自动 / 2。

    返回
    ----
    AlgorithmResult
    """

    cols = cols or ColumnConfig()
    config = config or AlgorithmConfig()

    try:
        min_customer_count_value = float(config.min_customer_count)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "min_customer_count 必须是正整数。"
        ) from exc

    if (
        not math.isfinite(min_customer_count_value)
        or min_customer_count_value <= 0
        or not min_customer_count_value.is_integer()
    ):
        raise ValueError("min_customer_count 必须是正整数。")

    # 提前校验坐标系配置。当前 H3 内部统一使用 WGS84。
    normalize_coordinate_system(config.input_coordinate_system)

    # 1. 城市参数
    city_params = prepare_city_parameters(
        fyp_threshold_df,
        distance_df,
        cols,
    )

    # 2. 行政街道 Geometry
    admin_boundaries = prepare_admin_boundaries(
        admin_df,
        cols,
        config,
    )

    # 3. 已有基础网格 Union
    occupied_by_city = prepare_existing_occupied_area(
        existing_grid_df,
        cols,
        config,
    )

    # 4. 行政街道铺满 H3 + 已有区域排除
    h3_pool = build_admin_h3_pool(
        admin_boundaries,
        occupied_by_city,
        config,
    )

    # 参数城市完整性检查
    required_cities = set(h3_pool["city"].astype(str))
    param_cities = set(city_params["city"].astype(str))

    missing_param_cities = sorted(
        required_cities - param_cities
    )

    if missing_param_cities:
        raise ValueError(
            "以下行政街道城市缺少城市距离/FYP参数："
            f"{missing_param_cities}"
        )

    # 5. 客户 -> H3 + FYP 聚合
    customer_diagnostic, h3_pool = (
        prepare_customers_and_attach_fyp(
            customer_df,
            h3_pool,
            cols,
            config,
        )
    )

    # 6. 主算法
    grids, h3_detail, failed, abandoned = (
        run_partition_on_h3_pool(
            h3_pool,
            city_params,
            config,
        )
    )

    # 7. 客户—专员格明细
    grid_customer_detail = build_grid_customer_detail(
        customer_diagnostic,
        grids,
        h3_detail,
        cols,
    )

    # 8. 覆盖率
    coverage = build_coverage_metrics(
        customer_diagnostic,
        h3_detail,
        cols,
    )

    # 9. 守恒 / 一致性
    validate_final_results(
        h3_pool,
        grids,
        h3_detail,
        abandoned,
        config,
    )

    return AlgorithmResult(
        grids=grids,
        h3_detail=h3_detail,
        grid_customer_detail=grid_customer_detail,
        failed_seeds=failed,
        abandoned_h3=abandoned,
        coverage_metrics=coverage,
        h3_pool=h3_pool,
        customer_diagnostic=customer_diagnostic,
    )


# ============================================================
# 14. 文件读取 / 输出辅助
# ============================================================

def load_table(path: str | Path) -> pd.DataFrame:
    """
    支持 csv / xlsx / xls / parquet / pkl。
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()

    if suffix == ".csv":
        return pd.read_csv(path)

    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path)

    if suffix == ".parquet":
        return pd.read_parquet(path)

    if suffix in (".pkl", ".pickle"):
        return pd.read_pickle(path)

    raise ValueError(
        f"不支持的文件格式：{suffix}。"
        "请使用 csv / xlsx / parquet / pkl。"
    )


def save_result_csv(
    result: AlgorithmResult,
    output_dir: str | Path,
) -> None:
    """
    将结果保存为 UTF-8-SIG CSV，方便 Windows / WPS / Excel 直接打开中文。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    tables = {
        "01_grid_level.csv": result.grids,
        "02_h3_detail.csv": result.h3_detail,
        "03_failed_seeds.csv": result.failed_seeds,
        "04_abandoned_h3.csv": result.abandoned_h3,
        "05_coverage_metrics.csv": result.coverage_metrics,
        "06_h3_pool_debug.csv": result.h3_pool,
        "07_customer_diagnostic.csv": result.customer_diagnostic,
        "08_grid_customer_detail.csv": result.grid_customer_detail,
    }

    for filename, df in tables.items():
        df.to_csv(
            output_dir / filename,
            index=False,
            encoding="utf-8-sig",
        )


# ============================================================
# 15. 可选：直接从文件运行
# ============================================================

# ------------------------------------------------------------------
# 如果你准备直接 python satellite_grid_partition_v1.py，
# 把下面路径修改为真实文件，然后将 RUN_FROM_FILES 改为 True。
# ------------------------------------------------------------------

RUN_FROM_FILES = False

INPUT_FILES = {
    "customer": "./customers.csv",
    "admin": "./admin_boundaries.csv",
    "existing_grid": "./existing_basic_grids.csv",
    "fyp_threshold": "./city_fyp_threshold.csv",
    "distance": "./city_distance.csv",
}

OUTPUT_DIR = "./satellite_grid_output"


def main() -> None:
    if not RUN_FROM_FILES:
        print(
            "\n当前 RUN_FROM_FILES=False。\n\n"
            "推荐在 Notebook 中：\n"
            "    from satellite_grid_partition_v1 import run_satellite_grid_algorithm\n"
            "    result = run_satellite_grid_algorithm(...)\n\n"
            "如果要直接运行本 py 文件，请：\n"
            "1. 修改文件底部 INPUT_FILES；\n"
            "2. 将 RUN_FROM_FILES = True；\n"
            "3. 执行 python satellite_grid_partition_v1.py\n"
        )
        return

    print("1/7 读取输入数据...")

    customer_df = load_table(
        INPUT_FILES["customer"]
    )
    admin_df = load_table(
        INPUT_FILES["admin"]
    )
    existing_grid_df = load_table(
        INPUT_FILES["existing_grid"]
    )
    fyp_threshold_df = load_table(
        INPUT_FILES["fyp_threshold"]
    )
    distance_df = load_table(
        INPUT_FILES["distance"]
    )

    print("2/7 启动卫星网点划分算法...")

    result = run_satellite_grid_algorithm(
        customer_df=customer_df,
        admin_df=admin_df,
        existing_grid_df=existing_grid_df,
        fyp_threshold_df=fyp_threshold_df,
        distance_df=distance_df,
    )

    print("3/7 网格划分完成。")
    print(
        f"成功 Grid 数：{len(result.grids):,}"
    )
    print(
        f"成功分配 H3 数：{len(result.h3_detail):,}"
    )
    print(
        f"失败 Seed 次数：{len(result.failed_seeds):,}"
    )
    print(
        f"最终废弃 H3 数：{len(result.abandoned_h3):,}"
    )

    print("4/7 输出覆盖率指标...")
    if not result.coverage_metrics.empty:
        print(result.coverage_metrics.to_string(index=False))

    print("5/7 保存结果 CSV...")
    save_result_csv(
        result,
        OUTPUT_DIR,
    )

    print("6/7 守恒与连通性校验已通过。")

    print(
        f"7/7 完成。输出目录：{Path(OUTPUT_DIR).resolve()}"
    )


if __name__ == "__main__":
    main()
