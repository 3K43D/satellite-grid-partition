# 操作指引

## 1. 准备环境

```bash
git clone <仓库地址>
cd satellite-grid-partition
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Windows PowerShell 激活虚拟环境：

```powershell
.venv\Scripts\Activate.ps1
```

算法默认在终端 / Notebook 打印 10 个主阶段和总耗时。
这些计时仅用于判断慢在 H3 空间池、客户处理、BFS 还是结果组装，
不参与任何业务计算。如需关闭：

```python
config = AlgorithmConfig(
    enable_timing=False,
)
```

## 2. 准备输入数据

建议把数据放在本地 `data/` 目录。该目录已被 Git 忽略，避免客户信息和业务边界误传到远程仓库。

### 客户表

| 字段 | 类型建议 | 规则 |
|---|---|---|
| `customer_id` | string | 非空才计入客户数；完全重复行自动去重；同一 ID 的冲突记录会报错 |
| `city` | string | 与其他参数表城市名称一致 |
| `lng` | number | GCJ-02 经度，范围 `[-180, 180]` |
| `lat` | number | GCJ-02 纬度，范围 `[-90, 90]` |
| `area_admin_code` | string | 当前数据实际为行政街道名称；开启街道限制时应与 `area_name` 对齐，关闭时可不提供 |
| `expected_fyp` | number/null | 0 合法；空值客户仍计人数，但不贡献 FYP |
| `aoi_id` | string/null | 客户所属 AOI；空值表示无 AOI，每条空值客户独立处理，不会归为同一组 |
| `aoi_lng` | number/null | AOI 质心经度；`aoi_id` 非空时必填，并与其他空间输入使用相同坐标系 |
| `aoi_lat` | number/null | AOI 质心纬度；同一 `aoi_id` 的质心经纬度必须一致 |

客户表不需要预先聚合。同一 AOI 的原始客户行全部保留，算法只把它们的 H3 分配位置统一替换为 AOI 质心。最低人数仍按原始非空客户号去重，FYP 仍按原始客户行求和。

### 行政街道表

| 字段 | 规则 |
|---|---|
| `city` | 城市名称 |
| `area_code` | 行政街道唯一编码 |
| `area_name` | 行政街道名称 |
| `area_geometry` | GeoJSON、WKT 或 WKB/EWKB Hex；当前为 GCJ-02；每个 `city + area_code` 一行 |

即使关闭行政街道限制，行政街道表仍需输入：算法会使用同一城市所有街道 Geometry 的整体覆盖范围构造城市 H3 空间，但不会再把街道边界作为 BFS 隔离线。

### 已有网格表

| 字段 | 规则 |
|---|---|
| `city` | 城市名称 |
| `agent_net_id` | 已有专员网格 ID |
| `basic_net_id` | 已有基础网格 ID |
| `basic_net_geom` | GCJ-02 已占用区域 Geometry |

### 城市参数表

- FYP 表：`city`, `target_expected_fyp`。
- 距离表：`city`, `distance_km`。该距离是直径，实际种子覆盖半径为其一半。

### 网点经纬度表（可选）

| 字段 | 规则 |
|---|---|
| `二级机构` | 省级机构；随归属网点输出，不参与候选筛选 |
| `城市` | 与 Grid 的 `city` 匹配 |
| `网点名称-正式` | 同一城市内必须唯一；同距离时按该名称升序选择 |
| `经度`, `纬度` | 网点点位的 GCJ-02 坐标；缺失或非法时直接报错 |

归属发生在专员格划分完成之后，不影响 Seed、BFS、FYP、
最低客户数或 AOI 结果。同城市仅一个网点时直接归属；有多个时，
用 `grid_centroid_gcj02_lng/lat` 选择直线距离最近的网点。
没有网点的城市保留空归属，不报错。

## 3. 首次试运行

优先在 Notebook 中运行，便于逐表检查：

```python
import pandas as pd

from satellite_grid_partition_v1 import *

outlet_df = pd.read_parquet("data/outlet_locations.parquet")

config = AlgorithmConfig(
    h3_resolution=9,
    min_customer_count=50,
    input_coordinate_system="GCJ02",
    restrict_to_admin_street=True,
    require_customer_admin_match=True,
    build_grid_geometry=True,
)

