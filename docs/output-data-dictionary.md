# 输出数据字典与指标解读

算法通过 `AlgorithmResult` 返回 8 张 DataFrame。保存 CSV 后与下列文件一一对应。

## 1. `grids` / `01_grid_level.csv`

一行代表一个成功专员格。

| 字段 | 含义与解读 |
|---|---|
| `grid_id` | 专员格唯一 ID |
| `city` | 所属城市 |
| `admin_code`, `admin_name` | 开启街道限制时为所属行政街道；关闭时固定为 `CITY_WIDE`、`城市内跨行政街道` |
| `admin_restriction_enabled` | 本次运行是否启用行政街道硬边界 |
| `seed_h3` | 本格扩张起点 H3 |
| `seed_lat`, `seed_lng` | 种子 H3 中心点坐标 |
| `seed_wgs84_lat`, `seed_wgs84_lng` | 明确标注的种子 WGS84 坐标 |
| `seed_gcj02_lat`, `seed_gcj02_lng` | 高德地图使用的种子 GCJ-02 坐标 |
| `h3_count` | 最终包含的 H3 数量，不等于客户数 |
| `grid_fyp` | 本格内所有符合 FYP 口径客户的 `expected_fyp` 汇总 |
| `target_expected_fyp` | 所在城市的最低 FYP 门槛 |
| `value_utilization` | `grid_fyp / target_expected_fyp`；大于等于 1 表示达标 |
| `grid_customer_count` | 本格内符合空间资格的非空客户号去重数 |
| `min_customer_count` | 最低客户数门槛，默认 50 |
| `customer_count_utilization` | `grid_customer_count / min_customer_count`；大于等于 1 表示达标 |
| `max_seed_distance_m` | 本格所有 H3 中心到种子中心的最大距离 |
| `max_seed_radius_m` | 允许的最大种子覆盖半径，等于城市直径参数的一半 |
| `distance_diameter_km` | 原业务距离口径，即专员格最大跨度/直径 |
| `expansion_layers` | 主 BFS 达标时的最大层数；不把后续孤岛吸附作为新主层 |
| `grid_status` | 成功格固定为 `SUCCESS` |
| `grid_geometry_geojson` | 向后兼容字段，最终格 WGS84 GeoJSON |
| `grid_geometry_geojson_wgs84` | 明确标注的 WGS84 Polygon/MultiPolygon，适合标准 GIS/H3 |
| `grid_geometry_geojson_gcj02` | GCJ-02 Polygon/MultiPolygon，可直接用于高德地图 |
| `grid_centroid_wgs84_lng`, `grid_centroid_wgs84_lat` | 合并后专员格 Geometry 的 WGS84 几何质心；凹形或 MultiPolygon 时可能落在地块外 |
| `grid_centroid_gcj02_lng`, `grid_centroid_gcj02_lat` | 上述几何质心转换后的 GCJ-02 坐标 |
| `grid_label_point_wgs84_lng`, `grid_label_point_wgs84_lat` | 保证位于 WGS84 专员格 Geometry 内的代表点 |
| `grid_label_point_gcj02_lng`, `grid_label_point_gcj02_lat` | 保证位于 GCJ-02 专员格 Geometry 内的代表点，推荐用于高德标签或气泡 |
| `assigned_secondary_org` | 归属网点对应的二级机构（当前业务中为省）；不参与网点筛选 |
| `assigned_outlet_name` | 归属的正式网点名称；候选网点只取同城市记录 |
| `assigned_outlet_lng`, `assigned_outlet_lat` | 归属网点的 GCJ-02 经纬度 |
| `distance_to_assigned_outlet_km` | 专员格 GCJ-02 几何质心到归属网点的球面直线距离，单位千米 |
| `outlet_assignment_method` | 网点归属方式，取值见下表 |

推荐同时观察 `value_utilization` 与 `customer_count_utilization`：前者很高、后者接近 1，说明格子由少量高价值客户驱动；后者很高、前者接近 1，说明需要较多人才能满足 FYP。

当 `build_grid_geometry=False` 时，边界、几何质心和内部代表点字段保留但值为空。

`outlet_assignment_method`：

