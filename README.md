# 多层配额原子试算 API

基于 **Python + FastAPI + Pydantic v2** 的令牌桶配额试算服务。给定若干令牌桶与一条带时间戳的请求序列，服务按时间顺序模拟连续补充，并对每次请求执行 **桶链原子扣减**：

- 每个桶以 `refill_rate`（令牌/秒）按时间差**连续补充**，余额上限为 `capacity`；
- 请求携带一条桶链 `chain`，链上**每个桶**都需满足 `余额 >= cost`；
- 全部充足 → 链上每个桶原子扣减 `cost`；任一不足 → **全部桶保持不变**，并返回链上第一个不足的桶（`rejected_by`）；
- 逐项返回放行结果、限制桶以及链上各桶扣减前后余额。

另提供 **等待时间预测** 接口 `POST /forecast`：给定当前时刻各桶快照（容量/速率/当前余额）、请求成本与桶链，在**不扣减任何状态**的前提下，计算链上全部桶首次同时满足成本所需的等待秒数，返回预计可通过时刻、等待最长的限制桶与各桶预计余额；零速率或容量不足导致永远不可达时返回明确的不可达标记。

以及 **初始余额规划** 接口 `POST /plan-initial-balances`：请求体与 `/simulate` 相同，以**所有请求均获放行**为目标，在 `[0, capacity]` 内为每个桶反推满足其全部链上消耗的最低 `initial`（误差 ≤ 1e-6），返回各桶 `required_initial`、相对请求 `initial` 的 `delta` 以及用规划值完整回放得到的 `final_balances`；满桶起步仍不可行时返回 `reachable = false` 与按处理顺序首个无法满足的请求和桶。

以及 **配额使用分析** 接口 `POST /usage-report`：请求体在 `/simulate` 基础上增加 `window_seconds`，按与 `/simulate` 完全一致的连续补充与链式原子判定回放请求序列，再按时间窗口统计每个桶的**放行消耗**、**拒绝次数**与**峰值占用**（= `capacity` − 窗口内最低余额），并给出每桶达到峰值的窗口起止时间与全局最忙窗口。

以及 **区间预留试算** 接口 `POST /reservation-simulate`：给定只有容量上限（无补充概念）的桶与一批携带 `start` / `end` / `cost` / `chain` 的区间预留，按 `start` 升序处理；仅当预留的整个半开区间 `[start, end)` 内链上每个桶的并发占用加 `cost` 处处不超过 `capacity` 时才**原子接受**，否则拒绝且不改变任何占用。返回按原始输入索引排列的逐项结果，以及每个桶**合并后的占用区间**与**峰值并发**。

以及 **公平性报告** 接口 `POST /fairness-report`：请求体在 `/usage-report` 基础上为每个请求附加 `principal`（主体）与 `weight`（公平份额权重），沿用与 `/simulate` 完全一致的时间排序、连续补充与链式原子扣减回放，按 principal 统计**放行消耗**、**拒绝次数**与**加权 Jain 公平指数**；并按 `window_seconds` 将每个窗口的实际获得量与理论权重份额对齐，找出**最受限 principal** 及造成其短缺的**首个桶**，同时给出全局与逐窗口报告。

## 一条命令启动

需要已安装 Docker（含 Compose 插件）。在本目录执行：

```bash
docker compose up --build
```

启动后访问地址与端口：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | http://localhost:8000/health |
| 试算接口 | http://localhost:8000/simulate （POST） |
| 预测接口 | http://localhost:8000/forecast （POST） |
| 规划接口 | http://localhost:8000/plan-initial-balances （POST） |
| 使用分析接口 | http://localhost:8000/usage-report （POST） |
| 预留试算接口 | http://localhost:8000/reservation-simulate （POST） |
| 公平性报告接口 | http://localhost:8000/fairness-report （POST） |
| Swagger 交互文档 | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |

依赖安装与启动初始化均已封装进镜像（见 `Dockerfile` 与 `entrypoint.sh`），宿主机无需 Python 环境。
后台运行可用 `docker compose up --build -d`，停止用 `docker compose down`。

## 请求格式 `POST /simulate`

```json
{
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
    {"id": "api_qps", "capacity": 5, "refill_rate": 5}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-2", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]}
  ]
}
```

