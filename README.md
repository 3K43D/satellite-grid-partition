# 卫星网点专员格自动划分

基于行政街道边界、既有基础网格、客户位置、城市 FYP 门槛和距离门槛，使用 H3 与事务式 BFS 自动生成卫星网点专员格，并可按同城市唯一或最近原则归属到机构网点。

当前版本：**V1.2**。

## 核心规则

- H3 分辨率默认为 9。
- 当前客户点、行政街道和已有网格输入坐标系为 GCJ-02；算法先转换为 WGS84 再调用 H3。
- H3 行政归属和既有网格占用均按 **H3 中心点落入 Polygon** 判断。
- 默认每个“城市 × 行政街道”独立划分；可关闭行政街道限制，改为同一城市内允许跨街道生长。
- 城市距离参数按专员格最大跨度（直径）解释，算法半径为 `distance_km / 2`。
- 种子按“自身 FYP + 未分配一阶邻居 FYP”选择。
- BFS 同层依次按 FYP 降序、距种子距离升序、H3 ID 升序吸收。
- 专员格只有在 FYP 和去重客户数两个门槛同时达标时才提交。
- 最低客户数默认为 50，可通过 `AlgorithmConfig(min_customer_count=...)` 调整。
- FYP 已达标但人数不足时继续扩张；半径或拓扑耗尽仍不达标则回滚。
- 客户数按非空 `customer_id` 去重。`expected_fyp=0` 或为空的合法客户仍计人数；空值不贡献 FYP。
- 非空 `aoi_id` 的客户统一使用该 AOI 的质心经纬度映射 H3，保证同一 AOI 最多进入一个专员格；空 `aoi_id` 客户仍各自使用客户坐标。
- 专员格完成后才归属网点：同城市只有一个网点时直接归属；有多个时，选择距 Grid GCJ-02 几何质心最近的网点；同距离按网点名称升序。
- 客户不单独计算网点，只继承成功专员格的网点；无成功专员格的客户网点字段为空。
- 默认在终端 / Notebook 输出 10 个主阶段耗时，仅用于性能定位，不写入或影响算法结果。

## 仓库结构

```text
.
├── satellite_grid_partition_v1.py   # 主算法与文件运行入口
├── run_satellite_grid.py            # 推荐的终端运行入口
├── amap_grid_viewer.py               # 单个专员格高德地图预览
├── gcj02_to_wgs84_batch.py           # 独立的客户坐标反算实验工具
├── column_config.example.json        # 自定义输入列名模板
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
| 客户表 | `customer_id`, `city`, `lng`, `lat`, `area_admin_code`, `expected_fyp`, `aoi_id`, `aoi_lng`, `aoi_lat` | 经纬度均为 GCJ-02；非空 AOI 必须有一致质心；空 `aoi_id` 之间互不归组；关闭街道限制时 `area_admin_code` 可不提供 |
| 行政街道表 | `city`, `area_code`, `area_name`, `area_geometry` | 每个 `city + area_code` 一行；Geometry 支持 GeoJSON、WKT、WKB/EWKB Hex；坐标为 GCJ-02 |
| 已有网格表 | `city`, `agent_net_id`, `basic_net_id`, `basic_net_geom` | GCJ-02 已占用空间，不参与新专员格划分 |
| FYP 门槛表 | `city`, `target_expected_fyp` | 每个城市的最低 FYP |
| 距离表 | `city`, `distance_km` | 专员格最大跨度/直径，算法自动除以 2 |
| 网点经纬度表（可选） | `二级机构`, `城市`, `网点名称-正式`, `经度`, `纬度` | 网点是最小粒度，坐标为 GCJ-02；二级机构是省，仅输出，不参与匹配 |

字段名不一致时，通过 `ColumnConfig` 映射，无需修改算法主体。

AOI 输入不需要提前聚合成一行。算法保留原始客户明细：同一 AOI 的客户共享一个 AOI 质心 H3，但最低人数仍按原始非空客户号去重，FYP 仍按原始客户求和。`aoi_id` 非空但质心经纬度缺失、同一 AOI 跨城市或质心不一致时会直接报错。

如果实际列名为大写或中文，可配置：

```python
cols = ColumnConfig(
    customer_aoi_id="AOI_ID",
    customer_aoi_lng="AOI经度",
    customer_aoi_lat="AOI纬度",
)
```

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
outlet_df = pd.read_csv("data/outlet_locations.csv")

result = run_satellite_grid_algorithm(
    customer_df=customer_df,
    admin_df=admin_df,
    existing_grid_df=existing_grid_df,
    fyp_threshold_df=fyp_threshold_df,
    distance_df=distance_df,
    outlet_df=outlet_df,
    cols=ColumnConfig(),
    config=AlgorithmConfig(
        min_customer_count=50,
        input_coordinate_system="GCJ02",
        restrict_to_admin_street=True,
        build_grid_geometry=True,
    ),
)

save_result_csv(result, "satellite_grid_output")
```