| 值 | 解读 |
|---|---|
| `ONLY_OUTLET_IN_CITY` | 该城市只有一个候选网点，直接归属 |
| `NEAREST_TO_GRID_CENTROID` | 该城市有多个候选网点，选择距专员格 GCJ-02 几何质心最近者；距离相同时按网点名称升序 |
| `NO_OUTLET_IN_CITY` | 网点表中没有该城市的网点，归属字段保留为空且不报错 |

## 2. `h3_detail` / `02_h3_detail.csv`

一行代表一个已进入成功专员格的 H3。

| 字段 | 含义与解读 |
|---|---|
| `grid_id` | 最终所属专员格 |
| `city`, `admin_code`, `admin_name` | 城市和划分范围；关闭街道限制时行政字段为城市级标记，不代表单一街道 |
| `admin_restriction_enabled` | 是否启用行政街道硬边界 |
| `h3_id` | H3 单元 ID |
| `h3_fyp` | 该 H3 内符合 FYP 口径的价值汇总 |
| `h3_customer_count` | 该 H3 内符合人数口径的去重客户数 |
| `seed_h3` | 所属格种子 H3 |
| `distance_to_seed_m` | H3 中心到种子中心距离 |
| `assignment_method` | `SEED`、`MAIN_BFS` 或 `ISLAND_MERGE` |
| `assignment_layer` | 种子为 0；主 BFS 为正整数；孤岛吸附为 -1 |

该表适合地图渲染、检查空间连续性，以及分析一个专员格由哪些 H3 构成。

## 3. `failed_seeds` / `03_failed_seeds.csv`

一行代表一次失败并回滚的种子尝试，不代表一个永久失败的客户或 H3。同一片区域可能由不同种子尝试多次。

| 字段 | 含义与解读 |
|---|---|
| `city`, `admin_code`, `admin_name` | 尝试所在区域 |
| `admin_restriction_enabled` | 是否启用行政街道硬边界 |
| `seed_h3` | 失败尝试的种子 |
| `seed_fyp`, `seed_customer_count` | 种子自身价值和客户数 |
| `seed_gravity_score` | 种子自身及未分配一阶邻居 FYP 之和 |
| `target_vmin` | 本次要求的最低 FYP，等同城市 `target_expected_fyp` |
| `min_customer_count` | 本次最低客户数 |
| `max_seed_radius_m` | 本次最大扩张半径 |
| `max_reachable_fyp` | 在拓扑和半径约束内最多可触达的 FYP |
| `max_reachable_customer_count` | 最多可触达的去重客户数 |
| `fyp_gap` | 距 FYP 门槛的缺口，达标则为 0 |
| `customer_count_gap` | 距人数门槛的缺口，达标则为 0 |
| `reachable_h3_count` | 本次最多触达的 H3 数 |
| `expansion_layers` | 本次触达的最大 BFS 层数 |
| `failure_reason` | 见下表 |

`failure_reason`：

| 值 | 解读 |
|---|---|
| `FYP_NOT_REACHED` | 人数可达标，但 FYP 不足 |
| `CUSTOMER_COUNT_NOT_REACHED` | FYP 可达标，但人数不足 |
| `FYP_AND_CUSTOMER_COUNT_NOT_REACHED` | 两个门槛都不足 |

## 4. `abandoned_h3` / `04_abandoned_h3.csv`

一行代表最终未进入任何成功专员格的合法 H3。

| 字段 | 含义与解读 |
|---|---|
| `city`, `admin_code`, `admin_name` | 所属区域 |
| `admin_restriction_enabled` | 是否启用行政街道硬边界 |
| `h3_id` | H3 ID |
| `h3_fyp` | H3 价值 |
| `h3_customer_count` | H3 去重客户数 |
| `center_lat`, `center_lng` | 向后兼容的 H3 中心点 WGS84 坐标 |
| `center_wgs84_lat`, `center_wgs84_lng` | 明确标注的 WGS84 中心点 |
| `center_gcj02_lat`, `center_gcj02_lng` | 高德地图使用的 GCJ-02 中心点 |
| `final_status` | 固定为 `ABANDONED` |

废弃不一定是数据错误：它可能是零客户空间、无法在半径内满足双门槛，或无法邻接吸附到已有成功格。

## 5. `coverage_metrics` / `05_coverage_metrics.csv`

一行代表一个城市的客户和 FYP 覆盖率漏斗。

