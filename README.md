# HTTP 内容协商调试服务（FastAPI）

一个用于调试 HTTP 内容协商（`Accept` 头）的小型 FastAPI 服务。给定原始
`Accept` 头字符串和一组候选表示（`media_type` + `server_priority`），
服务返回最终选定的表示，以及**每个候选命中的范围、q 值、匹配具体度和淘汰原因**，
方便直观地理解协商全过程。

此外，`POST /audit-accept` 提供**静态审计**：不返回单一选定项，而是列出每个候选的
全部命中范围与最终生效范围，并汇总从未生效的 shadowed 范围与同具体度 q 冲突。

`POST /compare-accept` 用于**对比两条 Accept（如规则调整前后）在同一组候选上的差异**：
分别按现有协商规则解析 `baseline_accept` 与 `trial_accept`，返回两侧各自的选定项、
按候选 index 对齐的 q/具体度/淘汰原因，以及顶层 `selection_changed`，便于定位规则
调整导致的结果变化。

## 一条命令启动

```bash
docker compose up --build
```

启动后访问：

- 服务地址与端口：**http://localhost:8000**
- 接口文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

> 依赖安装与代码预编译均在镜像构建阶段完成；容器启动时由 lifespan 完成初始化并输出就绪日志。

## 接口

### `POST /negotiate`

请求体：

```json
{
  "accept": "text/html;q=0.9, application/*;q=0.5, */*;q=0.1",
  "candidates": [
    {"media_type": "application/json", "server_priority": 1},
    {"media_type": "text/html", "server_priority": 5},
    {"media_type": "image/png", "server_priority": 9}
  ]
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `accept` | 原始 `Accept` 头字符串 |
| `candidates[].media_type` | 候选媒体类型，**必须具体**（`type/subtype`，不允许 `*`、参数） |
| `candidates[].server_priority` | 服务端优先级，数值越大越优先 |

#### 支持的 Accept 语法（受限子集）

- 范围仅支持：`type/subtype`、`type/*`、`*/*`，**不支持任何媒体参数**（如 `charset`）；
- 大小写不敏感（内部统一转小写比较）；
- 仅允许可选的 `q` 参数；`q` 缺省为 `1`，取值范围 `0..1`，最多 3 位小数；
- 任意非法语法（空条目、非法 token、未知参数、q 越界等）返回 **400**。

#### 协商规则

1. 每个候选在所有匹配的范围中，取**最具体**的范围：精确 `type/subtype`（具体度 2）
   > `type/*`（1） > `*/*`（0）；具体度相同时取头中**最早出现**的范围；
2. 该范围的 `q` 即候选的 q 值；`q=0` 表示显式拒绝，候选被排除；
3. 可用候选按 **q 降序 → 匹配具体度降序 → `server_priority` 降序 → 候选输入顺序**选定；
4. 没有任何可用候选（无范围命中或全部 `q=0`）时返回 **406**。

#### 200 响应示例（节选）

```json
{
  "selected": {
    "index": 1,
    "media_type": "text/html",
    "server_priority": 5.0,
    "matched_range": "text/html",
    "q": 0.9,
    "specificity": 2,
    "specificity_name": "exact"
  },
  "candidates": [
    {
      "index": 0,
      "media_type": "application/json",
      "server_priority": 1.0,
      "matched_range": "application/*",
      "q": 0.5,
      "specificity": 1,
      "specificity_name": "type_subtype_wildcard",
      "eligible": true,
      "eliminated_reason": "outranked"
    }
  ],
  "parsed_accept": [
    {"index": 0, "range": "text/html", "q": 0.9, "specificity": 2,
     "specificity_name": "exact"}
  ]
}
```

候选条目中的 `eliminated_reason` 取值：

| 值 | 含义 |
| --- | --- |
| `null` | 该候选即最终选中项 |
| `outranked` | 可用但排序靠后被淘汰 |
| `q_zero` | 命中最具体范围的 `q=0`，被显式排除 |
| `no_match` | 头中没有任何范围匹配该候选 |

`specificity_name`：`exact`（2）、`type_subtype_wildcard`（1）、`wildcard`（0）。

#### 错误响应

- `400`：Accept 语法非法、候选媒体类型不具体、候选列表为空或请求体格式错误；
- `406 Not Acceptable`：无可用候选（响应体仍包含每个候选的评估明细，便于调试）。

```bash
# 406 示例：所有候选被 q=0 排除
curl -i -X POST http://localhost:8000/negotiate \
  -H 'Content-Type: application/json' \
  -d '{"accept":"*/*;q=0","candidates":[{"media_type":"text/html","server_priority":0}]}'
