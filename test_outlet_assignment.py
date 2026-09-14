# -*- coding: utf-8 -*-
"""城市内网点归属与客户继承规则的精简回归测试。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

import satellite_grid_partition_v1 as sg
from amap_grid_viewer import load_grid_table
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


def test_same_outlet_can_have_multiple_workplaces() -> None:
    # 同名网点允许有多个职场坐标，并选择离 Grid 质心最近的一行。
    outlets = pd.DataFrame(
        [
            {
                "二级机构": "上海",
                "城市": "上海",
                "网点名称-正式": "多职场网点",
                "经度": 120.00,
                "纬度": 30.00,
            },
            {
                "二级机构": "上海",
                "城市": "上海",
                "网点名称-正式": "多职场网点",
                "经度": 121.47,
                "纬度": 31.23,
            },
        ]
    )
    result = _run(outlets)
    grid = result.grids.iloc[0]
    assert grid["assigned_outlet_name"] == "多职场网点"
    assert float(grid["assigned_outlet_lng"]) == 121.47
    assert float(grid["assigned_outlet_lat"]) == 31.23
    assert grid["outlet_assignment_method"] == (
        "NEAREST_TO_GRID_CENTROID"
    )

    # 距离与名称都相同，使用输入表原始行顺序作为最终稳定规则。
    exact_tie = pd.DataFrame(
        [
            {
                "二级机构": "原始第一行",
                "城市": "上海",
                "网点名称-正式": "同名网点",
                "经度": 121.47,
                "纬度": 31.23,
            },
            {
                "二级机构": "原始第二行",
                "城市": "上海",
                "网点名称-正式": "同名网点",
                "经度": 121.47,
                "纬度": 31.23,
            },
        ]
    )
    tied_result = _run(exact_tie)
    assert tied_result.grids.iloc[0]["assigned_secondary_org"] == (
        "原始第一行"
    )


def test_parquet_is_default_ready_and_csv_is_retained() -> None:
    outlets = pd.DataFrame(
        [
            {
                "二级机构": "上海",
                "城市": "上海",
                "网点名称-正式": "测试网点",
                "经度": 121.47,
                "纬度": 31.23,
            }
        ]
    )
    result = _run(outlets)

    with TemporaryDirectory() as temp_dir:
        parquet_dir = Path(temp_dir) / "parquet"
        csv_dir = Path(temp_dir) / "csv"
        sg.save_result_parquet(result, parquet_dir)
        sg.save_result_csv(result, csv_dir)

        parquet_files = sorted(parquet_dir.glob("*.parquet"))
        csv_files = sorted(csv_dir.glob("*.csv"))
        assert len(parquet_files) == 8
        assert len(csv_files) == 8

        customer_detail = pd.read_parquet(
            parquet_dir / "08_grid_customer_detail.parquet"
        )
        assert len(customer_detail) == len(result.grid_customer_detail)
        grid_table = load_grid_table(
            str(parquet_dir / "01_grid_level.parquet")
        )
        assert len(grid_table) == len(result.grids)


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
    test_same_outlet_can_have_multiple_workplaces()
    test_parquet_is_default_ready_and_csv_is_retained()
    test_invalid_outlet_coordinate_rejected()
    print("网点归属测试通过。")