| 字段 | 公式或含义 |
|---|---|
| `admin_restriction_enabled` | 本次运行是否启用行政街道硬边界 |
| `target_customer_count` | 非空客户号数量；客户号已全局去重 |
| `missing_customer_id_record_count` | 客户号为空的记录数，不进入人数分母 |
| `valid_coordinate_count` | 非空客户号中分配坐标有效的数量；有 AOI 时检查 AOI 质心，无 AOI 时检查客户坐标 |
| `valid_coordinate_rate` | `valid_coordinate_count / target_customer_count` |
| `fyp_available_count` | 非空客户号中 FYP 非空的数量 |
| `fyp_available_rate` | `fyp_available_count / target_customer_count` |
| `admin_geometry_matched_count` | H3 中心落入本城市行政边界的客户数 |
| `admin_geometry_matched_rate` | 上述数量 / 目标客户数 |
| `customer_admin_consistent_count` | 客户表街道名称与 Geometry 判定街道一致的客户数 |
| `customer_admin_consistent_rate` | 上述数量 / 目标客户数 |
| `existing_grid_excluded_count` | 落入已有基础网格而被排除的客户数 |
| `existing_grid_excluded_rate` | 上述数量 / 目标客户数 |
| `legal_candidate_customer_count` | 满足客户号、坐标、城市空间和既有网格规则的客户数；仅在开启街道限制时要求街道一致 |
| `legal_candidate_customer_rate` | 合法候选客户数 / 目标客户数 |
| `final_satellite_grid_customer_count` | 最终进入成功卫星专员格的合法客户数 |
| `overall_customer_coverage_rate` | 最终分配客户数 / 全部目标客户数 |
| `algorithm_candidate_coverage_rate` | 最终分配客户数 / 合法候选客户数；更适合评价算法本身 |
| `available_expected_fyp_total` | 所有非空客户号且 FYP 非空客户的价值总和 |
| `legal_candidate_expected_fyp` | 满足合法候选规则且 FYP 非空的价值总和 |
| `final_satellite_grid_expected_fyp` | 最终进入成功专员格的合法 FYP |
| `overall_value_coverage_rate` | 最终卫星格 FYP / 全部可用 FYP |
| `algorithm_candidate_value_coverage_rate` | 最终卫星格 FYP / 合法候选 FYP |

解读时应区分：

- `overall_*` 同时受数据质量、已有网格排除和算法成格能力影响。
- `algorithm_candidate_*` 已排除前置数据和业务资格问题，更接近算法成格效果。
- 分母为 0 时比率返回空值，而不是 0。

## 6. `h3_pool` / `06_h3_pool_debug.csv`

完整行政空间 H3 中间池，包括有客户和无客户 H3，主要用于研发调试。

| 字段 | 含义与解读 |
|---|---|
| `city`, `admin_code`, `admin_name` | H3 的行政归属 |
| `admin_restriction_enabled` | 本次运行是否启用行政街道硬边界；关闭时行政归属只用于空间来源和诊断 |
| `h3_id` | H3 ID |
| `center_lat`, `center_lng` | 向后兼容的 H3 中心点 WGS84 坐标 |
| `center_wgs84_lat`, `center_wgs84_lng` | 明确标注的 WGS84 中心点 |
| `center_gcj02_lat`, `center_gcj02_lng` | 高德地图使用的 GCJ-02 中心点 |
| `is_existing_occupied` | 中心点是否落入已有基础网格 |
| `h3_expected_fyp` | 合法且 FYP 非空客户的价值汇总；无客户为 0 |
| `h3_customer_count` | 合法非空客户号去重数；可包含 FYP 为空客户 |
| `is_legal_unassigned` | 是否进入新专员格算法候选空间，即未被已有网格占用 |

该表中的 H3 数量通常远大于有客户 H3 数量，因为算法保留零客户 H3 以维持空间拓扑连续。

## 7. `customer_diagnostic` / `07_customer_diagnostic.csv`

一行对应一条去重后的客户记录，保留原始字段并增加诊断字段。