```

### `POST /audit-accept`

对同一套协商规则做**静态审计**：请求体与 `POST /negotiate` 完全相同，但响应
**不返回单一选定项**，而是完整呈现每个候选的命中情况与头部范围的全局使用情况，
适合排查"为什么某个范围没有生效"这类问题。

#### 审计规则

1. 每个候选列出**全部命中范围**（按头部出现顺序），并标记**最终生效范围**
   （与协商规则一致：最具体优先，同具体度取头中最早）；
2. `shadowed` 汇总头部中**合法但从未对任何候选生效**的范围（按头部顺序），
   包括被更具体/更早范围遮蔽的，以及不匹配任何候选的；
3. 每个候选的 `conflicts` 报告**同一候选上具体度相同但 q 不同**的冲突范围组，
   附头部索引，便于定位重复定义；
4. 输出严格按候选输入顺序与头部顺序稳定排列；
5. `q=0` 排除、无命中与 **406** 语义与 `/negotiate` 一致（无可用候选时返回 406，
   响应体仍带完整审计明细）；任一输入非法时返回与 `/negotiate` 相同的 **400** 格式。

#### 200 响应示例（节选）

```json
{
  "candidates": [
    {
      "index": 0,
      "media_type": "application/json",
      "server_priority": 1.0,
      "eligible": true,
      "eliminated_reason": null,
      "effective_range": {"index": 0, "range": "application/json", "q": 0.3,
                          "specificity": 2, "specificity_name": "exact"},
      "matched_ranges": [
        {"index": 0, "range": "application/json", "q": 0.3, "specificity": 2,
         "specificity_name": "exact", "effective": true},
        {"index": 1, "range": "application/json", "q": 0.9, "specificity": 2,
         "specificity_name": "exact", "effective": false}
      ],
      "conflicts": [
        {"specificity": 2, "specificity_name": "exact",
         "ranges": [
           {"index": 0, "range": "application/json", "q": 0.3},
           {"index": 1, "range": "application/json", "q": 0.9}
         ]}
      ]
    }
  ],
  "shadowed": [
    {"index": 1, "range": "application/json", "q": 0.9, "specificity": 2,
     "specificity_name": "exact"}
  ],
  "parsed_accept": [
    {"index": 0, "range": "application/json", "q": 0.3, "specificity": 2,
     "specificity_name": "exact"}
  ]
}
```

审计候选条目中的 `eliminated_reason` 取值：`null`（可用）、`q_zero`（生效范围
`q=0` 被显式排除）、`no_match`（无任何范围命中）。审计不做最终选定，因此不会
出现 `outranked`。

```bash
curl -i -X POST http://localhost:8000/audit-accept \
  -H 'Content-Type: application/json' \
  -d '{"accept":"application/json;q=0.3, application/json;q=0.9","candidates":[{"media_type":"application/json","server_priority":1}]}'