result = run_satellite_grid_algorithm(
    customer_df=customer_df,
    admin_df=admin_df,
    existing_grid_df=existing_grid_df,
    fyp_threshold_df=fyp_threshold_df,
    distance_df=distance_df,
    outlet_df=outlet_df,
    config=config,
)
```

修改 `.py` 后，如果 Notebook 已导入过旧版本，请重启 Kernel，或显式重新加载模块。

## 4. 推荐验收顺序

### 第一步：检查客户诊断

查看 `result.customer_diagnostic`：

```python
result.customer_diagnostic[[
    "_customer_id_available",
    "_valid_coordinate",
    "_fyp_available",
    "_h3_in_admin_geometry",
    "_admin_consistent",
    "_excluded_by_existing_grid",
    "_legal_customer_for_count",
    "_legal_h3_candidate",
]].mean()
```

重点关注坐标无效、行政街道不一致、落入已有网格的比例。

### 第二步：检查城市覆盖率

```python
result.coverage_metrics.sort_values("overall_customer_coverage_rate")
```

优先定位覆盖率较低城市，再结合失败 Seed 和废弃 H3 分析原因。

### 第三步：检查成功网格双门槛

```python
assert (
    result.grids["grid_fyp"]
    >= result.grids["target_expected_fyp"]
).all()

assert (
    result.grids["grid_customer_count"]
    >= result.grids["min_customer_count"]
).all()
```

主函数本身也会执行 FYP、客户数、H3 守恒、半径和连通性校验；校验失败会抛出异常，不会静默输出错误结果。

### 第四步：使用客户大表分析

```python
assigned_customers = result.grid_customer_detail.query(
    "has_successful_grid == True"
)

grid_analysis = (
    assigned_customers.groupby(
        ["city", "grid_admin_name", "grid_id"],
        as_index=False,
    )
    .agg(
        customer_count=("customer_id", "nunique"),
        expected_fyp=("expected_fyp", "sum"),
    )
)
```

不要用 `_h3_id` 非空作为“成功分格”判断；H3 映射成功只说明坐标可转换。

## 5. 保存输出

```python
save_result_csv(result, "satellite_grid_output")
```

CSV 使用 UTF-8-SIG 编码，可直接用 Excel/WPS 打开。所有输出的解释见 `docs/output-data-dictionary.md`。

## 6. 在 Terminal 中运行

```bash
python run_satellite_grid.py \
  --customer data/customers.csv \
  --outlet data/outlet_locations.csv \
  --admin data/admin_boundaries.csv \
  --existing-grid data/existing_basic_grids.csv \
  --fyp-threshold data/city_fyp_threshold.csv \
  --distance data/city_distance.csv \
  --input-coordinate-system GCJ02 \
  --min-customer-count 50 \
  --output-dir satellite_grid_output
```

使用 `python run_satellite_grid.py --help` 查看全部参数。

### 输入列名不一致

复制示例：

```bash
cp column_config.example.json column_config.json
```

将 JSON 右侧修改为真实字段名，例如：

```json
{
  "customer_id": "客户号",
  "customer_city": "城市名称",
  "customer_lng": "客户经度",
  "customer_lat": "客户纬度",
  "customer_admin_code": "行政街道名称",
  "customer_expected_fyp": "预计FYP",
  "customer_aoi_id": "AOI_ID",
  "customer_aoi_lng": "AOI经度",
  "customer_aoi_lat": "AOI纬度",
  "admin_city": "城市名称",
  "admin_code": "街道编码",
  "admin_name": "街道名称",
  "admin_geometry": "街道边界",
  "existing_city": "城市名称",
  "existing_agent_grid_id": "专员格ID",
  "existing_basic_grid_id": "基础格ID",
  "existing_geometry": "基础格边界",
  "threshold_city": "城市名称",
  "threshold_fyp": "最低FYP",
  "distance_city": "城市名称",
  "distance_km": "最大跨度KM",
  "outlet_secondary_org": "二级机构",
  "outlet_city": "城市",
  "outlet_name": "网点名称-正式",
  "outlet_lng": "经度",
  "outlet_lat": "纬度"
}
```

然后增加：

```bash
--column-config column_config.json
```

### GCJ-02 与 H3

所有输入空间数据必须使用同一种坐标系。当前配置为 GCJ-02，程序会将客户点、行政街道边界和已有网格边界反算成 WGS84，再调用 H3。输出 Grid 同时提供：

- `grid_geometry_geojson`：兼容字段，WGS84。
- `grid_geometry_geojson_wgs84`：明确标注的 WGS84。
- `grid_geometry_geojson_gcj02`：高德地图直接展示用。

GCJ-02 到 WGS84 没有高德官方反向接口，项目采用迭代反算，并通过 WGS84→GCJ-02 正向计算收敛。若以后研发可直接提供 WGS84 数据，应设置 `--input-coordinate-system WGS84`，可避免反算。

## 7. 高德地图展示一个专员格

### 7.1 申请 Key

在高德开放平台创建应用，添加“Web端（JS API）”Key，取得：

- Web JS API Key
- 安全密钥（securityJsCode）

不要把 Key 或安全密钥写进源码、JSON 配置或提交到 GitHub。

### 7.2 设置当前 Terminal 环境变量

```bash
export AMAP_JS_API_KEY='你的 Web JS API Key'
export AMAP_SECURITY_JS_CODE='你的安全密钥'
```

### 7.3 选择专员格

先列出 ID：

```bash
python amap_grid_viewer.py \
  --grid-file satellite_grid_output/01_grid_level.csv \
  --list-grids
