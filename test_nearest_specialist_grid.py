"""nearest_specialist_grid.py 的轻量回归测试。"""

import pandas as pd

from nearest_specialist_grid import assign_nearest_specialist_grid


def main() -> None:
    grid_df = pd.DataFrame(
        {
            # G1 重复模拟从客户明细中直接提取的情况。
            "grid_id": ["G1", "G1", "G2", "G3"],
            "city": ["上海", "上海", "上海", "无候选城市"],
            "grid_centroid_gcj02_lng": [
                121.4700,
                121.4700,
                121.5200,
                110.0000,
            ],
            "grid_centroid_gcj02_lat": [
                31.2300,
                31.2300,
                31.2300,
                20.0000,
            ],
        }
    )
    specialist_grid_df = pd.DataFrame(
        {
            "专员格id": ["S002", "S001", "S003"],
            "专员格归属网点": ["西网点", "东网点", "北京网点"],
            "专员格归属城市": ["上海", "上海", "北京"],
            "专员格质心经度": [121.4600, 121.5300, 116.4000],
            "专员格质心纬度": [31.2300, 31.2300, 39.9000],
        }
    )

    result = assign_nearest_specialist_grid(
        grid_df,
        specialist_grid_df,
    )

    assert result.columns.tolist() == [
        "grid_id",
        "归属专员格id",
        "归属专员格网点",
    ]
    assert result["grid_id"].tolist() == ["G1", "G2", "G3"]
    assert result.loc[0, "归属专员格id"] == "S002"
    assert result.loc[0, "归属专员格网点"] == "西网点"
    assert result.loc[1, "归属专员格id"] == "S001"
    assert result.loc[1, "归属专员格网点"] == "东网点"
    assert pd.isna(result.loc[2, "归属专员格id"])
    assert pd.isna(result.loc[2, "归属专员格网点"])

    tie_grid = pd.DataFrame(
        {
            "grid_id": ["TIE"],
            "city": ["上海"],
            "grid_centroid_gcj02_lng": [121.4950],
            "grid_centroid_gcj02_lat": [31.2300],
        }
    )
    tie_result = assign_nearest_specialist_grid(
        tie_grid,
        specialist_grid_df,
    )
    # 两个上海候选点关于查询点对称；按专员格 ID 升序选 S001。
    assert tie_result.loc[0, "归属专员格id"] == "S001"

    try:
        assign_nearest_specialist_grid(
            grid_df,
            specialist_grid_df,
            missing_city="raise",
        )
    except ValueError as exc:
        assert "无候选城市" in str(exc)
    else:
        raise AssertionError("missing_city='raise' 应对缺少候选城市报错。")

    print("nearest specialist grid tests passed")


if __name__ == "__main__":
    main()