| 字段 | 含义与解读 |
|---|---|
| `_customer_id_norm` | 标准化后的客户号 |
| `_customer_id_available` | 客户号是否非空 |
| `_lng`, `_lat` | 转成数值后的经纬度 |
| `_input_coordinate_system` | 输入坐标系，当前为 `GCJ02` |
| `_admin_restriction_enabled` | 本次运行是否启用行政街道硬边界 |
| `_customer_coordinate_valid` | 原始客户经纬度是否合法 |
| `_customer_wgs84_lng`, `_customer_wgs84_lat` | 原始客户坐标转换后的 WGS84 |
| `_aoi_id_norm`, `_has_aoi` | 标准化 AOI ID，以及该客户是否具有 AOI |
| `_aoi_lng_canonical`, `_aoi_lat_canonical` | 同一 AOI 统一后的代表质心经纬度 |
| `_aoi_customer_count`, `_aoi_expected_fyp` | AOI 原始去重客户数和 FYP 合计，仅用于解释与验收 |
| `_allocation_lng`, `_allocation_lat` | 实际分配坐标；有 AOI 使用 AOI 质心，否则使用客户坐标 |
| `_allocation_wgs84_lng`, `_allocation_wgs84_lat` | 实际送入 H3 的 WGS84 分配坐标 |
| `_allocation_coordinate_source` | `AOI_CENTROID` 或 `CUSTOMER_POINT` |
| `_wgs84_lng`, `_wgs84_lat` | 向后兼容字段，等同实际 H3 分配坐标的 WGS84 值 |
| `_fyp` | 转成数值后的 FYP；无法解析时为空 |
| `_valid_coordinate` | 实际分配坐标是否有效 |
| `_fyp_available` | FYP 是否可用；0 为可用 |
| `_h3_id` | 客户坐标映射的 H3；失败时为空 |
| `_h3_admin_code`, `_h3_admin_name` | 由 H3 中心和行政 Geometry 判定的街道 |
| `_h3_in_admin_geometry` | 是否落入行政 H3 池 |
| `_customer_admin_name_norm` | 客户表街道名称标准化值 |
| `_h3_admin_name_norm` | Geometry 判定街道名称标准化值 |
| `_admin_consistent` | 两种街道名称是否一致 |
| `_h3_existing_occupied` | 对应 H3 是否被已有网格占用 |
| `_excluded_by_existing_grid` | 客户是否因此被排除 |
| `_legal_customer_for_count` | 是否计入最低客户数 |
| `_legal_h3_candidate` | 是否同时贡献 H3 FYP；比人数口径额外要求 FYP 可用 |

原始客户表的其他字段也会继续保留。该表适合排查“为什么某客户未参与算法”，不应直接用 `_h3_id` 判断最终是否拥有专员格。

## 8. `grid_customer_detail` / `08_grid_customer_detail.csv`

客户维度最终分析大表，一行对应一条去重后的客户记录。它连接客户诊断、H3 分配和 Grid 指标，是日常分析的首选输出。

### 客户和位置字段

| 字段 | 含义 |
|---|---|
| `customer_id`, `city` | 客户号和城市 |
| `customer_lng`, `customer_lat` | 数值化经纬度 |
| `customer_coordinate_system` | 上述客户坐标的坐标系，当前为 `GCJ02` |
| `customer_wgs84_lng`, `customer_wgs84_lat` | 原始客户坐标转换后的 WGS84 |
| `expected_fyp` | 数值化客户 FYP |
| `aoi_id`, `has_aoi` | 原始 AOI ID 和是否具有 AOI；空 AOI 客户互不归组 |
| `aoi_lng`, `aoi_lat` | 同一 AOI 统一后的代表质心经纬度 |
| `aoi_customer_count`, `aoi_expected_fyp` | AOI 中原始去重客户数和 FYP 合计 |
| `allocation_lng`, `allocation_lat` | 实际用于分配的位置；有 AOI 时为 AOI 质心，否则为客户位置 |
| `allocation_wgs84_lng`, `allocation_wgs84_lat` | 实际送入 H3 的 WGS84 分配坐标 |
| `allocation_coordinate_source` | `AOI_CENTROID` 或 `CUSTOMER_POINT` |
| `customer_admin_name` | 客户表行政街道名称 |
| `h3_id` | 实际分配坐标所在 H3；有 AOI 时是 AOI 质心 H3，否则是客户 H3 |
| `_h3_admin_code`, `_h3_admin_name` | Geometry 判定的行政街道 |

### 最终分配判断

