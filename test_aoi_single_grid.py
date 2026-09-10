# -*- coding: utf-8 -*-
"""AOI 质心统一投射与唯一专员格约束的端到端测试。"""

from __future__ import annotations

import json

import pandas as pd
from shapely.geometry import mapping
from shapely.ops import unary_union

import satellite_grid_partition_v1 as sg


def _to_gcj_point(cell: str) -> tuple[float, float]:
    lat, lng = sg.h3_cell_to_latlng(cell)
    return sg.wgs84_to_gcj02(lng, lat)


def _build_inputs():
    resolution = 9
    cell_a = sg.h3_latlng_to_cell(31.2304, 121.4737, resolution)
    candidates = set(sg.h3.grid_disk(cell_a, 4)) - {cell_a}
    cell_b = max(
        candidates,
        key=lambda cell: sg.haversine_distance_m(
            *sg.h3_cell_to_latlng(cell_a),
            *sg.h3_cell_to_latlng(cell),
        ),
    )
    assert cell_b not in sg.h3_neighbors(cell_a)

    geom_a_gcj = sg.geometry_to_gcj02(
        sg.h3_cell_boundary_polygon(cell_a)
    )
    geom_b_gcj = sg.geometry_to_gcj02(
        sg.h3_cell_boundary_polygon(cell_b)
    )
    city_geometry_gcj = unary_union([geom_a_gcj, geom_b_gcj])

    admin_df = pd.DataFrame(
        [
            {
                "city": "上海",
                "area_code": "TEST",
                "area_name": "测试街道",
                "area_geometry": json.dumps(mapping(city_geometry_gcj)),
            }
        ]
    )

    a_lng, a_lat = _to_gcj_point(cell_a)
    b_lng, b_lat = _to_gcj_point(cell_b)
    customers = []

    # 同一个 AOI 的原始客户点故意分布在两个远离且不相邻的 H3，
    # 但统一使用 cell_a 的 AOI 质心，因此正式分配只能进入 cell_a。
    for index in range(20):
        customer_lng, customer_lat = (
            (a_lng, a_lat) if index < 10 else (b_lng, b_lat)
        )
        customers.append(
            {
                "customer_id": f"AOI{index:03d}",
                "city": "上海",
                "lng": customer_lng,
                "lat": customer_lat,
                "area_admin_code": "测试街道",
                "expected_fyp": 1.0,
                "aoi_id": "AOI_SHARED",
                "aoi_lng": a_lng,
                "aoi_lat": a_lat,
            }
        )

    # 30 个无 AOI 客户与 AOI 的 20 人共同令 cell_a 达到 50 人。
    for index in range(30):
        customers.append(
            {
                "customer_id": f"A{index:03d}",
                "city": "上海",
                "lng": a_lng,
                "lat": a_lat,
                "area_admin_code": "测试街道",
                "expected_fyp": 1.0,
                "aoi_id": None,
                "aoi_lng": None,
                "aoi_lat": None,
            }
        )

    # cell_b 的 50 个无 AOI 客户独立形成第二个专员格。
    for index in range(50):
        customers.append(
            {
                "customer_id": f"B{index:03d}",
                "city": "上海",
                "lng": b_lng,
                "lat": b_lat,
                "area_admin_code": "测试街道",
                "expected_fyp": 1.0,
                "aoi_id": None,
                "aoi_lng": None,
                "aoi_lat": None,
            }
        )

    return (
        pd.DataFrame(customers),
        admin_df,
        pd.DataFrame(columns=["city", "basic_net_geom"]),
        pd.DataFrame(
            [{"city": "上海", "target_expected_fyp": 50.0}]
        ),
        pd.DataFrame([{"city": "上海", "distance_km": 10.0}]),
    )


def _run(customer_df: pd.DataFrame):
    _, admin_df, existing_grid_df, fyp_threshold_df, distance_df = (
        _build_inputs()
    )
    return sg.run_satellite_grid_algorithm(
        customer_df=customer_df,
        admin_df=admin_df,
        existing_grid_df=existing_grid_df,
        fyp_threshold_df=fyp_threshold_df,
        distance_df=distance_df,
        config=sg.AlgorithmConfig(
            h3_resolution=9,
            input_coordinate_system="GCJ02",
            min_customer_count=50,
            restrict_to_admin_street=True,
            build_grid_geometry=False,
        ),
    )


def test_aoi_single_grid() -> None:
    customer_df, *_ = _build_inputs()
    result = _run(customer_df)

    assert len(result.grids) == 2
    assert sorted(result.grids["grid_customer_count"].tolist()) == [50, 50]
    assert sorted(result.grids["grid_fyp"].tolist()) == [50.0, 50.0]

    aoi_rows = result.grid_customer_detail.query(
        "aoi_id == 'AOI_SHARED'"
    )
    assert len(aoi_rows) == 20
    assert aoi_rows["h3_id"].nunique() == 1
    assert aoi_rows["grid_id"].nunique() == 1
    assert aoi_rows["has_successful_grid"].all()
    assert set(aoi_rows["allocation_coordinate_source"]) == {
        "AOI_CENTROID"
    }
    assert set(aoi_rows["aoi_customer_count"].astype(int)) == {20}
    assert set(aoi_rows["aoi_expected_fyp"].astype(float)) == {20.0}

    no_aoi_rows = result.grid_customer_detail.query("has_aoi == False")
    assert len(no_aoi_rows) == 80
    assert no_aoi_rows["aoi_id"].isna().all()
    assert set(no_aoi_rows["allocation_coordinate_source"]) == {
        "CUSTOMER_POINT"
    }


def test_inconsistent_aoi_coordinate_rejected() -> None:
    customer_df, *_ = _build_inputs()
    aoi_indexes = customer_df.index[
        customer_df["aoi_id"] == "AOI_SHARED"
    ]
    customer_df.loc[aoi_indexes[0], "aoi_lng"] += 0.001
    try:
        _run(customer_df)
    except ValueError as exc:
        assert "aoi_lng/aoi_lat 必须一致" in str(exc)
    else:
        raise AssertionError("AOI 质心不一致时应当报错。")


if __name__ == "__main__":
    test_aoi_single_grid()
    test_inconsistent_aoi_coordinate_rejected()
    print("AOI 唯一专员格测试通过。")
