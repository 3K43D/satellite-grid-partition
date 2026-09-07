# -*- coding: utf-8 -*-
"""
批量估算 GCJ-02 客户坐标对应的 WGS84 坐标。

重要说明
--------
1. 本文件完全独立，不调用 satellite_grid_partition_v1.py。
2. 不调用高德 API，不访问外网。
3. 使用的是社区广泛流传的 GCJ-02 正向近似模型，并用固定点迭代反求 WGS84。
4. 输出的 WGS84 是“公开模型下的估计值”，不是高德官方提供或认证的反向结果。
5. model_roundtrip_error_m 只表示公开模型内部的回算残差很小，不能证明
   wgs84_lng_est / wgs84_lat_est 就是真实原始 WGS84。
6. DataFrame 中的坐标反算、回算验证和距离计算均使用 NumPy 向量化执行。

Notebook 示例
-------------
import pandas as pd
from gcj02_to_wgs84_batch import convert_customer_coordinates

customer_df = pd.read_excel("客户数据.xlsx")

result = convert_customer_coordinates(
    customer_df,
    customer_id_col="客户号",
    gcj_lng_col="客户经度",
    gcj_lat_col="客户纬度",
)

result.head()
result.to_parquet("客户坐标_GCJ与WGS估算对照.parquet", index=False)
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd


_PI = math.pi
_A = 6378245.0
_EE = 0.00669342162296594323
_EARTH_RADIUS_M = 6_371_008.8


def _outside_gcj02_area(lng: float, lat: float) -> bool:
    """社区模型常用的 GCJ-02 适用范围判断。"""
    return not (
        72.004 <= float(lng) <= 137.8347
        and 0.8293 <= float(lat) <= 55.8271
    )


def _transform_lat(lng_offset: float, lat_offset: float) -> float:
    value = (
        -100.0
        + 2.0 * lng_offset
        + 3.0 * lat_offset
        + 0.2 * lat_offset * lat_offset
        + 0.1 * lng_offset * lat_offset
        + 0.2 * math.sqrt(abs(lng_offset))
    )
    value += (
        20.0 * math.sin(6.0 * lng_offset * _PI)
        + 20.0 * math.sin(2.0 * lng_offset * _PI)
    ) * 2.0 / 3.0
    value += (
        20.0 * math.sin(lat_offset * _PI)
        + 40.0 * math.sin(lat_offset / 3.0 * _PI)
    ) * 2.0 / 3.0
    value += (
        160.0 * math.sin(lat_offset / 12.0 * _PI)
        + 320.0 * math.sin(lat_offset * _PI / 30.0)
    ) * 2.0 / 3.0
    return value


def _transform_lng(lng_offset: float, lat_offset: float) -> float:
    value = (
        300.0
        + lng_offset
        + 2.0 * lat_offset
        + 0.1 * lng_offset * lng_offset
        + 0.1 * lng_offset * lat_offset
        + 0.1 * math.sqrt(abs(lng_offset))
    )
    value += (
        20.0 * math.sin(6.0 * lng_offset * _PI)
        + 20.0 * math.sin(2.0 * lng_offset * _PI)
    ) * 2.0 / 3.0
    value += (
        20.0 * math.sin(lng_offset * _PI)
        + 40.0 * math.sin(lng_offset / 3.0 * _PI)
    ) * 2.0 / 3.0
    value += (
        150.0 * math.sin(lng_offset / 12.0 * _PI)
        + 300.0 * math.sin(lng_offset / 30.0 * _PI)
    ) * 2.0 / 3.0
    return value


def wgs84_to_gcj02(lng: float, lat: float) -> Tuple[float, float]:
    """
    使用公开近似模型将 WGS84 正向转换为 GCJ-02。

    返回顺序始终为 (经度, 纬度)。
    """
    lng = float(lng)
    lat = float(lat)

    if _outside_gcj02_area(lng, lat):
        return lng, lat

    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)

    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1.0 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)

    dlat = (
        dlat
        * 180.0
        / ((_A * (1.0 - _EE)) / (magic * sqrt_magic) * _PI)
    )
    dlng = (
        dlng
        * 180.0
        / (_A / sqrt_magic * math.cos(radlat) * _PI)
    )

    return lng + dlng, lat + dlat


def gcj02_to_wgs84(
    lng: float,
    lat: float,
    *,
    tolerance: float = 1e-7,
    max_iterations: int = 10,
) -> Tuple[float, float]:
    """
    使用固定点迭代反求公开正向模型对应的 WGS84 估计值。

    参数
    ----
    lng, lat
        GCJ-02 经度和纬度。
    tolerance
        正向回算到输入 GCJ-02 后允许的角度残差。
    max_iterations
        最大迭代次数。

    返回
    ----
    (wgs84_lng_est, wgs84_lat_est)
        公开模型下的 WGS84 估计经纬度。
    """
    lng = float(lng)
    lat = float(lat)

    if _outside_gcj02_area(lng, lat):
        return lng, lat

    wgs_lng = lng
    wgs_lat = lat

    for _ in range(max_iterations):
        predicted_gcj_lng, predicted_gcj_lat = wgs84_to_gcj02(
            wgs_lng,
            wgs_lat,
        )
        delta_lng = lng - predicted_gcj_lng
        delta_lat = lat - predicted_gcj_lat
        wgs_lng += delta_lng
        wgs_lat += delta_lat

        if (
            abs(delta_lng) <= tolerance
            and abs(delta_lat) <= tolerance
        ):
            break

    return wgs_lng, wgs_lat


def wgs84_to_gcj02_array(
    lng: np.ndarray,
    lat: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """NumPy 向量化的 WGS84 -> GCJ-02，返回顺序为（经度，纬度）。"""
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
        20.0 * np.sin(6.0 * lng_offset * _PI)
        + 20.0 * np.sin(2.0 * lng_offset * _PI)
    ) * 2.0 / 3.0
    dlat += (
        20.0 * np.sin(lat_offset * _PI)
        + 40.0 * np.sin(lat_offset / 3.0 * _PI)
    ) * 2.0 / 3.0
    dlat += (
        160.0 * np.sin(lat_offset / 12.0 * _PI)
        + 320.0 * np.sin(lat_offset * _PI / 30.0)
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
        20.0 * np.sin(6.0 * lng_offset * _PI)
        + 20.0 * np.sin(2.0 * lng_offset * _PI)
    ) * 2.0 / 3.0
    dlng += (
        20.0 * np.sin(lng_offset * _PI)
        + 40.0 * np.sin(lng_offset / 3.0 * _PI)
    ) * 2.0 / 3.0
    dlng += (
        150.0 * np.sin(lng_offset / 12.0 * _PI)
        + 300.0 * np.sin(lng_offset / 30.0 * _PI)
    ) * 2.0 / 3.0

    radlat = np.deg2rad(current_lat)
    magic = np.sin(radlat)
    magic = 1.0 - _EE * magic * magic
    sqrt_magic = np.sqrt(magic)
    dlat = (
        dlat
        * 180.0
        / ((_A * (1.0 - _EE)) / (magic * sqrt_magic) * _PI)
    )
    dlng = (
        dlng
        * 180.0
        / (_A / sqrt_magic * np.cos(radlat) * _PI)
    )

    output_lng[inside] = current_lng + dlng
    output_lat[inside] = current_lat + dlat
    return output_lng, output_lat


def gcj02_to_wgs84_array(
    lng: np.ndarray,
    lat: np.ndarray,
    *,
    tolerance: float = 1e-7,
    max_iterations: int = 10,
) -> Tuple[np.ndarray, np.ndarray]:
    """NumPy 向量化的 GCJ-02 -> WGS84 固定点迭代反算。"""
    if tolerance <= 0:
        raise ValueError("tolerance 必须 > 0。")
    if max_iterations <= 0:
        raise ValueError("max_iterations 必须是正整数。")

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

        predicted_lng, predicted_lat = wgs84_to_gcj02_array(
            wgs_lng[active_positions],
            wgs_lat[active_positions],
        )
        delta_lng = original_lng[active_positions] - predicted_lng
        delta_lat = original_lat[active_positions] - predicted_lat
        wgs_lng[active_positions] += delta_lng
        wgs_lat[active_positions] += delta_lat

        converged = (
            (np.abs(delta_lng) <= tolerance)
            & (np.abs(delta_lat) <= tolerance)
        )
        active[active_positions[converged]] = False

    return wgs_lng, wgs_lat


def _haversine_distance_m(
    lng1: float,
    lat1: float,
    lng2: float,
    lat2: float,
) -> float:
    """计算两个经纬度数值之间的大圆距离，单位米。"""
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lng2) - float(lng1))

    value = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(dlambda / 2.0) ** 2
    )
    value = min(1.0, max(0.0, value))
    return 2.0 * _EARTH_RADIUS_M * math.atan2(
        math.sqrt(value),
        math.sqrt(1.0 - value),
    )


def _haversine_distance_m_array(
    lng1: np.ndarray,
    lat1: np.ndarray,
    lng2: np.ndarray,
    lat2: np.ndarray,
) -> np.ndarray:
    """向量化计算两组经纬度数值之间的大圆距离，单位米。"""
    lng1_array, lat1_array, lng2_array, lat2_array = np.broadcast_arrays(
        np.asarray(lng1, dtype=np.float64),
        np.asarray(lat1, dtype=np.float64),
        np.asarray(lng2, dtype=np.float64),
        np.asarray(lat2, dtype=np.float64),
    )
    phi1 = np.deg2rad(lat1_array)
    phi2 = np.deg2rad(lat2_array)
    dphi = np.deg2rad(lat2_array - lat1_array)
    dlambda = np.deg2rad(lng2_array - lng1_array)
    value = (
        np.sin(dphi / 2.0) ** 2
        + np.cos(phi1)
        * np.cos(phi2)
        * np.sin(dlambda / 2.0) ** 2
    )
    value = np.clip(value, 0.0, 1.0)
    return 2.0 * _EARTH_RADIUS_M * np.arctan2(
        np.sqrt(value),
        np.sqrt(1.0 - value),
    )


def convert_customer_coordinates(
    customer_df: pd.DataFrame,
    *,
    customer_id_col: str = "customer_id",
    gcj_lng_col: str = "lng",
    gcj_lat_col: str = "lat",
    tolerance: float = 1e-7,
    max_iterations: int = 10,
) -> pd.DataFrame:
    """
    用 NumPy 向量化批量转换客户 DataFrame，并保留全部原始字段。

    新增字段
    --------
    gcj02_lng, gcj02_lat
        成功转成数值后的原始 GCJ-02 坐标。
    wgs84_lng_est, wgs84_lat_est
        使用公开模型反算的 WGS84 估计值。
    gcj_wgs_numeric_shift_m
        把两组数字当经纬度计算出的数值位移，仅用于直观看偏移量。
    roundtrip_gcj02_lng, roundtrip_gcj02_lat
        将 WGS84 估计值再次用同一公开模型正向转换得到的 GCJ-02。
    model_roundtrip_error_m
        正向回算结果与原 GCJ-02 的残差；它只衡量模型内部收敛程度。
    conversion_status
        ESTIMATED / OUTSIDE_MODEL_AREA_UNCHANGED / INVALID_COORDINATE。
    """
    required = [customer_id_col, gcj_lng_col, gcj_lat_col]
    missing = [column for column in required if column not in customer_df.columns]
    if missing:
        raise ValueError(
            f"输入 DataFrame 缺少字段：{missing}。"
            f"当前字段：{list(customer_df.columns)}"
        )

    if tolerance <= 0:
        raise ValueError("tolerance 必须 > 0。")
    if max_iterations <= 0:
        raise ValueError("max_iterations 必须是正整数。")

    result = customer_df.copy()
    result["gcj02_lng"] = pd.to_numeric(
        result[gcj_lng_col],
        errors="coerce",
    )
    result["gcj02_lat"] = pd.to_numeric(
        result[gcj_lat_col],
        errors="coerce",
    )

    valid = (
        result["gcj02_lng"].between(-180, 180)
        & result["gcj02_lat"].between(-90, 90)
    )

    valid_mask = valid.to_numpy(dtype=bool)
    gcj_lng_all = result["gcj02_lng"].to_numpy(dtype=np.float64)
    gcj_lat_all = result["gcj02_lat"].to_numpy(dtype=np.float64)
    valid_lng = gcj_lng_all[valid_mask]
    valid_lat = gcj_lat_all[valid_mask]

    wgs_lng, wgs_lat = gcj02_to_wgs84_array(
        valid_lng,
        valid_lat,
        tolerance=tolerance,
        max_iterations=max_iterations,
    )
    roundtrip_lng, roundtrip_lat = wgs84_to_gcj02_array(
        wgs_lng,
        wgs_lat,
    )

    def expand_valid(values: np.ndarray) -> np.ndarray:
        expanded = np.full(len(result), np.nan, dtype=np.float64)
        expanded[valid_mask] = values
        return expanded

    result["wgs84_lng_est"] = expand_valid(wgs_lng)
    result["wgs84_lat_est"] = expand_valid(wgs_lat)
    result["gcj_wgs_numeric_shift_m"] = expand_valid(
        _haversine_distance_m_array(
            valid_lng,
            valid_lat,
            wgs_lng,
            wgs_lat,
        )
    )
    result["roundtrip_gcj02_lng"] = expand_valid(roundtrip_lng)
    result["roundtrip_gcj02_lat"] = expand_valid(roundtrip_lat)
    result["model_roundtrip_error_m"] = expand_valid(
        _haversine_distance_m_array(
            valid_lng,
            valid_lat,
            roundtrip_lng,
            roundtrip_lat,
        )
    )

    status = np.full(len(result), "INVALID_COORDINATE", dtype=object)
    inside_model_area = (
        (valid_lng >= 72.004)
        & (valid_lng <= 137.8347)
        & (valid_lat >= 0.8293)
        & (valid_lat <= 55.8271)
    )
    status[valid_mask] = np.where(
        inside_model_area,
        "ESTIMATED",
        "OUTSIDE_MODEL_AREA_UNCHANGED",
    )
    result["conversion_status"] = status

    return result


def _read_table(path: str | Path) -> pd.DataFrame:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(source)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(source)
    if suffix == ".parquet":
        return pd.read_parquet(source)
    raise ValueError("输入文件仅支持 CSV、XLSX、XLS 或 Parquet。")


def _write_table(table: pd.DataFrame, path: str | Path) -> None:
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix == ".csv":
        table.to_csv(target, index=False, encoding="utf-8-sig")
        return
    if suffix in {".xlsx", ".xls"}:
        table.to_excel(target, index=False)
        return
    if suffix == ".parquet":
        table.to_parquet(target, index=False)
        return
    raise ValueError("输出文件仅支持 CSV、XLSX、XLS 或 Parquet。")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="批量估算客户 GCJ-02 坐标对应的 WGS84。",
    )
    parser.add_argument("--input", required=True, help="输入客户文件")
    parser.add_argument("--output", required=True, help="输出对照文件")
    parser.add_argument("--customer-id-col", default="customer_id")
    parser.add_argument("--gcj-lng-col", default="lng")
    parser.add_argument("--gcj-lat-col", default="lat")
    parser.add_argument("--tolerance", type=float, default=1e-7)
    parser.add_argument("--max-iterations", type=int, default=10)
    args = parser.parse_args()

    source = _read_table(args.input)
    converted = convert_customer_coordinates(
        source,
        customer_id_col=args.customer_id_col,
        gcj_lng_col=args.gcj_lng_col,
        gcj_lat_col=args.gcj_lat_col,
        tolerance=args.tolerance,
        max_iterations=args.max_iterations,
    )
    _write_table(converted, args.output)

    print(f"输入记录：{len(source):,}")
    print(converted["conversion_status"].value_counts(dropna=False).to_string())
    print(f"输出文件：{Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