| 字段 | 含义 |
|---|---|
| `grid_id` | 最终所属专员格；没有成功格时为空 |
| `grid_admin_code`, `grid_admin_name` | 网格划分范围；关闭街道限制时为 `CITY_WIDE`、`城市内跨行政街道`，客户自身街道仍看 `customer_admin_name` |
| `admin_restriction_enabled` | 本次运行是否启用行政街道硬边界 |
| `h3_assigned_to_grid` | 客户所在 H3 是否进入成功网格，仅为空间判断 |
| `has_successful_grid` | H3 已成功分格且客户符合人数资格；正式业务判断字段 |
| `counts_toward_grid_customer_minimum` | 该客户是否计入所属格最低人数，目前等同 `has_successful_grid` |
| `grid_assignment_status` | `ASSIGNED`、`H3_ASSIGNED_BUT_CUSTOMER_INELIGIBLE` 或 `UNASSIGNED` |

状态解释：

- `ASSIGNED`：客户正式属于成功专员格。
- `H3_ASSIGNED_BUT_CUSTOMER_INELIGIBLE`：空间 H3 属于成功格，但客户自身因客户号、坐标、已有网格或街道一致性等规则不合格。
- `UNASSIGNED`：客户所在 H3 没有进入成功格，或客户无法映射到 H3。

### H3 和 Grid 指标

| 字段 | 含义 |
|---|---|
| `assigned_h3_fyp`, `assigned_h3_customer_count` | 客户所在已分配 H3 的价值和去重客户数 |
| `grid_seed_h3`, `distance_to_grid_seed_m` | 所属格种子及距离 |
| `h3_assignment_method`, `h3_assignment_layer` | H3 如何进入该格及 BFS 层级 |
| `grid_centroid_wgs84_lng`, `grid_centroid_wgs84_lat` | 所属专员格的 WGS84 几何质心 |
| `grid_centroid_gcj02_lng`, `grid_centroid_gcj02_lat` | 所属专员格的 GCJ-02 几何质心 |
| `grid_label_point_wgs84_lng`, `grid_label_point_wgs84_lat` | 所属专员格的 WGS84 内部代表点 |
| `grid_label_point_gcj02_lng`, `grid_label_point_gcj02_lat` | 所属专员格的 GCJ-02 内部代表点，推荐用于高德展示 |
| `h3_count` | 所属格 H3 数量 |
| `grid_fyp`, `target_expected_fyp`, `value_utilization` | 所属格价值、门槛和达成倍数 |
| `grid_customer_count`, `min_customer_count`, `customer_count_utilization` | 所属格客户数、人数门槛和达成倍数 |
| `max_seed_distance_m`, `max_seed_radius_m`, `distance_diameter_km` | 所属格距离指标 |
| `grid_expansion_layers`, `grid_status` | 所属格扩张层数和状态 |

### 归属网点字段

| 字段 | 含义 |
|---|---|
| `assigned_secondary_org` | 成功专员格归属网点对应的二级机构（省） |
| `assigned_outlet_name` | 客户所属成功专员格的归属网点；客户本身不重复计算最近网点 |
| `assigned_outlet_lng`, `assigned_outlet_lat` | 归属网点的 GCJ-02 经纬度 |
| `distance_to_assigned_outlet_km` | 所属专员格 GCJ-02 几何质心到归属网点的球面直线距离，单位千米；同一专员格客户取值相同 |
| `outlet_assignment_method` | 成功格客户继承 `ONLY_OUTLET_IN_CITY`、`NEAREST_TO_GRID_CENTROID` 或 `NO_OUTLET_IN_CITY`；没有成功专员格的客户为 `NO_SUCCESSFUL_GRID` |

只有 `grids` 和 `grid_customer_detail` 包含网点归属字段。其他六张输出表不增加这些字段，且网点归属不参与专员格划分。

表尾还保留客户诊断字段和原始输入字段，便于在一张表中完成客户、行政街道、H3、专员格和 FYP 的联合分析。

## 守恒关系

算法结束前自动校验：

1. 合法候选 H3 = 已分配 H3 + 废弃 H3。
2. 合法候选 FYP = 已分配 FYP + 废弃 FYP。
3. 合法候选去重客户数 = 已分配客户数 + 废弃客户数。
4. 每个成功格的 Grid 汇总与 H3 明细一致。
5. 每个成功格同时满足 FYP、最低客户数、半径和拓扑连通性约束。

校验失败会抛出异常，应先处理问题再使用输出结果。