```

### `POST /compare-accept`

在**同一组候选表示**上分别解析、协商两条 `Accept`（典型场景：调整协商规则前后各取一条
Accept 做对比），并排呈现差异，便于快速回答"规则调整后哪个候选的判定变了、最终选择
有没有变"。

请求体：

```json
{
  "baseline_accept": "application/json;q=0.9, text/*;q=0.5, */*;q=0.1",
  "trial_accept": "application/json;q=0.1, text/*;q=0.9, */*;q=0.1",
  "candidates": [
    {"media_type": "application/json", "server_priority": 1},
    {"media_type": "text/html", "server_priority": 5},
    {"media_type": "image/png", "server_priority": 9}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `baseline_accept` | 基线（规则调整前）原始 `Accept` 头字符串 |
| `trial_accept` | 试验（规则调整后）原始 `Accept` 头字符串 |
| `candidates` | 两侧**共用**的候选表示列表，语法同 `/negotiate` |

响应规则：

1. `baseline` / `trial` 各自是一个与 `/negotiate` 同构的完整协商结果：`selected`、
   `candidates`（含 `eliminated_reason` 等完整明细）、`parsed_accept`，并额外带
   `status`/`error`/`detail`；
2. `candidate_alignment` 按候选 **index 对齐**两侧评估，每个候选一行，给出两侧的
   `matched_range`、`q`、`specificity`/`specificity_name`、`eligible`、
   `eliminated_reason` 与 `selected`，并用 `changed` 标记该行是否有差异；
3. 顶层 `selection_changed` 表示两侧最终选定项是否不同（任一侧无可接受候选视为
   未选定，两侧均未选定时为 `false`）；
4. 任一 `Accept` 语法非法、候选不具体、候选列表为空或请求体格式错误时，沿用
   **400**（两侧都不返回）；
5. **某一侧无可接受候选不影响另一侧比较**：该侧保留与 `/negotiate` 一致的 **406**
   结果（侧内 `status=406`、`error=not_acceptable`，`selected=null` 且候选明细完整
   保留），另一侧正常给出，整体响应仍为 **200**。

#### 200 响应示例（节选）

```json
{
  "baseline": {
    "status": 200,
    "error": null,
    "detail": null,
    "selected": {"index": 0, "media_type": "application/json",
                 "matched_range": "application/json", "q": 0.9,
                 "specificity": 2, "specificity_name": "exact"},
    "candidates": [],
    "parsed_accept": []
  },
  "trial": {
    "status": 200,
    "error": null,
    "detail": null,
    "selected": {"index": 1, "media_type": "text/html",
                 "matched_range": "text/*", "q": 0.9,
                 "specificity": 1, "specificity_name": "type_subtype_wildcard"},
    "candidates": [],
    "parsed_accept": []
  },
  "candidate_alignment": [
    {
      "index": 0,
      "media_type": "application/json",
      "baseline": {"matched_range": "application/json", "q": 0.9,
                   "specificity": 2, "specificity_name": "exact",
                   "eligible": true, "eliminated_reason": null, "selected": true},
      "trial": {"matched_range": "application/json", "q": 0.1,
                "specificity": 2, "specificity_name": "exact",
                "eligible": true, "eliminated_reason": "outranked", "selected": false},
      "changed": true
    }
  ],
  "selection_changed": true
}
```

#### 一侧 406 的示例（整体仍为 200）

```bash
curl -i -X POST http://localhost:8000/compare-accept \
  -H 'Content-Type: application/json' \
  -d '{"baseline_accept":"*/*;q=0","trial_accept":"*/*","candidates":[{"media_type":"a/b","server_priority":0}]}'
```

响应中 `baseline.status` 为 `406`、`error` 为 `not_acceptable`、`selected` 为
`null`（`candidates` 明细保留，淘汰原因为 `q_zero`），`trial` 正常选定，
`selection_changed` 为 `true`。

## 本地开发（不使用容器）

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
pip install pytest httpx
pytest -q
```

## 项目结构

```
app/
  main.py          # FastAPI 路由（/negotiate、/audit-accept、/compare-accept）、请求/响应模型、错误处理与启动初始化
  negotiation.py   # Accept 解析、内容协商与静态审计核心逻辑（无框架依赖，可独立测试）
tests/             # 协商规则、审计规则与 HTTP 接口测试
Dockerfile         # 依赖安装与初始化纳入镜像构建阶段
docker-compose.yml # 一键启动，映射 8000 端口
```
