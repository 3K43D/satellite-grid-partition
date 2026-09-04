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

## 2. 准备输入数据

建议把数据放在本地 `data/` 目录。该目录已被 Git 忽略，避免客户信息和业务边界误传到远程仓库。

### 客户表

| 字段 | 类型建议 | 规则 |
|---|---|---|
| `customer_id` | string | 非空才计入客户数；完全重复行自动去重；同一 ID 的冲突记录会报错 |
| `city` | string | 与其他参数表城市名称一致 |
| `lng` | number | 经度，范围 `[-180, 180]` |
| `lat` | number | 纬度，范围 `[-90, 90]` |
| `area_admin_code` | string | 当前数据实际为行政街道名称，应与 `area_name` 对齐 |
| `expected_fyp` | number/null | 0 合法；空值客户仍计人数，但不贡献 FYP |

### 行政街道表

| 字段 | 规则 |
|---|---|
| `city` | 城市名称 |
| `area_code` | 行政街道唯一编码 |
| `area_name` | 行政街道名称 |
| `area_geometry` | GeoJSON、WKT 或 WKB/EWKB Hex；默认 WGS84 |

### 已有网格表

| 字段 | 规则 |
|---|---|
| `city` | 城市名称 |
| `agent_net_id` | 已有专员网格 ID |
| `basic_net_id` | 已有基础网格 ID |
| `basic_net_geom` | 已占用区域 Geometry |

### 城市参数表

- FYP 表：`city`, `target_expected_fyp`。
- 距离表：`city`, `distance_km`。该距离是直径，实际种子覆盖半径为其一半。

## 3. 首次试运行

优先在 Notebook 中运行，便于逐表检查：

```python
from satellite_grid_partition_v1 import *

config = AlgorithmConfig(
    h3_resolution=9,
    min_customer_count=50,
    require_customer_admin_match=True,
    build_grid_geometry=True,
)

result = run_satellite_grid_algorithm(
    customer_df,
    admin_df,
    existing_grid_df,
    fyp_threshold_df,
    distance_df,
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

## 6. 调整参数

```python
config = AlgorithmConfig(
    min_customer_count=60,       # 所有城市统一最低人数
    h3_resolution=9,
    build_grid_geometry=False,   # 大数据调试时关闭几何输出以提速
)
```

当前最低客户数是全局统一参数。如果以后需要按城市设置不同人数门槛，需要增加一张城市人数参数表或在城市参数表中新增字段。

## 7. 常见问题

### FYP 达标但人数不足

算法继续沿 BFS 合法邻接空间扩张，直到人数也达标。若半径或拓扑耗尽仍不足，事务回滚，失败原因是 `CUSTOMER_COUNT_NOT_REACHED`。

### 同一客户号出现多次

关键字段完全相同的重复记录自动保留一条；去重后同一非空客户号仍有多条记录时，程序报错并给出示例，必须先处理源数据冲突。

### 客户有 H3，但没有专员格

可能原因包括：落入已有网格、街道名称不一致、所在 H3 最终废弃，或整个局部区域无法同时满足 FYP/人数门槛。使用 `grid_assignment_status` 和诊断字段区分。

### 行政街道生成异常或 H3 数量异常

确认 Geometry 的 CRS 为 EPSG:4326、坐标顺序为 `[longitude, latitude]`，并检查同一 `area_code` 多行是否确为合法的多个空间片区。

### 运行速度慢或内存占用高

- 首轮调试设置 `build_grid_geometry=False`。
- 分城市运行小样本确认数据口径。
- 避免把明显超出目标城市范围的 Geometry 传入。
- H3 分辨率越高，单元数量增长越快；不要在未评估数据量时提高分辨率。