需要允许同一城市内跨行政街道时，只改这一项：

```python
config = AlgorithmConfig(
    min_customer_count=50,
    input_coordinate_system="GCJ02",
    restrict_to_admin_street=False,
    build_grid_geometry=True,
)
```

此时城市内所有行政街道的合法 H3 在同一个池中参与 BFS，客户街道名称不再作为排除条件；已有基础网格、城市边界、距离、最低 50 人和 FYP 门槛继续生效。输出网格的 `admin_code`、`admin_name` 分别标记为 `CITY_WIDE`、`城市内跨行政街道`。

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

运行时会看到类似：

```text
[TIMING] 4/10 构建行政 H3 空间池: 85.310s | 120,000 个 H3
[TIMING] 5/10 客户清洗、AOI、坐标及 H3 聚合: 132.480s | 1,000,000 条客户记录
[TIMING] 6/10 BFS 划分与专员格 Geometry: 916.200s | 500 个成功专员格
[TIMING] 7/10 专员格归属网点: 0.280s | 500 个专员格
```

如需关闭计时输出，设置 `AlgorithmConfig(enable_timing=False)`。计时器不改变输入表、8 张输出表或专员格划分顺序。

`result.grids` 同时输出专员格几何质心和内部代表点的 WGS84、GCJ-02 经纬度。几何质心适合分析，但可能落在凹形或 MultiPolygon 外；高德地图标注建议使用 `grid_label_point_gcj02_lng`、`grid_label_point_gcj02_lat`。

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

## Terminal 运行与自定义列名

```bash
python run_satellite_grid.py \
  --customer data/customers.csv \
  --outlet data/outlet_locations.csv \
  --admin data/admin_boundaries.csv \
  --existing-grid data/existing_basic_grids.csv \
  --fyp-threshold data/city_fyp_threshold.csv \
  --distance data/city_distance.csv \
  --column-config column_config.json \
  --input-coordinate-system GCJ02 \
  --min-customer-count 50 \
  --allow-cross-admin-street \
  --output-dir satellite_grid_output
```

不传 `--allow-cross-admin-street` 时保持原有的不可跨街道逻辑。

列名不同时，复制 `column_config.example.json` 为 `column_config.json`，只修改右侧的真实列名。

## 高德地图展示一个专员格

算法输出的 `grid_geometry_geojson_gcj02` 可直接用于高德地图。申请 Web 端 JS API Key 和安全密钥后：

```bash
export AMAP_JS_API_KEY='你的 Web JS API Key'
export AMAP_SECURITY_JS_CODE='你的安全密钥'

python amap_grid_viewer.py \
  --grid-file satellite_grid_output/01_grid_level.csv \
  --list-grids

python amap_grid_viewer.py \
  --grid-file satellite_grid_output/01_grid_level.csv \
  --grid-id '要展示的grid_id'

python -m http.server 8000
```

浏览器打开 `http://localhost:8000/amap_grid_preview.html`。无需在高德控制台手工上传经纬度；模块会读取所选 Grid 的完整 Polygon/MultiPolygon。

## 单独测试 GCJ-02 反算

`gcj02_to_wgs84_batch.py` 与主算法相互独立，可在 Notebook 中批量转换客户点，用于控制点实验和人工抽样检查：

```python
import pandas as pd
from gcj02_to_wgs84_batch import convert_customer_coordinates

customer_df = pd.read_excel("客户数据.xlsx")

coordinate_check = convert_customer_coordinates(
    customer_df,
    customer_id_col="客户号",
    gcj_lng_col="客户经度",
    gcj_lat_col="客户纬度",
)

coordinate_check.to_parquet(
    "客户坐标_GCJ与WGS估算对照.parquet",
    index=False,
)
```

坐标反算、回算验证和距离指标都使用 NumPy 向量化执行；
所有原始字段和验证字段保留不变。百万级数据建议读写 Parquet。

其中 `model_roundtrip_error_m` 仅表示公开模型内部的正反算残差，不能作为相对于真实 WGS84 的误差。

## 重要数据口径

`H3 映射成功` 不等于 `客户拥有专员格`。正式判断请使用客户大表中的：

- `has_successful_grid=True`：客户所在 H3 已进入成功网格，且客户符合空间和数据资格。
- `grid_assignment_status=ASSIGNED`：同上，适合分类统计。
- `h3_assigned_to_grid=True` 但 `has_successful_grid=False`：H3 已分配，但该客户本身不符合计入规则。

## 已确认的数据口径

- 每个 `city + area_code` 只有一行；重复时程序会报错，提示检查取数逻辑。
- 客户经纬度、行政街道 Geometry 和已有网格 Geometry 均使用 GCJ-02，顺序为 `[经度, 纬度]`。
- H3 官方使用 WGS84 球面坐标，因此程序内部会统一转换；不要将 GCJ-02 经纬度直接与外部标准 H3 ID 混用。