### 字段说明

**桶 `buckets[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 桶唯一标识，非空字符串 |
| `capacity` | 是 | 桶容量（令牌上限），必须 > 0 |
| `refill_rate` | 是 | 连续补充速率（令牌/秒），必须 ≥ 0 |
| `initial` | 否 | 初始余额，默认等于 `capacity`；要求 0 ≤ initial ≤ capacity |

**请求 `requests[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 请求唯一标识 |
| `timestamp` | 是 | 请求时间（秒，任意一致的时间原点，如相对秒或 Unix 时间） |
| `cost` | 是 | 消耗量，必须 > 0；从链上**每个**桶各扣此数量 |
| `chain` | 是 | 桶 ID 列表（至少 1 个），同一链内不可重复 |

### 模拟语义

1. **时间顺序**：所有请求按 `timestamp` 升序处理；同一时间戳按输入数组顺序处理。`results` 仍按原始数组顺序返回（含 `index`）。
2. **连续补充**：桶在模拟起始时刻（最早请求时间戳）持有 `initial` 余额，之后每次被请求触及时按 `(当前时间 - 上次更新时间) × refill_rate` 惰性补充，封顶 `capacity`。因此时间戳可以是任意大小（Unix 秒等），只取决于时间差。
3. **原子扣减**：判定时先对链上所有桶补充到当前时刻，再按链顺序找第一个余额不足的桶。不足则全部不扣，`balances` 中各桶 `after == before`；充足则每个桶都扣 `cost`。
4. **终态**：`final_balances` 给出所有桶推进到最后一个时间戳后的余额。

## 响应示例

```json
{
  "allowed_count": 2,
  "rejected_count": 1,
  "results": [
    {
      "request_id": "req-1",
      "index": 0,
      "timestamp": 0.0,
      "allowed": true,
      "rejected_by": null,
      "balances": {
        "tenant_quota": {"before": 100.0, "after": 96.0},
        "api_qps": {"before": 5.0, "after": 1.0}
      }
    },
    {
      "request_id": "req-2",
      "index": 1,
      "timestamp": 0.0,
      "allowed": false,
      "rejected_by": "api_qps",
      "balances": {
        "tenant_quota": {"before": 96.0, "after": 96.0},
        "api_qps": {"before": 1.0, "after": 1.0}
      }
    }
  ],
  "final_balances": {"tenant_quota": 96.0, "api_qps": 1.0}
}
```

- `before`：该请求时刻补充之后、扣减之前的余额；
- `after`：扣减之后的余额；被拒绝时与 `before` 相同（原子性）。

## 请求格式 `POST /forecast`

```json
{
  "now": 1000,
  "cost": 4,
  "chain": ["tenant_quota", "api_qps", "user_quota"],
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "balance": 96},
    {"id": "api_qps", "capacity": 5, "refill_rate": 5, "balance": 1},
    {"id": "user_quota", "capacity": 20, "refill_rate": 1, "balance": 2}
  ]
}
```

### 字段说明

**顶层**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `now` | 是 | 当前时刻（秒，任意一致的时间原点，如相对秒或 Unix 时间） |
| `cost` | 是 | 请求成本，必须 > 0；链上**每个**桶都需持有此数量 |
| `chain` | 是 | 桶 ID 列表（至少 1 个），同一链内不可重复 |
| `buckets` | 是 | 桶状态快照集合（至少 1 个） |

**桶快照 `buckets[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 桶唯一标识，非空字符串 |
| `capacity` | 是 | 桶容量（令牌上限），必须 > 0 |
| `refill_rate` | 是 | 连续补充速率（令牌/秒），必须 ≥ 0 |
| `balance` | 是 | 当前余额（`now` 时刻持有），要求 0 ≤ balance ≤ capacity |

### 预测语义

1. **纯计算**：不扣减、不修改任何状态；`now` 可以是任意一致的时间原点。
2. **单桶等待**：余额已满足 `cost` → 等待 0；否则若 `refill_rate > 0`，等待 `(cost - balance) / refill_rate` 秒。
3. **链等待**：链上各桶等待时间的**最大值**，即全部桶首次同时满足成本的时刻；`pass_at = now + wait_seconds`。
4. **限制桶**：等待时间最长的桶（`limiting_bucket`），并列时取链上首个；立即可通过（等待为 0）时为 `null`。
5. **预计余额**：`projected_balance` 为预计可通过时刻连续补充后的余额（封顶 `capacity`），**不扣减** `cost`。
6. **不可达标记**：`cost > capacity`（`cost_exceeds_capacity`）或余额不足且 `refill_rate == 0`（`zero_refill_rate`）的桶永远不可达；链上任一桶不可达则整体 `reachable = false`，`wait_seconds` / `pass_at` / `limiting_bucket` / `projected_balance` 均为 `null`，各桶 `reason` 标明原因。

### 响应示例（可达）

```json
{
  "reachable": true,
  "wait_seconds": 2.0,
  "pass_at": 1002.0,
  "limiting_bucket": "user_quota",
  "buckets": [
    {"id": "tenant_quota", "balance_now": 96.0, "reachable": true, "reason": null, "wait_seconds": 0.0, "projected_balance": 100.0},
    {"id": "api_qps", "balance_now": 1.0, "reachable": true, "reason": null, "wait_seconds": 0.6, "projected_balance": 5.0},
    {"id": "user_quota", "balance_now": 2.0, "reachable": true, "reason": null, "wait_seconds": 2.0, "projected_balance": 4.0}
  ]
}
```

### 响应示例（不可达，`user_quota` 零速率且余额不足）

```json
{
  "reachable": false,
  "wait_seconds": null,
  "pass_at": null,
  "limiting_bucket": null,
  "buckets": [
    {"id": "tenant_quota", "balance_now": 96.0, "reachable": true, "reason": null, "wait_seconds": 0.0, "projected_balance": null},
    {"id": "api_qps", "balance_now": 1.0, "reachable": true, "reason": null, "wait_seconds": 0.6, "projected_balance": null},
    {"id": "user_quota", "balance_now": 2.0, "reachable": false, "reason": "zero_refill_rate", "wait_seconds": null, "projected_balance": null}
  ]
}
```

## 请求格式 `POST /plan-initial-balances`

请求体与 `/simulate` **完全相同**（`buckets` + `requests`，字段与校验规则一致，`initial` 缺省仍按 `capacity` 计）。接口以**所有请求均获放行**为目标，按与 `/simulate` 相同的时间排序与连续补充语义，为每个桶在 `[0, capacity]` 内反推满足其全部链上消耗的最低初始余额。

```json
{
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10},
    {"id": "api_qps", "capacity": 8, "refill_rate": 5},
    {"id": "user_quota", "capacity": 20, "refill_rate": 1, "initial": 3}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps", "user_quota"]},
    {"id": "req-2", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-4", "timestamp": 2, "cost": 4, "chain": ["tenant_quota", "api_qps", "user_quota"]}
  ]
}
```

### 规划语义

1. **目标全量计入**：规划假设每个请求都会放行，链上每个桶都按 `cost` 全量扣减——即使某请求在真实模拟中会因其他桶暂时不足而被原子拒绝，它对链上各桶的消耗也必须计入，不得跳过。
2. **时间顺序与连续补充**：与 `/simulate` 一致——按 `timestamp` 升序（同刻按输入顺序）处理；`initial` 在最早请求时刻持有，之后按 `(时间差) × refill_rate` 连续补充、封顶 `capacity`。
3. **最低 initial**：对每个桶独立反推（自最后一个消耗事件向前递推，跨过补充区间时减去 `refill_rate × 时间差`、不下穿 0），给出恰好满足其全部链上消耗的最低初始余额，误差不超过 1e-6；从未被任何链引用的桶 `required_initial` 为 0。
4. **不可行**：若满桶起步（`initial = capacity`）仍无法让全部请求放行，返回 `reachable = false`，`first_failure` 指出**按处理顺序**首个无法满足的请求与桶（同一请求多个桶不足时取链上首个），此时 `buckets` 与 `final_balances` 为 `null`。
5. **可行**：返回各桶 `required_initial`、相对请求 `initial` 的 `delta`（= `required_initial` − 请求 `initial`，缺省按 `capacity` 计；为正表示请求初始余额不足），以及**用规划值完整回放**得到的 `final_balances`（回放中所有请求均放行）。

### 响应示例（可行）

```json
{
  "reachable": true,
  "buckets": [
    {"id": "tenant_quota", "required_initial": 8.0, "requested_initial": 100.0, "delta": -92.0},
    {"id": "api_qps", "required_initial": 8.0, "requested_initial": 8.0, "delta": 0.0},
    {"id": "user_quota", "required_initial": 6.0, "requested_initial": 3.0, "delta": 3.0}
  ],
  "final_balances": {"tenant_quota": 12.0, "api_qps": 2.0, "user_quota": 0.0},
  "first_failure": null
}
```

- `tenant_quota`：`t=0` 连扣两笔 4，之后每秒补 10 足够覆盖后续请求 → 最低 8；
- `api_qps`：`t=0` 两笔 4 恰好占满容量 → 最低 8（请求的缺省 initial 正好够用，`delta = 0`）；
- `user_quota`：`t=0` 扣 4，`t=2` 前只能补 2，还需 2 → 最低 6，比请求的 `initial: 3` 多 3。

### 响应示例（不可行）

把上例中 `api_qps` 的 `capacity` 改为 5：`t=0` 的 req-1 + req-2 共需 8，满桶 5 也无法满足。

```json
{
  "reachable": false,
  "buckets": null,
  "final_balances": null,
  "first_failure": {"request_id": "req-2", "index": 1, "timestamp": 0.0, "bucket_id": "api_qps", "cost": 4.0, "available": 1.0}
}
```

- `available`：满桶起步回放中，该桶在判定时刻（补充之后、扣减之前）的余额。

## 请求格式 `POST /usage-report`

请求体 = `/simulate` 请求体（`buckets` + `requests`，字段与校验规则完全一致）+ `window_seconds`。接口按与 `/simulate` 相同的时间排序、连续补充与链式原子判定回放请求序列，再按时间窗口统计每个桶的使用情况。

```json
{
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
    {"id": "api_qps", "capacity": 5, "refill_rate": 5}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-2", "timestamp": 0.5, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-4", "timestamp": 3, "cost": 4, "chain": ["tenant_quota"]}
  ],
  "window_seconds": 2
}
```

### 字段说明

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `buckets` | 是 | 同 `/simulate` |
| `requests` | 是 | 同 `/simulate` |
| `window_seconds` | 是 | 统计窗口宽度（秒），必须为正的有限数值；否则返回 422 |

### 统计语义

1. **窗口划分**：以最早请求时刻 `t_start` 为原点，窗口 `k` 为**半开区间** `[t_start + k·window_seconds, t_start + (k+1)·window_seconds)`，覆盖到最后一个请求所在窗口为止。恰好落在窗口边界上的请求属于**右侧**窗口。
2. **归属唯一**：每个请求只计入其 `timestamp` 所在的那个窗口，绝不跨窗重复计数。
3. **回放一致**：放行/拒绝判定与 `/simulate` 完全一致（时间戳升序、同刻按输入顺序；连续补充封顶 `capacity`；链上任一桶不足则全部不扣）。
4. **放行消耗** `allowed_consumption`：窗口内所有放行请求对该桶的 `cost` 合计（请求被放行时，链上每个桶各计一份 `cost`）。
5. **拒绝次数** `rejected_count`：窗口内引用了该桶且被原子拒绝的请求数（无论限制桶是否为该桶本身）。
6. **峰值占用** `peak_occupancy`：`capacity − 窗口内最低余额`，即该窗口内配额被占用（消耗待补充）的峰值。最低余额在窗口起点与每次放行扣减之后采样——余额在事件之间只会因连续补充而上升，因此这些采样点足以刻画窗口最低值。满桶且无人引用的桶占用恒为 0。
7. **峰值窗口** `peak_window`：该桶 `peak_occupancy` 最大的窗口起止时间；并列时取最早窗口。
8. **全局最忙窗口** `busiest_window`：跨桶放行消耗合计最大的窗口（并列取最早），附该窗口的放行/拒绝请求数。

### 响应示例

```json
{
  "window_seconds": 2.0,
  "window_count": 2,
  "windows": [
    {"index": 0, "start": 0.0, "end": 2.0},
    {"index": 1, "start": 2.0, "end": 4.0}
  ],
  "buckets": [
    {
      "id": "tenant_quota",
      "allowed_consumption": 12.0,
      "rejected_count": 1,
      "peak_occupancy": 4.0,
      "peak_window": {"index": 0, "start": 0.0, "end": 2.0},
      "windows": [
        {"index": 0, "allowed_consumption": 8.0, "rejected_count": 1, "peak_occupancy": 4.0},
        {"index": 1, "allowed_consumption": 4.0, "rejected_count": 0, "peak_occupancy": 4.0}
      ]
    },
    {
      "id": "api_qps",
      "allowed_consumption": 8.0,
      "rejected_count": 1,
      "peak_occupancy": 4.0,
      "peak_window": {"index": 0, "start": 0.0, "end": 2.0},
      "windows": [
        {"index": 0, "allowed_consumption": 8.0, "rejected_count": 1, "peak_occupancy": 4.0},
        {"index": 1, "allowed_consumption": 0.0, "rejected_count": 0, "peak_occupancy": 0.0}
      ]
    }
  ],
  "busiest_window": {"index": 0, "start": 0.0, "end": 2.0, "allowed_consumption": 16.0, "allowed_count": 2, "rejected_count": 1}
}
```

- 窗口 0 `[0, 2)`：req-1 放行（`api_qps` 5→1）、req-2 因 `api_qps` 不足被原子拒绝、req-3 放行；`api_qps` 最低余额 1 → 峰值占用 4；
- 窗口 1 `[2, 4)`：req-4 只走 `tenant_quota`；`api_qps` 无事件，余额补满 → 占用回落到 0；
- 最忙窗口为窗口 0：跨桶放行消耗 8 + 8 = 16。

## 请求格式 `POST /reservation-simulate`

```json
{
  "buckets": [
    {"id": "room_a", "capacity": 2},
    {"id": "room_b", "capacity": 1}
  ],
  "reservations": [
    {"start": 0, "end": 3, "cost": 1, "chain": ["room_a", "room_b"]},
    {"start": 2, "end": 5, "cost": 1, "chain": ["room_a", "room_b"]},
    {"start": 5, "end": 8, "cost": 1, "chain": ["room_a"]},
    {"start": 6, "end": 7, "cost": 1, "chain": ["room_a"]},
    {"start": 7, "end": 9, "cost": 1, "chain": ["room_a", "room_b"]}
  ]
}
```

### 字段说明

**桶 `buckets[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 桶唯一标识，非空字符串 |
| `capacity` | 是 | 桶容量，即任意时刻允许的**并发占用**上限，必须 > 0；此模型没有补充（refill）概念 |

**预留 `reservations[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `start` | 是 | 预留起始时刻（含），秒，任意一致的时间原点 |
| `end` | 是 | 预留结束时刻（不含），必须为有限数值且严格大于 `start` |
| `cost` | 是 | 预留期间对链上**每个**桶的并发占用量，必须 > 0 |
| `chain` | 是 | 桶 ID 列表（至少 1 个），同一链内不可重复 |

### 预留语义

1. **半开区间**：占用区间为 `[start, end)`——在 `start` 时刻生效，在 `end` 时刻释放。两个恰好在端点相接的区间（如 `[0,2)` 与 `[2,4)`）**不算并发**，可各自占满容量。
2. **处理顺序**：所有预留按 `start` 升序处理；同一 `start` 按输入数组顺序处理。`results` 仍按原始数组顺序返回（含 `index`）。
3. **整段原子判定**：接受前对链上每个桶检查，在 `[start, end)` 的**每一处**（含被其他已接受区间部分覆盖的重叠段）并发占用加 `cost` 都不超过 `capacity`；链上第一个放不下的桶记入 `rejected_by`，链上所有桶都不登记该预留（原子拒绝）。即使 `cost <= capacity`，只要区间内存在任何重叠时刻超限也会被拒。
4. **合并占用区间**：`buckets[].intervals` 是该桶所有**已接受**预留叠加后的占用量恒定分段，按起点升序；占用为 0 的空档不输出，端点相接且占用相同的相邻段自动合并。
5. **峰值并发**：`buckets[].peak_occupancy` 为整个时间轴上该桶的最高并发占用量；从未被任何已接受预留引用的桶为 0、`intervals` 为空。

### 响应示例

```json
{
  "allowed_count": 4,
  "rejected_count": 1,
  "results": [
    {"index": 0, "start": 0.0, "end": 3.0, "cost": 1.0, "allowed": true, "rejected_by": null},
    {"index": 1, "start": 2.0, "end": 5.0, "cost": 1.0, "allowed": false, "rejected_by": "room_b"},
    {"index": 2, "start": 5.0, "end": 8.0, "cost": 1.0, "allowed": true, "rejected_by": null},
    {"index": 3, "start": 6.0, "end": 7.0, "cost": 1.0, "allowed": true, "rejected_by": null},
    {"index": 4, "start": 7.0, "end": 9.0, "cost": 1.0, "allowed": true, "rejected_by": null}
  ],
  "buckets": [
    {
      "id": "room_a",
      "capacity": 2.0,
      "peak_occupancy": 2.0,
      "intervals": [
        {"start": 0.0, "end": 3.0, "occupancy": 1.0},
        {"start": 5.0, "end": 6.0, "occupancy": 1.0},
        {"start": 6.0, "end": 8.0, "occupancy": 2.0},
        {"start": 8.0, "end": 9.0, "occupancy": 1.0}
      ]
    },
    {
      "id": "room_b",
      "capacity": 1.0,
      "peak_occupancy": 1.0,
      "intervals": [
        {"start": 0.0, "end": 3.0, "occupancy": 1.0},
        {"start": 7.0, "end": 9.0, "occupancy": 1.0}
      ]
    }
  ]
}
```

- index 0 接受；index 1 与 index 0 在 `[2,3)` 重叠，`room_b` 容量 1 放不下第二笔，被原子拒绝（`room_a` 也不留占用）；
- index 2、3 接受：`room_a` 在 `[6,7)` 峰值并发 2；index 4 在 t=7 与 index 3 端点相接（半开，不并发），链上均有空位，接受。

## 请求格式 `POST /fairness-report`

请求体 = `/usage-report` 请求体（`buckets` + `requests` + `window_seconds`），其中每个请求额外携带 `principal` 与 `weight`。接口按与 `/simulate` 完全相同的时间排序、连续补充与链式原子扣减回放请求序列，再按 principal 统计公平性指标。

```json
{
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
    {"id": "api_qps", "capacity": 5, "refill_rate": 5}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "alice", "weight": 1},
    {"id": "req-2", "timestamp": 0.5, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "bob", "weight": 1},
    {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "alice", "weight": 1},
    {"id": "req-4", "timestamp": 3, "cost": 4, "chain": ["tenant_quota"], "principal": "bob", "weight": 1}
  ],
  "window_seconds": 2
}
```

### 字段说明

`buckets`、`window_seconds` 同 `/usage-report`；`requests[]` 在 `/simulate` 字段（`id` / `timestamp` / `cost` / `chain`）之外新增：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `principal` | 是 | 请求归属的主体（租户/调用方）标识，非空字符串 |
| `weight` | 是 | 该主体的公平份额权重，必须为正的有限数值；同一 `principal` 的全部请求必须声明相同 `weight`，否则返回 422（`inconsistent_weight`） |

### 统计语义

1. **回放一致**：放行/拒绝判定与 `/simulate` 完全一致（时间戳升序、同刻按输入顺序；连续补充封顶 `capacity`；链上任一桶不足则全部不扣）。
2. **放行消耗** `allowed_consumption`：该 principal 放行请求的 `cost` 合计（每个请求计一次，与链上桶数无关），即该主体的**实际获得量**。
3. **拒绝次数** `rejected_count`：该 principal 被原子拒绝的请求数。
4. **理论权重份额** `expected_share`：统计范围内总放行消耗 ×（自身权重 ÷ 范围内各 principal 权重之和）。全局范围的权重和覆盖全部 principal；单窗口只覆盖窗口内有请求的**活跃 principal**。
5. **达成率** `attainment_ratio` = 实际获得量 ÷ 理论权重份额；范围内总放行消耗为 0 时无法定义，为 `null`。
6. **缺口** `shortfall` = `max(0, expected_share − allowed_consumption)`。
7. **加权 Jain 公平指数** `jain_index`：`J = (Σxᵢ)² ÷ (Σwᵢ · Σ(xᵢ²/wᵢ))`，其中 `xᵢ` 为各 principal 实际获得量、`wᵢ` 为权重；获得量与权重严格成比例时取 1，完全不均时趋近 0。权重全相等时退化为经典 Jain 指数。范围内总放行消耗为 0（全员为零，完全相等）时按约定返回 1。
8. **窗口划分**：与 `/usage-report` 相同——以最早请求时刻为原点的半开窗口 `[t_start + k·window_seconds, t_start + (k+1)·window_seconds)`，边界上的请求属于右侧窗口，每个请求只计入一个窗口。
9. **最受限 principal** `most_constrained`：范围内存在理论缺口（`shortfall > 0`）者中**达成率最低**的主体；并列时取缺口较大者，再并列取 principal 名字典序较小者。无人存在缺口（完全对齐或总放行消耗为 0）时为 `null`。
10. **短缺证据**：`first_shortage_bucket` / `first_shortage_request_id` / `first_shortage_timestamp` 为最受限 principal 在范围内**按处理顺序首个被拒请求**的限制桶（`rejected_by`）、请求 ID 与时间戳；该 principal 在范围内无拒绝时三者均为 `null`（短缺完全由他人超份额获得造成）。
11. **全局与逐窗口**：响应同时给出全局报告（`jain_index` / `principals` / `most_constrained`，覆盖全部 principal，按请求中首次出现顺序排列）与逐窗口报告 `window_reports`（每窗只统计窗口内活跃 principal）。

### 响应示例

```json
{
  "window_seconds": 2.0,
  "window_count": 2,
  "windows": [
    {"index": 0, "start": 0.0, "end": 2.0},
    {"index": 1, "start": 2.0, "end": 4.0}
  ],
  "jain_index": 0.9,
  "principals": [
    {"principal": "alice", "weight": 1.0, "allowed_consumption": 8.0, "rejected_count": 0, "expected_share": 6.0, "attainment_ratio": 1.333333333, "shortfall": 0.0},
    {"principal": "bob", "weight": 1.0, "allowed_consumption": 4.0, "rejected_count": 1, "expected_share": 6.0, "attainment_ratio": 0.666666667, "shortfall": 2.0}
  ],
  "most_constrained": {
    "principal": "bob",
    "attainment_ratio": 0.666666667,
    "shortfall": 2.0,
    "first_shortage_bucket": "api_qps",
    "first_shortage_request_id": "req-2",
    "first_shortage_timestamp": 0.5
  },
  "window_reports": [
    {
      "index": 0, "start": 0.0, "end": 2.0,
      "jain_index": 0.5,
      "principals": [
        {"principal": "alice", "weight": 1.0, "allowed_consumption": 8.0, "rejected_count": 0, "expected_share": 4.0, "attainment_ratio": 2.0, "shortfall": 0.0},
        {"principal": "bob", "weight": 1.0, "allowed_consumption": 0.0, "rejected_count": 1, "expected_share": 4.0, "attainment_ratio": 0.0, "shortfall": 4.0}
      ],
      "most_constrained": {"principal": "bob", "attainment_ratio": 0.0, "shortfall": 4.0, "first_shortage_bucket": "api_qps", "first_shortage_request_id": "req-2", "first_shortage_timestamp": 0.5}
    },
    {
      "index": 1, "start": 2.0, "end": 4.0,
      "jain_index": 1.0,
      "principals": [
        {"principal": "bob", "weight": 1.0, "allowed_consumption": 4.0, "rejected_count": 0, "expected_share": 4.0, "attainment_ratio": 1.0, "shortfall": 0.0}
      ],
      "most_constrained": null
    }
  ]
}
```

- 全局：alice 获 8、bob 获 4（req-2 在 t=0.5 被 `api_qps` 原子拒绝），等权重下理论份额各 6 → 加权 Jain = 12² ÷ (2 × (8² + 4²)) = 0.9；bob 达成率最低（≈0.667），其短缺的首个桶为 `api_qps`；
- 窗口 0 `[0, 2)`：bob 实际获得 0、理论份额 4 → 达成率 0，窗口 Jain = 0.5；
- 窗口 1 `[2, 4)`：仅 bob 活跃且足额获得 → Jain = 1，无人受限（`most_constrained` 为 `null`）。

## 校验规则（HTTP 422）

以下非法输入都会被拒绝，错误体统一为 `{"detail": [ {"type", "loc", "msg", ...}, ... ]}`：

- **重复桶**：`buckets[].id` 重复（`duplicate_bucket`）；
- **非法速率/容量**：`capacity <= 0`、`refill_rate < 0`、非有限数值、`initial`/`balance` 越界；
- **未知引用**：`chain` 引用了未在 `buckets` 中定义的 ID（`unknown_bucket_ref`）；
- **非法窗口宽度**：`window_seconds` 缺失、非有限数值或 `<= 0`（仅 `/usage-report` 与 `/fairness-report`）；
- **非法预留区间**：`start` / `end` 非有限数值，或 `end <= start`（仅 `/reservation-simulate`）；
- **非法主体/权重**：`principal` 为空字符串，`weight` 缺失、非有限数值或 `<= 0`（仅 `/fairness-report`）；
- **权重不一致**：同一 `principal` 的请求声明了不同的 `weight`（`inconsistent_weight`，仅 `/fairness-report`）；
- 此外还校验：链为空、链内桶重复、`cost <= 0`、请求 ID 重复（`duplicate_request`，仅 `/simulate` 系接口）、多余字段（`extra="forbid"`）。

以上规则对 `/simulate`、`/forecast`、`/plan-initial-balances`、`/usage-report`、`/reservation-simulate` 与 `/fairness-report` 同时生效（按各自字段；`/plan-initial-balances` 与 `/usage-report` 复用 `/simulate` 的请求模型与全部跨字段校验；`/fairness-report` 在 `/usage-report` 请求模型上扩展 `principal`/`weight` 字段与权重一致性校验；`/reservation-simulate` 使用独立的容量桶模型）。

## 本地调用示例

```bash
curl -X POST http://localhost:8000/simulate \
  -H 'Content-Type: application/json' \
  -d @examples/request.json

