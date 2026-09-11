# -*- coding: utf-8 -*-
"""城市内网点归属与客户继承规则的精简回归测试。"""

from __future__ import annotations

import pandas as pd

import satellite_grid_partition_v1 as sg
from test_admin_street_switch import _make_inputs


def _run(outlet_df: pd.DataFrame):
    customer, admin, existing, threshold, distance = _make_inputs()
    return sg.run_satellite_grid_algorithm(
        customer_df=customer,
        admin_df=admin,
        existing_grid_df=existing,
        fyp_threshold_df=threshold,
        distance_df=distance,
        outlet_df=outlet_df,
        config=sg.AlgorithmConfig(
            h3_resolution=9,
            input_coordinate_system="GCJ02",
            min_customer_count=50,
            restrict_to_admin_street=False,
            build_grid_geometry=True,
            enable_timing=False,
        ),
    )


def test_outlet_assignment_and_customer_inheritance() -> None:
    # 两个同城市网点使用相同坐标，到 Grid 质心的距离
    # 完全相同；应按网点名称升序选择“网点A”。
    outlets = pd.DataFrame(
        [
            {
                "二级机构": "上海",
                "城市": "上海",
                "网点名称-正式": "网点B",
                "经度": 121.47,
                "纬度": 31.23,
            },
            {
                "二级机构": "上海",
                "城市": "上海",
                "网点名称-正式": "网点A",
                "经度": 121.47,
                "纬度": 31.23,
            },
        ]
    )
    result = _run(outlets)

    assert len(result.grids) == 1
    grid = result.grids.iloc[0]
    assert grid["assigned_secondary_org"] == "上海"
    assert grid["assigned_outlet_name"] == "网点A"
    assert grid["outlet_assignment_method"] == (
        "NEAREST_TO_GRID_CENTROID"
    )
    assert float(grid["distance_to_assigned_outlet_km"]) >= 0

    assigned = result.grid_customer_detail.query(
        "has_successful_grid == True"
    )
    assert len(assigned) == 60
    assert set(assigned["assigned_outlet_name"]) == {"网点A"}
    assert set(assigned["outlet_assignment_method"]) == {
        "NEAREST_TO_GRID_CENTROID"
    }

    no_grid = result.grid_customer_detail.query(
        "has_successful_grid == False"
    )
    assert len(no_grid) == 1
    assert no_grid["assigned_outlet_name"].isna().all()
    assert set(no_grid["outlet_assignment_method"]) == {
        "NO_SUCCESSFUL_GRID"
    }

    for table_name in [
        "h3_detail",
        "failed_seeds",
        "abandoned_h3",
        "coverage_metrics",
        "h3_pool",
        "customer_diagnostic",
    ]:
        table = getattr(result, table_name)
        assert not (set(sg.OUTLET_ASSIGNMENT_COLUMNS) & set(table.columns))


def test_only_outlet_and_missing_city() -> None:
    only_outlet = pd.DataFrame(
        [
            {
                "二级机构": "上海",
                "城市": "上海",
                "网点名称-正式": "唯一网点",
                "经度": 121.47,
                "纬度": 31.23,
            }
        ]
    )
    result = _run(only_outlet)
    assert set(result.grids["assigned_outlet_name"]) == {"唯一网点"}
    assert set(result.grids["outlet_assignment_method"]) == {
        "ONLY_OUTLET_IN_CITY"
    }

    other_city_outlet = only_outlet.copy()
    other_city_outlet["城市"] = "北京"
    result = _run(other_city_outlet)
    assert result.grids["assigned_outlet_name"].isna().all()
    assert set(result.grids["outlet_assignment_method"]) == {
        "NO_OUTLET_IN_CITY"
    }


def test_invalid_outlet_coordinate_rejected() -> None:
    invalid_outlet = pd.DataFrame(
        [
            {
                "二级机构": "上海",
                "城市": "上海",
                "网点名称-正式": "坐标错误网点",
                "经度": None,
                "纬度": 31.23,
            }
        ]
    )
    try:
        _run(invalid_outlet)
    except ValueError as exc:
        assert "非法的 GCJ-02 经纬度" in str(exc)
    else:
        raise AssertionError("网点坐标非法时应当报错。")


if __name__ == "__main__":
    test_outlet_assignment_and_customer_inheritance()
    test_only_outlet_and_missing_city()
    test_invalid_outlet_coordinate_rejected()
    print("网点归属测试通过。")
