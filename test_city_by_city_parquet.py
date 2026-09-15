# -*- coding: utf-8 -*-
"""按城市运行、独立落盘和失败继续的回归测试。"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

import satellite_grid_partition_v1 as sg
from test_admin_street_switch import _make_inputs


def _copy_city_inputs(
    city: str,
    customer_prefix: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    customer, admin, existing, _threshold, _distance = _make_inputs()
    customer = customer.copy()
    admin = admin.copy()
    existing = existing.copy()
    customer["city"] = city
    customer["customer_id"] = (
        customer_prefix + "_" + customer["customer_id"].astype(str)
    )
    admin["city"] = city
    existing["city"] = city
    return customer, admin, existing


def test_city_failure_is_skipped_and_reported() -> None:
    failed_city = "A_失败城市"
    first_success_city = "B_成功城市"
    no_admin_city = "C_无行政边界"
    second_success_city = "D_成功城市"

    failed_customer, failed_admin, _failed_existing = _copy_city_inputs(
        failed_city,
        "FAIL",
    )
    first_customer, first_admin, first_existing = _copy_city_inputs(
        first_success_city,
        "FIRST",
    )
    second_customer, second_admin, second_existing = _copy_city_inputs(
        second_success_city,
        "SECOND",
    )
    no_admin_customer, *_unused = _copy_city_inputs(
        no_admin_city,
        "NOADMIN",
    )
    no_admin_customer = no_admin_customer.head(3).copy()

    # 同一原始 object 字段在不同城市分别只有数字、
    # 空值和文本，用于验证多文件 Parquet Schema 仍一致。
    failed_customer["mixed_source_field"] = "FAILED"
    first_customer["mixed_source_field"] = 1
    no_admin_customer["mixed_source_field"] = None
    second_customer["mixed_source_field"] = "TEXT"

    customer_df = pd.concat(
        [
            failed_customer,
            first_customer,
            no_admin_customer,
            second_customer,
        ],
        ignore_index=True,
    )
    admin_df = pd.concat(
        [failed_admin, first_admin, second_admin],
        ignore_index=True,
    )
    existing_grid_df = pd.concat(
        [first_existing, second_existing],
        ignore_index=True,
    )
    fyp_threshold_df = pd.DataFrame(
        [
            {
                "city": first_success_city,
                "target_expected_fyp": 50.0,
            },
            {
                "city": second_success_city,
                "target_expected_fyp": 50.0,
            },
        ]
    )
    distance_df = pd.DataFrame(
        [
            {"city": first_success_city, "distance_km": 10.0},
            {"city": second_success_city, "distance_km": 10.0},
        ]
    )
    config = sg.AlgorithmConfig(
        h3_resolution=9,
        input_coordinate_system="GCJ02",
        min_customer_count=50,
        restrict_to_admin_street=False,
        build_grid_geometry=False,
        enable_timing=False,
    )

    with TemporaryDirectory() as temp_dir:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            summary = sg.run_satellite_grid_by_city_to_parquet(
                customer_df=customer_df,
                admin_df=admin_df,
                existing_grid_df=existing_grid_df,
                fyp_threshold_df=fyp_threshold_df,
                distance_df=distance_df,
                output_dir=temp_dir,
                config=config,
            )

        status_by_city = summary.set_index("city")["status"].to_dict()
        assert status_by_city == {
            failed_city: "FAILED",
            first_success_city: "SUCCESS",
            no_admin_city: "SAVED_UNASSIGNED_NO_ADMIN_BOUNDARY",
            second_success_city: "SUCCESS",
        }

        failed_row = summary.loc[
            summary["city"] == failed_city
        ].iloc[0]
        assert not bool(failed_row["file_saved"])
        assert "缺少城市距离/FYP参数" in failed_row["error_message"]
        assert failed_city in stdout.getvalue()
        assert "运行失败城市及原因" in stdout.getvalue()

        output_files = sorted(Path(temp_dir).glob("*.parquet"))
        assert len(output_files) == 3
        assert not list(Path(temp_dir).glob("*.tmp"))

        combined_dataset = pd.read_parquet(temp_dir)
        assert len(combined_dataset) == (
            len(first_customer)
            + len(no_admin_customer)
            + len(second_customer)
        )
        assert set(combined_dataset["city"].dropna()) == {
            first_success_city,
            no_admin_city,
            second_success_city,
        }
        assert set(
            combined_dataset.loc[
                combined_dataset["city"] == first_success_city,
                "mixed_source_field",
            ].dropna()
        ) == {"1"}

        # 与一次性全量运行对比：成功城市和无边界城市
        # 的客户明细值保持一致，Grid ID 也按全局顺序续编。
        comparable_customers = customer_df.loc[
            customer_df["city"] != failed_city
        ].copy()
        comparable_admin = admin_df.loc[
            admin_df["city"] != failed_city
        ].copy()
        full_result = sg.run_satellite_grid_algorithm(
            customer_df=comparable_customers,
            admin_df=comparable_admin,
            existing_grid_df=existing_grid_df,
            fyp_threshold_df=fyp_threshold_df,
            distance_df=distance_df,
            config=config,
        )

        for city in [
            first_success_city,
            no_admin_city,
            second_success_city,
        ]:
            output_file = summary.loc[
                summary["city"] == city,
                "output_file",
            ].iloc[0]
            saved = pd.read_parquet(output_file).reset_index(drop=True)
            expected = full_result.grid_customer_detail.loc[
                full_result.grid_customer_detail["city"] == city
            ].reset_index(drop=True)
            expected = sg._normalize_city_detail_for_parquet(expected)
            pd.testing.assert_frame_equal(
                saved,
                expected,
                check_dtype=False,
                check_exact=True,
            )

        first_ids = pd.read_parquet(
            summary.loc[
                summary["city"] == first_success_city,
                "output_file",
            ].iloc[0]
        )["grid_id"].dropna()
        second_ids = pd.read_parquet(
            summary.loc[
                summary["city"] == second_success_city,
                "output_file",
            ].iloc[0]
        )["grid_id"].dropna()
        assert first_ids.str.endswith("G000001").all()
        assert second_ids.str.endswith("G000002").all()


if __name__ == "__main__":
    test_city_failure_is_skipped_and_reported()
    print("按城市 Parquet 运行测试通过。")
