# -*- coding: utf-8 -*-
"""将卫星网点 Grid 匹配到同城市最近的现有专员格。

本模块完全独立，只依赖 NumPy 和 Pandas。两个输入数据集的坐标必须
使用同一坐标系；本项目默认两边都是 GCJ-02。

Jupyter 示例
------------
from nearest_specialist_grid import assign_nearest_specialist_grid

nearest_df = assign_nearest_specialist_grid(
    grid_df,
    专员格_df,
)

nearest_df.head()
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd


EARTH_RADIUS_KM = 6_371.0088


def _require_columns(
    df: pd.DataFrame,
    required: list[str],
    table_name: str,
) -> None:
    """检查必需字段，并提前拒绝重复字段名。"""
    duplicated = df.columns[df.columns.duplicated()].tolist()
    if duplicated:
        raise ValueError(
            f"{table_name}存在重复字段名：{duplicated}。"
            "请先处理重复字段名后再匹配。"
        )

    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"{table_name}缺少必需字段：{missing}")


def _normalize_text(values: pd.Series) -> pd.Series:
    """将业务键标准化为去除首尾空格的字符串。"""
    return values.astype("string").fillna("").str.strip()


def _validate_coordinates(
    df: pd.DataFrame,
    lng_col: str,
    lat_col: str,
    table_name: str,
) -> tuple[pd.Series, pd.Series]:
    """转换并校验经纬度，返回 float64 Series。"""
    lng = pd.to_numeric(df[lng_col], errors="coerce").astype(float)
    lat = pd.to_numeric(df[lat_col], errors="coerce").astype(float)

    valid = (
        np.isfinite(lng.to_numpy())
        & np.isfinite(lat.to_numpy())
        & lng.between(-180.0, 180.0).to_numpy()
        & lat.between(-90.0, 90.0).to_numpy()
    )
    if not valid.all():
        examples = df.loc[~valid, [lng_col, lat_col]].head(20)
        raise ValueError(
            f"{table_name}存在缺失或非法经纬度。\n示例：\n{examples}"
        )

    return lng, lat


def _deduplicate_or_raise(
    df: pd.DataFrame,
    normalized_id_col: str,
    consistency_cols: list[str],
    table_name: str,
) -> pd.DataFrame:
    """完全一致的重复 ID 保留一行；同 ID 属性冲突则报错。"""
    duplicated = df[normalized_id_col].duplicated(keep=False)
    if not duplicated.any():
        return df

    duplicate_rows = df.loc[
        duplicated,
        [normalized_id_col, *consistency_cols],
    ]
    conflicting_ids: list[str] = []
    for normalized_id, group in duplicate_rows.groupby(
        normalized_id_col,
        sort=False,
    ):
        if any(group[column].nunique(dropna=False) > 1 for column in consistency_cols):
            conflicting_ids.append(str(normalized_id))
            if len(conflicting_ids) >= 20:
                break

    if conflicting_ids:
        raise ValueError(
            f"{table_name}存在同一 ID 对应不同城市、坐标或属性的情况。"
            f"示例 ID：{conflicting_ids}"
        )

    return df.drop_duplicates(normalized_id_col, keep="first").copy()


def _nearest_candidate_indices(
    query_lng: np.ndarray,
    query_lat: np.ndarray,
    candidate_lng: np.ndarray,
    candidate_lat: np.ndarray,
    max_pairwise_elements: int,
) -> tuple[np.ndarray, np.ndarray]:
    """按块计算 Haversine 距离，返回最近候选下标及距离（公里）。"""
    candidate_count = len(candidate_lng)
    if candidate_count == 0:
        raise ValueError("候选点不能为空。")

    chunk_size = max(
        1,
        min(
            len(query_lng),
            max_pairwise_elements // candidate_count,
        ),
    )
    winners = np.empty(len(query_lng), dtype=np.int64)
    winner_distances_km = np.empty(len(query_lng), dtype=np.float64)

    candidate_lng_rad = np.radians(candidate_lng)[None, :]
    candidate_lat_rad = np.radians(candidate_lat)[None, :]

    for start in range(0, len(query_lng), chunk_size):
        end = min(start + chunk_size, len(query_lng))
        query_lng_rad = np.radians(query_lng[start:end])[:, None]
        query_lat_rad = np.radians(query_lat[start:end])[:, None]

        delta_lng = candidate_lng_rad - query_lng_rad
        delta_lat = candidate_lat_rad - query_lat_rad
        haversine_a = (
            np.sin(delta_lat / 2.0) ** 2
            + np.cos(query_lat_rad)
            * np.cos(candidate_lat_rad)
            * np.sin(delta_lng / 2.0) ** 2
        )
        clipped_a = np.clip(haversine_a, 0.0, 1.0)
        # 球面距离对 a 单调递增，因此直接用 a 选择最近候选；只对
        # 最终胜出的候选计算完整距离，避免无谓的矩阵运算。
        chunk_winners = np.argmin(clipped_a, axis=1)
        winners[start:end] = chunk_winners
        winner_a = clipped_a[
            np.arange(end - start),
            chunk_winners,
        ]
        winner_distances_km[start:end] = (
            2.0
            * EARTH_RADIUS_KM
            * np.arctan2(
                np.sqrt(winner_a),
                np.sqrt(1.0 - winner_a),
            )
        )

    return winners, winner_distances_km


def assign_nearest_specialist_grid(
    grid_df: pd.DataFrame,
    specialist_grid_df: pd.DataFrame,
    *,
    grid_id_col: str = "grid_id",
    grid_city_col: str = "city",
    grid_lng_col: str = "grid_centroid_gcj02_lng",
    grid_lat_col: str = "grid_centroid_gcj02_lat",
    specialist_id_col: str = "专员格id",
    specialist_city_col: str = "专员格归属城市",
    specialist_lng_col: str = "专员格质心经度",
    specialist_lat_col: str = "专员格质心纬度",
    missing_city: Literal["null", "raise"] = "null",
    max_pairwise_elements: int = 2_000_000,
) -> pd.DataFrame:
    """为每个卫星网点 Grid 匹配同城市最近的现有专员格。

    参数
    ----
    grid_df
        待匹配 Grid 表。默认字段为 ``grid_id``、``city``、
        ``grid_centroid_gcj02_lng`` 和 ``grid_centroid_gcj02_lat``。
        如果来自客户明细、同一 grid_id 出现多行，只要城市和质心一致，
        函数会自动去重为一行。
    specialist_grid_df
        现有专员格表。默认字段为 ``专员格id``、``专员格归属城市``、
        ``专员格质心经度``、``专员格质心纬度``。
    missing_city
        ``"null"``：该城市没有候选专员格时，归属字段留空；
        ``"raise"``：发现这种城市时立即报错。
    max_pairwise_elements
        单个向量化距离块最多包含的“查询点 × 候选点”数量，用于控制内存。

    返回
    ----
    DataFrame
        严格返回三列：``grid_id``、``归属专员格id``、
        ``distance_to_specialist_grid_km``。

    说明
    ----
    - 只会在相同城市内匹配；不会跨城市寻找候选。
    - 两边坐标必须使用同一坐标系，本项目默认均为 GCJ-02。
    - 距离相同时，按专员格 ID 升序、原始行序选择。
    """
    if missing_city not in {"null", "raise"}:
        raise ValueError("missing_city 只能是 'null' 或 'raise'。")
    if not isinstance(max_pairwise_elements, int) or max_pairwise_elements <= 0:
        raise ValueError("max_pairwise_elements 必须是正整数。")

    grid_required = [
        grid_id_col,
        grid_city_col,
        grid_lng_col,
        grid_lat_col,
    ]
    specialist_required = [
        specialist_id_col,
        specialist_city_col,
        specialist_lng_col,
        specialist_lat_col,
    ]
    _require_columns(grid_df, grid_required, "grid_df")
    _require_columns(
        specialist_grid_df,
        specialist_required,
        "specialist_grid_df",
    )

    if grid_df.empty:
        return pd.DataFrame(
            columns=[
                "grid_id",
                "归属专员格id",
                "distance_to_specialist_grid_km",
            ]
        )

    grid = grid_df[grid_required].copy()
    grid["_row_order"] = np.arange(len(grid), dtype=np.int64)
    grid["_grid_id_norm"] = _normalize_text(grid[grid_id_col])
    grid["_city_norm"] = _normalize_text(grid[grid_city_col])
    grid["_lng"], grid["_lat"] = _validate_coordinates(
        grid,
        grid_lng_col,
        grid_lat_col,
        "grid_df",
    )

    blank_grid_id = grid["_grid_id_norm"] == ""
    blank_grid_city = grid["_city_norm"] == ""
    if blank_grid_id.any() or blank_grid_city.any():
        examples = grid.loc[
            blank_grid_id | blank_grid_city,
            [grid_id_col, grid_city_col],
        ].head(20)
        raise ValueError(
            "grid_df存在空 grid_id 或空城市。\n"
            f"示例：\n{examples}"
        )

    grid = _deduplicate_or_raise(
        grid,
        "_grid_id_norm",
        ["_city_norm", "_lng", "_lat"],
        "grid_df",
    )
    grid = grid.sort_values("_row_order", kind="mergesort").reset_index(
        drop=True
    )

    specialist = specialist_grid_df[specialist_required].copy()
    specialist["_row_order"] = np.arange(
        len(specialist),
        dtype=np.int64,
    )
    specialist["_specialist_id_norm"] = _normalize_text(
        specialist[specialist_id_col]
    )
    specialist["_city_norm"] = _normalize_text(
        specialist[specialist_city_col]
    )
    specialist["_lng"], specialist["_lat"] = _validate_coordinates(
        specialist,
        specialist_lng_col,
        specialist_lat_col,
        "specialist_grid_df",
    )

    blank_specialist_id = specialist["_specialist_id_norm"] == ""
    blank_specialist_city = specialist["_city_norm"] == ""
    if blank_specialist_id.any() or blank_specialist_city.any():
        examples = specialist.loc[
            blank_specialist_id | blank_specialist_city,
            [specialist_id_col, specialist_city_col],
        ].head(20)
        raise ValueError(
            "specialist_grid_df存在空专员格 ID 或空城市。\n"
            f"示例：\n{examples}"
        )

    specialist = _deduplicate_or_raise(
        specialist,
        "_specialist_id_norm",
        [
            "_city_norm",
            "_lng",
            "_lat",
        ],
        "specialist_grid_df",
    )
    specialist = specialist.sort_values(
        [
            "_city_norm",
            "_specialist_id_norm",
            "_row_order",
        ],
        kind="mergesort",
    ).reset_index(drop=True)

    assigned_ids = np.empty(len(grid), dtype=object)
    assigned_ids[:] = pd.NA
    assigned_distances_km = np.full(len(grid), np.nan, dtype=np.float64)

    specialists_by_city = {
        str(city): group.reset_index(drop=True)
        for city, group in specialist.groupby(
            "_city_norm",
            sort=False,
        )
    }
    missing_cities: list[str] = []

    for city, city_grid in grid.groupby("_city_norm", sort=False):
        city_key = str(city)
        candidates = specialists_by_city.get(city_key)
        positions = city_grid.index.to_numpy(dtype=np.int64)

        if candidates is None or candidates.empty:
            missing_cities.append(city_key)
            continue

        winners, winner_distances_km = _nearest_candidate_indices(
            city_grid["_lng"].to_numpy(dtype=np.float64),
            city_grid["_lat"].to_numpy(dtype=np.float64),
            candidates["_lng"].to_numpy(dtype=np.float64),
            candidates["_lat"].to_numpy(dtype=np.float64),
            max_pairwise_elements,
        )
        assigned_ids[positions] = candidates.iloc[winners][
            specialist_id_col
        ].to_numpy(dtype=object)
        assigned_distances_km[positions] = winner_distances_km

    if missing_cities and missing_city == "raise":
        raise ValueError(
            "以下城市在专员格表中没有候选专员格："
            + ", ".join(sorted(set(missing_cities)))
        )

    return pd.DataFrame(
        {
            "grid_id": grid[grid_id_col].to_numpy(copy=False),
            "归属专员格id": assigned_ids,
            "distance_to_specialist_grid_km": assigned_distances_km,
        }
    )


__all__ = ["assign_nearest_specialist_grid"]
