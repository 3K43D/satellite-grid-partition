# -*- coding: utf-8 -*-
"""行政街道限制开关的端到端回归测试。"""

from __future__ import annotations

import json

import pandas as pd
from shapely.geometry import Point, mapping
from shapely.ops import unary_union

import satellite_grid_partition_v1 as sg


def _make_inputs():
    resolution = 9
    cell_a = sg.h3_latlng_to_cell(31.2304, 121.4737, resolution)
    cell_b = sorted(sg.h3_neighbors(cell_a))[0]
    cell_occupied = sorted(sg.h3_neighbors(cell_b) - {cell_a})[0]

    geom_a_wgs84 = sg.h3_cell_boundary_polygon(cell_a)
    geom_b_wgs84 = unary_union(
        [
            sg.h3_cell_boundary_polygon(cell_b),
            sg.h3_cell_boundary_polygon(cell_occupied),
        ]
    )
    geom_a = sg.geometry_to_gcj02(geom_a_wgs84)
    geom_b = sg.geometry_to_gcj02(geom_b_wgs84)

    admin_df = pd.DataFrame(
        [
            {
                "city": "上海",
                "area_code": "A",
                "area_name": "街道A",
                "area_geometry": json.dumps(mapping(geom_a)),
            },
            {
                "city": "上海",
                "area_code": "B",
                "area_name": "街道B",
                "area_geometry": json.dumps(mapping(geom_b)),
            },
        ]
    )

    customers = []
    for street, cell, prefix in [
        ("街道A", cell_a, "A"),
        ("街道B", cell_b, "B"),
    ]:
        lat, lng = sg.h3_cell_to_latlng(cell)
        gcj_lng, gcj_lat = sg.wgs84_to_gcj02(lng, lat)
        for index in range(30):
            customers.append(
                {
                    "customer_id": f"{prefix}{index:03d}",
                    "city": "上海",
                    "lng": gcj_lng,
                    "lat": gcj_lat,
                    "area_admin_code": street,
                    "expected_fyp": 1.0,
                }
            )

    occupied_lat, occupied_lng = sg.h3_cell_to_latlng(cell_occupied)
    occupied_gcj_lng, occupied_gcj_lat = sg.wgs84_to_gcj02(
        occupied_lng,
        occupied_lat,
    )
    customers.append(
        {
            "customer_id": "OCCUPIED",
            "city": "上海",
            "lng": occupied_gcj_lng,
            "lat": occupied_gcj_lat,
            "area_admin_code": "街道B",
            "expected_fyp": 1000.0,
        }
    )

    customer_df = pd.DataFrame(customers)
    existing_grid_df = pd.DataFrame(
        [
            {
                "city": "上海",
                "basic_net_geom": json.dumps(
                    mapping(
                        sg.geometry_to_gcj02(
                            sg.h3_cell_boundary_polygon(cell_occupied)
                        )
                    )
                ),
            }
        ]
    )
    fyp_threshold_df = pd.DataFrame(
        [{"city": "上海", "target_expected_fyp": 50.0}]
    )
    distance_df = pd.DataFrame(
        [{"city": "上海", "distance_km": 10.0}]
    )
    return (
        customer_df,
        admin_df,
        existing_grid_df,
        fyp_threshold_df,
        distance_df,
    )


def _run(
    restrict_to_admin_street: bool,
    build_grid_geometry: bool = True,
):
    (
        customer_df,
        admin_df,
        existing_grid_df,
        fyp_threshold_df,
        distance_df,
    ) = _make_inputs()

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
            restrict_to_admin_street=restrict_to_admin_street,
            require_customer_admin_match=True,
            build_grid_geometry=build_grid_geometry,
        ),
    )


def test_admin_street_switch() -> None:
    restricted = _run(True)
    assert restricted.grids.empty

    city_wide = _run(False)
    assert len(city_wide.grids) == 1
    grid = city_wide.grids.iloc[0]
    assert grid["admin_code"] == "CITY_WIDE"
    assert grid["admin_name"] == "城市内跨行政街道"
    assert not bool(grid["admin_restriction_enabled"])
    assert int(grid["grid_customer_count"]) == 60
    assert float(grid["grid_fyp"]) == 60.0

    grid_wgs84 = sg.parse_geometry(grid["grid_geometry_geojson_wgs84"])
    grid_gcj02 = sg.parse_geometry(grid["grid_geometry_geojson_gcj02"])
    assert grid_wgs84.covers(
        Point(
            grid["grid_label_point_wgs84_lng"],
            grid["grid_label_point_wgs84_lat"],
        )
    )
    assert grid_gcj02.covers(
        Point(
            grid["grid_label_point_gcj02_lng"],
            grid["grid_label_point_gcj02_lat"],
        )
    )
    assert pd.notna(grid["grid_centroid_wgs84_lng"])
    assert pd.notna(grid["grid_centroid_gcj02_lng"])

    assigned = city_wide.grid_customer_detail.query(
        "has_successful_grid == True"
    )
    assert assigned["customer_id"].nunique() == 60
    assert set(assigned["customer_admin_name"]) == {"街道A", "街道B"}
    assert not assigned["admin_restriction_enabled"].any()
    assert assigned["grid_centroid_gcj02_lng"].notna().all()

    occupied = city_wide.grid_customer_detail.loc[
        city_wide.grid_customer_detail["customer_id"] == "OCCUPIED"
    ].iloc[0]
    assert bool(occupied["_excluded_by_existing_grid"])
    assert not bool(occupied["has_successful_grid"])

    without_geometry = _run(False, build_grid_geometry=False)
    no_geometry_grid = without_geometry.grids.iloc[0]
    assert pd.isna(no_geometry_grid["grid_geometry_geojson_wgs84"])
    assert pd.isna(no_geometry_grid["grid_centroid_wgs84_lng"])
    assert pd.isna(no_geometry_grid["grid_label_point_gcj02_lng"])


if __name__ == "__main__":
    test_admin_street_switch()
    print("行政街道限制开关测试通过。")
