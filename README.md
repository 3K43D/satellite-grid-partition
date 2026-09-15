# 卫星网点专员格自动划分

基于行政街道边界、既有基础网格、客户位置、城市 FYP 门槛和距离门槛，使用 H3 与事务式 BFS 自动生成卫星网点专员格，并可按同城市最近职场坐标归属到机构网点。

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
- 专员格完成后才归属网点：网点表每行代表一个职场坐标，同一网点允许有多个职场；选择距 Grid GCJ-02 几何质心最近的一行，同距离按网点名称、原始行顺序依次选择。
- 客户不单独计算网点，只继承成功专员格的网点；无成功专员格的客户网点字段为空。
- `pred_prob` 保留在客户维度大表中，仅供后续分析，不参与网格划分。
- 默认在终端 / Notebook 输出 10 个主阶段耗时，仅用于性能定位，不写入或影响算法结果。
- 客户坐标转换默认每 500,000 行分块处理，以降低千万级数据的中间数组峰值内存；转换公式、行顺序和输出不变。
- 大数据可一次传入全部城市，再按客户城市串行计算；每个城市只保存 `grid_customer_detail` Parquet 后即释放中间结果。单城市失败会记录原因并继续下一城市。

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
| 客户表 | `customer_id`, `city`, `lng`, `lat`, `area_admin_code`, `expected_fyp`, `pred_prob`, `aoi_id`, `aoi_lng`, `aoi_lat` | 经纬度均为 GCJ-02；`pred_prob` 只随客户明细输出；非空 AOI 必须有一致质心；空 `aoi_id` 之间互不归组；关闭街道限制时 `area_admin_code` 可不提供 |
| 行政街道表 | `city`, `area_code`, `area_name`, `area_geometry` | 每个 `city + area_code` 一行；Geometry 支持 GeoJSON、WKT、WKB/EWKB Hex；坐标为 GCJ-02 |
| 已有网格表 | `city`, `agent_net_id`, `basic_net_id`, `basic_net_geom` | GCJ-02 已占用空间，不参与新专员格划分 |
| FYP 门槛表 | `city`, `target_expected_fyp` | 每个城市的最低 FYP |
| 距离表 | `city`, `distance_km` | 专员格最大跨度/直径，算法自动除以 2 |
| 网点经纬度表（可选） | `二级机构`, `城市`, `网点名称-正式`, `经度`, `纬度` | 每行是一个 GCJ-02 职场坐标；同一网点可以有多行；二级机构是省，仅输出，不参与匹配 |

字段名不一致时，通过 `ColumnConfig` 映射，无需修改算法主体。

AOI 输入不需要提前聚合成一行。算法保留原始客户明细：同一 AOI 的客户共享一个 AOI 质心 H3，但最低人数仍按原始非空客户号去重，FYP 仍按原始客户求和。`aoi_id` 非空但质心经纬度缺失、同一 AOI 跨城市或质心不一致时会直接报错。

如果实际列名为大写或中文，可配置：

```python
cols = ColumnConfig(
    customer_pred_prob="客户预测概率",
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
    run_satellite_grid_by_city_to_parquet,
    run_satellite_grid_algorithm,
    save_result_csv,
    save_result_parquet,
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
        coordinate_transform_chunk_size=500_000,
    ),
)

save_result_parquet(result, "satellite_grid_output")

# 如需 Excel/WPS 更方便查看的 CSV，仍可使用：
# save_result_csv(result, "satellite_grid_output_csv")
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

### 千万级数据：按城市只保存客户大表

如果最终只需要各城市的 `grid_customer_detail`，推荐改用：

```python
from satellite_grid_partition_v1 import (
    AlgorithmConfig,
    ColumnConfig,
    run_satellite_grid_by_city_to_parquet,
)

cols = ColumnConfig()
config = AlgorithmConfig(
    min_customer_count=50,
    input_coordinate_system="GCJ02",
    restrict_to_admin_street=True,
    build_grid_geometry=True,
    coordinate_transform_chunk_size=500_000,
)

city_run_summary = run_satellite_grid_by_city_to_parquet(
    customer_df=customer_df,
    admin_df=admin_df,
    existing_grid_df=existing_grid_df,
    fyp_threshold_df=fyp_threshold_df,
    distance_df=distance_df,
    outlet_df=outlet_df,
    output_dir="city_customer_detail_output_20260915",
    cols=cols,
    config=config,
    compression="snappy",
    overwrite=False,
)

failed_cities = city_run_summary.query("status == 'FAILED'")[
    ["city", "error_type", "error_message"]
]
failed_cities
```

程序只遍历客户表中实际出现的城市，并按规范化后的城市名升序运行。文件名示例为 `0001_上海_grid_customer_detail.parquet`。该模式不保存 Grid、H3 明细、覆盖率等其他 7 张表，返回值只是一张很小的城市运行汇总表。

状态含义：

- `SUCCESS`：算法完成且客户 Parquet 写出成功。
- `SAVED_UNASSIGNED_NO_ADMIN_BOUNDARY`：客户城市没有行政边界；仍保存该城市全部客户，但 `grid_id` 为空。
- `FAILED`：该城市计算或保存失败，不生成该城市的有效输出文件；程序继续处理后续城市，并在最后打印失败城市及原因。

请每次使用新的空目录。`overwrite=False` 可防止意外覆盖；确实需要覆盖同名文件时才设为 `True`。如果更换了城市集合，直接复用旧目录可能留下上一次的旧文件。

为了让 `pd.read_parquet("整个输出文件夹")` 能同时读取所有城市，该模式会在落盘副本中统一标准数值和布尔字段的类型，并将其余 `object`/category 字段统一保存为字符串。字段值保留，且不会回写原始输入或改变算法判断。

该模式会降低“多个城市全部结果同时常驻内存”的峰值，但传入的全量 `customer_df` 本身仍需要放在内存中。全表缺少必需列、算法配置非法等无法归属到某一城市的结构性错误，仍会在开始前直接报错。

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
| `01_grid_level.parquet` | `result.grids` | 成功专员格主表 |
| `02_h3_detail.parquet` | `result.h3_detail` | H3 到专员格的分配明细 |
| `03_failed_seeds.parquet` | `result.failed_seeds` | 失败尝试及未达标原因 |
| `04_abandoned_h3.parquet` | `result.abandoned_h3` | 最终未进入成功专员格的合法 H3 |
| `05_coverage_metrics.parquet` | `result.coverage_metrics` | 城市级客户与 FYP 覆盖率漏斗 |
| `06_h3_pool_debug.parquet` | `result.h3_pool` | 算法运行前的完整 H3 中间池 |
| `07_customer_diagnostic.parquet` | `result.customer_diagnostic` | 客户映射与资格诊断 |
| `08_grid_customer_detail.parquet` | `result.grid_customer_detail` | 客户维度分析大表，建议作为主要分析入口 |

每张表的字段、公式和业务解读见 [输出数据字典](docs/output-data-dictionary.md)。完整运行步骤和问题排查见 [操作指引](docs/operation-guide.md)。

命令行和直接运行主文件默认保存 Snappy 压缩的 Parquet。需要 CSV 时，可在命令行增加 `--output-format csv`，或在 Notebook 中调用 `save_result_csv`。如果原始扩展字段在同一列混入了字符串、数字或其他不兼容对象，程序会仅在 Parquet 写出副本中将实际报错的列转成字符串，并列出字段名；内存中的 `result` 不会改变。

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
  --grid-file satellite_grid_output/01_grid_level.parquet \
  --list-grids

python amap_grid_viewer.py \
  --grid-file satellite_grid_output/01_grid_level.parquet \
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
