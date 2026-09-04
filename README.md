# 卫星网点专员格自动划分

基于行政街道边界、既有基础网格、客户位置、城市 FYP 门槛和距离门槛，使用 H3 与事务式 BFS 自动生成卫星网点专员格。

当前版本：**V1.1**。

## 核心规则

- H3 分辨率默认为 9。
- H3 行政归属和既有网格占用均按 **H3 中心点落入 Polygon** 判断。
- 每个“城市 × 行政街道”独立划分，不跨行政街道生长。
- 城市距离参数按专员格最大跨度（直径）解释，算法半径为 `distance_km / 2`。
- 种子按“自身 FYP + 未分配一阶邻居 FYP”选择。
- BFS 同层依次按 FYP 降序、距种子距离升序、H3 ID 升序吸收。
- 专员格只有在 FYP 和去重客户数两个门槛同时达标时才提交。
- 最低客户数默认为 50，可通过 `AlgorithmConfig(min_customer_count=...)` 调整。
- FYP 已达标但人数不足时继续扩张；半径或拓扑耗尽仍不达标则回滚。
- 客户数按非空 `customer_id` 去重。`expected_fyp=0` 或为空的合法客户仍计人数；空值不贡献 FYP。

## 仓库结构

```text
.
├── satellite_grid_partition_v1.py   # 主算法与文件运行入口
├── requirements.txt                 # Python 依赖
├── README.md                        # 项目概览与快速开始
└── docs/
    ├── operation-guide.md           # 完整操作、校验与排错指引
    └── output-data-dictionary.md    # 8 张输出表及指标解释
```

客户数据、行政边界和算法输出默认被 `.gitignore` 排除，不应提交到 GitHub。

## 环境安装

建议使用 Python 3.10 或更高版本：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 输入表

| 输入 | 必需字段 | 说明 |
|---|---|---|
| 客户表 | `customer_id`, `city`, `lng`, `lat`, `area_admin_code`, `expected_fyp` | `area_admin_code` 当前实际存放行政街道名称，如“南桥镇” |
| 行政街道表 | `city`, `area_code`, `area_name`, `area_geometry` | Geometry 支持 GeoJSON、WKT、WKB/EWKB Hex；默认 WGS84 |
| 已有网格表 | `city`, `agent_net_id`, `basic_net_id`, `basic_net_geom` | 已占用空间，不参与新专员格划分 |
| FYP 门槛表 | `city`, `target_expected_fyp` | 每个城市的最低 FYP |
| 距离表 | `city`, `distance_km` | 专员格最大跨度/直径，算法自动除以 2 |

字段名不一致时，通过 `ColumnConfig` 映射，无需修改算法主体。

## Notebook 快速开始

```python
import pandas as pd

from satellite_grid_partition_v1 import (
    AlgorithmConfig,
    ColumnConfig,
    run_satellite_grid_algorithm,
    save_result_csv,
)

customer_df = pd.read_csv("data/customers.csv")
admin_df = pd.read_csv("data/admin_boundaries.csv")
existing_grid_df = pd.read_csv("data/existing_basic_grids.csv")
fyp_threshold_df = pd.read_csv("data/city_fyp_threshold.csv")
distance_df = pd.read_csv("data/city_distance.csv")

result = run_satellite_grid_algorithm(
    customer_df=customer_df,
    admin_df=admin_df,
    existing_grid_df=existing_grid_df,
    fyp_threshold_df=fyp_threshold_df,
    distance_df=distance_df,
    cols=ColumnConfig(),
    config=AlgorithmConfig(
        min_customer_count=50,
        build_grid_geometry=True,
    ),
)

save_result_csv(result, "satellite_grid_output")
```

运行后可直接访问：

```python
result.grids
result.h3_detail
result.grid_customer_detail
result.failed_seeds
result.abandoned_h3
result.coverage_metrics
result.h3_pool
result.customer_diagnostic
```

## 输出文件

| 文件 | DataFrame | 用途 |
|---|---|---|
| `01_grid_level.csv` | `result.grids` | 成功专员格主表 |
| `02_h3_detail.csv` | `result.h3_detail` | H3 到专员格的分配明细 |
| `03_failed_seeds.csv` | `result.failed_seeds` | 失败尝试及未达标原因 |
| `04_abandoned_h3.csv` | `result.abandoned_h3` | 最终未进入成功专员格的合法 H3 |
| `05_coverage_metrics.csv` | `result.coverage_metrics` | 城市级客户与 FYP 覆盖率漏斗 |
| `06_h3_pool_debug.csv` | `result.h3_pool` | 算法运行前的完整 H3 中间池 |
| `07_customer_diagnostic.csv` | `result.customer_diagnostic` | 客户映射与资格诊断 |
| `08_grid_customer_detail.csv` | `result.grid_customer_detail` | 客户维度分析大表，建议作为主要分析入口 |

每张表的字段、公式和业务解读见 [输出数据字典](docs/output-data-dictionary.md)。完整运行步骤和问题排查见 [操作指引](docs/operation-guide.md)。

## 重要数据口径

`H3 映射成功` 不等于 `客户拥有专员格`。正式判断请使用客户大表中的：

- `has_successful_grid=True`：客户所在 H3 已进入成功网格，且客户符合空间和数据资格。
- `grid_assignment_status=ASSIGNED`：同上，适合分类统计。
- `h3_assigned_to_grid=True` 但 `has_successful_grid=False`：H3 已分配，但该客户本身不符合计入规则。

## 当前待业务确认事项

- 同一行政街道出现多条 Geometry 时，是否代表合法的多个空间片区，或存在数据重复。
- Geometry 坐标系是否确定为 WGS84 / EPSG:4326，坐标顺序是否为经度、纬度。

程序支持 Polygon/MultiPolygon 和同街道多空间片区，但正式生产运行前仍建议向数据研发确认上述口径。