```

再生成一个专员格的页面：

```bash
python amap_grid_viewer.py \
  --grid-file satellite_grid_output/01_grid_level.csv \
  --grid-id '上海市_310120_G000001' \
  --output amap_grid_preview.html
```

这里不需要向高德控制台上传经纬度。脚本会从 `grid_geometry_geojson_gcj02` 读取该专员格的全部 Polygon/MultiPolygon 顶点，并调用高德 JS API 绘制。

### 7.4 打开页面

```bash
python -m http.server 8000
```

访问：

```text
http://localhost:8000/amap_grid_preview.html
```

如果 Grid 表的列名不同：

```bash
python amap_grid_viewer.py \
  --grid-file your_grid.xlsx \
  --grid-id '目标ID' \
  --grid-id-column '你的网格ID列' \
  --geometry-column '你的边界列' \
  --geometry-coordinate-system GCJ02
```

生成的 HTML 内含当前 Key 配置，只用于本地预览，不要提交到 Git。

## 8. 调整参数

```python
config = AlgorithmConfig(
    min_customer_count=60,       # 所有城市统一最低人数
    h3_resolution=9,
    restrict_to_admin_street=True,
    build_grid_geometry=False,   # 大数据调试时关闭几何输出以提速
)
```

当前最低客户数是全局统一参数。如果以后需要按城市设置不同人数门槛，需要增加一张城市人数参数表或在城市参数表中新增字段。

### 是否限制在同一行政街道

保持原逻辑、禁止跨行政街道：

```python
config = AlgorithmConfig(
    restrict_to_admin_street=True,
)
```

允许同一城市内跨行政街道：

```python
config = AlgorithmConfig(
    restrict_to_admin_street=False,
)
```

关闭限制后，算法按城市建立统一邻接池，客户街道名称不再影响人数和 FYP 资格。已有基础网格占用区仍然排除，网格仍然不能跨城市，距离、最低客户数和 FYP 门槛均保持不变。城市级网格的行政字段标记为 `CITY_WIDE` 和 `城市内跨行政街道`。

`require_customer_admin_match` 只在 `restrict_to_admin_street=True` 时生效。

## 9. 常见问题

### FYP 达标但人数不足

算法继续沿 BFS 合法邻接空间扩张，直到人数也达标。若半径或拓扑耗尽仍不足，事务回滚，失败原因是 `CUSTOMER_COUNT_NOT_REACHED`。

### 同一客户号出现多次

关键字段完全相同的重复记录自动保留一条；去重后同一非空客户号仍有多条记录时，程序报错并给出示例，必须先处理源数据冲突。

### 客户有 H3，但没有专员格

可能原因包括：落入已有网格、街道名称不一致、所在 H3 最终废弃，或整个局部区域无法同时满足 FYP/人数门槛。使用 `grid_assignment_status` 和诊断字段区分。

### 行政街道生成异常或 H3 数量异常

确认输入确为 GCJ-02、坐标顺序为 `[longitude, latitude]`，并检查是否意外出现重复的 `city + area_code`。

### 运行速度慢或内存占用高

- 首轮调试设置 `build_grid_geometry=False`。
- 分城市运行小样本确认数据口径。
- 避免把明显超出目标城市范围的 Geometry 传入。
- H3 分辨率越高，单元数量增长越快；不要在未评估数据量时提高分辨率。