curl -X POST http://localhost:8000/forecast \
  -H 'Content-Type: application/json' \
  -d @examples/forecast.json

curl -X POST http://localhost:8000/plan-initial-balances \
  -H 'Content-Type: application/json' \
  -d @examples/plan-initial-balances.json

curl -X POST http://localhost:8000/usage-report \
  -H 'Content-Type: application/json' \
  -d @examples/usage-report.json

curl -X POST http://localhost:8000/reservation-simulate \
  -H 'Content-Type: application/json' \
  -d @examples/reservation-simulate.json

curl -X POST http://localhost:8000/fairness-report \
  -H 'Content-Type: application/json' \
  -d @examples/fairness-report.json
```

## 不使用 Docker 的本地开发（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与异常处理
│   ├── models.py        # Pydantic 模型与输入校验
│   ├── simulation.py    # 连续补充 + 原子扣减模拟引擎
│   ├── forecast.py      # 等待时间预测引擎（不扣减状态）
│   ├── planner.py       # 初始余额规划引擎（反推最低 initial）
│   ├── usage.py         # 配额使用分析引擎（按窗口统计消耗/拒绝/峰值占用）
│   ├── reservation.py   # 区间预留试算引擎（容量受限的链上原子预留）
│   ├── fairness.py      # 公平性报告引擎（加权 Jain 指数、窗口份额对齐、最受限主体定位）
│   └── errors.py        # 跨字段校验错误类型
├── tests/               # pytest 测试（引擎语义 + HTTP）
├── examples/request.json
├── examples/forecast.json
├── examples/plan-initial-balances.json
├── examples/usage-report.json
├── examples/reservation-simulate.json
├── examples/fairness-report.json
├── requirements.txt
├── Dockerfile           # 依赖安装在镜像构建阶段完成
├── docker-compose.yml   # 单命令启动，映射 8000 端口
├── entrypoint.sh        # 容器启动初始化
└── README.md
```
