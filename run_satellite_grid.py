"""卫星网点专员格算法的命令行入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from satellite_grid_partition_v1 import (
    AlgorithmConfig,
    ColumnConfig,
    load_table,
    run_satellite_grid_algorithm,
    save_result_csv,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="运行卫星网点专员格划分及网点归属算法。",
    )
    parser.add_argument("--customer", required=True, help="客户表路径")
    parser.add_argument(
        "--outlet",
        help="可选网点经纬度表路径；用于 Grid 和客户归属网点",
    )
    parser.add_argument("--admin", required=True, help="行政街道表路径")
    parser.add_argument(
        "--existing-grid",
        required=True,
        help="已有基础网格表路径",
    )
    parser.add_argument(
        "--fyp-threshold",
        required=True,
        help="城市 FYP 门槛表路径",
    )
    parser.add_argument(
        "--distance",
        required=True,
        help="城市距离表路径",
    )
    parser.add_argument(
        "--output-dir",
        default="satellite_grid_output",
        help="输出目录，默认 satellite_grid_output",
    )
    parser.add_argument(
        "--column-config",
        help="ColumnConfig JSON 路径；列名与默认值不同时使用",
    )
    parser.add_argument(
        "--min-customer-count",
        type=int,
        default=50,
        help="每个成功专员格的最低去重客户数，默认 50",
    )
    parser.add_argument(
        "--h3-resolution",
        type=int,
        default=9,
        help="H3 分辨率，默认 9",
    )
    parser.add_argument(
        "--input-coordinate-system",
        choices=["GCJ02", "WGS84"],
        default="GCJ02",
        help="全部空间输入的坐标系，当前业务默认 GCJ02",
    )
    parser.add_argument(
        "--no-grid-geometry",
        action="store_true",
        help="不生成最终网格 GeoJSON，可用于调试提速",
    )
    parser.add_argument(
        "--skip-customer-admin-match",
        action="store_true",
        help="不要求客户街道名称与 Geometry 判定街道一致",
    )
    parser.add_argument(
        "--allow-cross-admin-street",
        action="store_true",
        help=(
            "关闭行政街道硬边界：专员格可在同一城市内跨街道，"
            "已有基础网格仍然排除"
        ),
    )
    parser.add_argument(
        "--allow-negative-fyp",
        action="store_true",
        help="允许负 expected_fyp；正常业务不建议开启",
    )
    return parser.parse_args()


def load_column_config(path: str | None) -> ColumnConfig:
    if not path:
        return ColumnConfig()

    config_path = Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(
            f"无法读取 ColumnConfig JSON：{config_path}：{exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError("ColumnConfig JSON 顶层必须是对象。")

    try:
        return ColumnConfig(**payload)
    except TypeError as exc:
        valid_fields = list(ColumnConfig.__dataclass_fields__)
        raise ValueError(
            "ColumnConfig JSON 包含未知字段或缺少正确格式。"
            f"允许字段：{valid_fields}"
        ) from exc


def main() -> None:
    args = parse_args()
    cols = load_column_config(args.column_config)
    config = AlgorithmConfig(
        h3_resolution=args.h3_resolution,
        min_customer_count=args.min_customer_count,
        input_coordinate_system=args.input_coordinate_system,
        restrict_to_admin_street=(
            not args.allow_cross_admin_street
        ),
        require_customer_admin_match=(
            not args.skip_customer_admin_match
        ),
        allow_negative_fyp=args.allow_negative_fyp,
        build_grid_geometry=not args.no_grid_geometry,
    )

    print("1/4 读取输入表...")
    result = run_satellite_grid_algorithm(
        customer_df=load_table(args.customer),
        admin_df=load_table(args.admin),
        existing_grid_df=load_table(args.existing_grid),
        fyp_threshold_df=load_table(args.fyp_threshold),
        distance_df=load_table(args.distance),
        cols=cols,
        config=config,
        outlet_df=(
            load_table(args.outlet)
            if args.outlet
            else None
        ),
    )

    print("2/4 算法与一致性校验完成。")
    print(f"成功专员格：{len(result.grids):,}")
    print(f"成功分配 H3：{len(result.h3_detail):,}")
    print(f"失败 Seed：{len(result.failed_seeds):,}")
    print(f"废弃 H3：{len(result.abandoned_h3):,}")

    print("3/4 保存 CSV...")
    save_result_csv(result, args.output_dir)
    print(result.coverage_metrics.to_string(index=False))

    print(f"4/4 完成：{Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
