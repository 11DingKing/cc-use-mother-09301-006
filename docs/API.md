# API 手册

- Base：`http://<host>:<port>`
- 鉴权：除 `GET /healthz` 外所有接口需要请求头 `X-Auth-Token: <令牌>`
- 响应统一：成功 `{"ok": true, "data": ...}`；失败 `{"ok": false, "error": "..."}`
- 角色：`planning` 发展规划处 / `department` 院系负责人 / `supervisor` 校级督导
- 案件状态：`监测`、`预警`、`核实`、`整改`、`关闭`（watch 级规则→监测，warn 级→预警）

## 1. 组织与账号（规划处）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/departments` | 系列院系列表 |
| POST | `/api/departments` | 建院系 `{code,name}` |
| GET | `/api/actors` | 账号列表 |
| POST | `/api/actors` | 建账号 `{name,role,department_code?,token?}`；department 必须绑定院系 |
| POST | `/api/actors/{id}/disable` | 停用账号（不能停用自己） |

## 2. 周期（规划处）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/cycles` | 周期列表 |
| POST | `/api/cycles` | 建周期 `{code,name,starts_on?,ends_on?}` |
| POST | `/api/cycles/{code}/close` | 关闭周期；关闭后不能新建/修改快照、不能封存 |
| POST | `/api/cycles/{code}/replay` | 重放整个周期内所有已封存快照 |

## 3. 规则版本（起草=规划处，批准=督导）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/rule-sets` | 全部版本 |
| POST | `/api/rule-sets` | 起草新版本（自动递增 version，状态 draft） |
| POST | `/api/rule-sets/{v}` | 修改草稿（approved 不可改） |
| GET | `/api/rule-sets/{v}` | 查看某版本 |
| POST | `/api/rule-sets/{v}/approve` | 督导批准；批准人不得是起草人 |

请求体：

```json
{
  "rationale": "首版预警规则",
  "settings": {"explanation_days": 10, "rectification_days": 30,
               "exemption_max_days": 365},
  "rules": [ /* 见下 */ ]
}
```

规则种类（`kind`）：

- `core_share`：核心学科资源占比下限。params：`{"source":"investment|enrollment",
  "field":"funding|faculty|slots|intake","min_share":0.6}`
- `noncore_share`：非核心占比上限，params 同上但用 `max_share`
- `commitment`：承诺目标完成率下限，params：`{"min_ratio":0.9}`
- `missing_core`：核心学科在指定来源全部零投入。params：`{"sources":["investment","enrollment"]}`
- 每条规则含 `id`、`name`、`kind`、`params`、可选 `level`（`warn` 默认 / `watch` 观察线）

批准后规则集冻结；封存时把当前最新批准版本钉死到快照。**新建版本永不回溯旧周期。**

## 4. 快照

### 4.1 五类材料（PUT 由本院系负责人提交，规划处也可代提交）

`PUT /api/snapshots/{cycle}/{dept}/items/{kind}`，`kind` 取值：

**mission**

```json
{"core_discipline_codes": ["CS", "MATH"], "text": "使命陈述原文"}
```

**commitments** — 承诺目标，`metric` ∈ funding/faculty/slots/intake/service_scale

```json
{"targets": [{"id":"T1","name":"核心经费承诺","metric":"funding",
              "code":"CS","target":1000}]}
```

**investment** — 学科投入

```json
{"disciplines": [{"code":"CS","name":"计算机","funding":300,
                  "faculty":40,"slots":10}]}
```

**enrollment** — 招生结构

```json
{"programs": [{"code":"CS","name":"计算机","intake":400}]}
```

**service** — 服务成果（`core_related` 可直接标注核心相关）

```json
{"projects": [{"id":"P1","name":"开源平台","code":"CS",
               "core_related": true, "scale": 80}]}
```

开放周期内可反复覆盖提交；周期关闭或快照封存后拒绝修改（409）。
五类不齐不能封存。

### 4.2 查询与封存

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/api/snapshots?cycle={code}` | 全部 | 院系只看到自己的 |
| GET | `/api/snapshots/{cycle}/{dept}` | 规划/本院系 | 快照详情，含 `missing_kinds` 与封存哈希 |
| POST | `.../evaluate` | 规划/本院系 | **封存前试算**，`provisional:true`，不落任何案件 |
| POST | `.../seal` | **规划处** | 封存：钉规则版本、算哈希与发现、生成待核案件 |
| POST | `.../replay` | 任意 | 用钉死版本重放，返回各哈希是否一致 |

封存响应/详情包含 `items_hash`、`findings_hash`、`prev_hash`、`chain_hash`、
`rule_set_version`、`sealed_at`。

## 5. 案件工作流

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/api/cases?cycle=&department=&status=&overdue_only=` | 全部 | 院系只见本院系 |
| GET | `/api/cases/{case_no}` | 权限内 | 详情，含解释/整改逾期标记与豁免状态 |
| GET | `/api/cases/{case_no}/timeline` | 权限内 | 追加式事件时间线 |
| POST | `/api/cases/{case_no}/explanation` | 院系 | `{text}`；限期=`封存日+explanation_days` |
| POST | `/api/cases/{case_no}/verify` | 督导 | 监测/预警 → 核实 |
| POST | `/api/cases/{case_no}/decide` | 督导 | 裁定，见下 |
| POST | `/api/cases/{case_no}/rectification` | 院系 | `{text}`；限期=`裁定日+rectification_days` |
| POST | `/api/cases/{case_no}/acceptance` | 督导 | `{accepted:true|false,note?}` |

裁定 `POST /decide`：

```jsonc
{"verdict": "unsubstantiated", "note": "口径误差"}         // 关闭，close_reason=不成立
{"verdict": "exempt", "note": "过渡期", "exempt_until": "2026-01-10"}
//   关闭，close_reason=豁免；日期不得早于今天、不得超过 exempt_until_max_days
{"verdict": "rectify", "note": "解释不充分"}               // → 整改，带整改期限
```

规则要点：

- 预警案件**不预设违规**；只有 `rectify` 裁定才进入整改。
- 解释/整改逾期仍可补交，事件与审计中记录 `"overdue": true`，列表可筛 `overdue_only=true`。
- 整改报告未交时验收通过会被拒绝（409）；验收不通过案件继续留在整改阶段。
- 豁免期内同院系、同规则、同主题在后续周期再次命中，封存时自动以"豁免沿用"关闭，
  并沿用原截止日；过期后恢复正常立案。

案件编号格式：`{周期}-{院系}-{序号:03d}`，例如 `2025-D01-001`。

## 6. 审计

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/api/audit?limit=` | 规划/督导 | 倒序审计条目（含前后哈希） |
| POST | `/api/audit/verify` | 规划/督导 | 从头重算哈希链，返回 `{entries,intact}` |

案件时间线（`case_events`）按案件内 `seq` 只增；全库审计（`audit_log`）哈希成链。
任一被改条目都会让 verify 或 snapshot replay 的哈希比对失败。

## 7. curl 走查

```bash
P=bootstrap-planning; S=bootstrap-supervisor
curl -s -X POST localhost:8080/api/departments -H "X-Auth-Token: $P" \
  -d '{"code":"D01","name":"信息学院"}'
curl -s -X POST localhost:8080/api/actors -H "X-Auth-Token: $P" \
  -d '{"name":"丁院长","role":"department","department_code":"D01","token":"dept-d01"}'
curl -s -X POST localhost:8080/api/cycles -H "X-Auth-Token: $P" \
  -d '{"code":"2025","name":"2025年度","starts_on":"2025-01-01","ends_on":"2025-12-31"}'
curl -s -X POST localhost:8080/api/rule-sets -H "X-Auth-Token: $P" \
  -d '{"settings":{"explanation_days":10,"rectification_days":30,"exemption_max_days":365},
       "rules":[{"id":"R1","name":"核心经费占比≥60%","kind":"core_share","level":"warn",
                 "params":{"source":"investment","field":"funding","min_share":0.6}}]}'
curl -s -X POST localhost:8080/api/rule-sets/1/approve -H "X-Auth-Token: $S" -d '{}'

curl -s -X PUT localhost:8080/api/snapshots/2025/D01/items/mission \
  -H "X-Auth-Token: dept-d01" -d '{"core_discipline_codes":["CS"],"text":"信息学科为核心"}'
# ……依次提交 commitments/investment/enrollment/service（格式见 4.1）
curl -s -X POST localhost:8080/api/snapshots/2025/D01/seal -H "X-Auth-Token: $P"
curl -s -X POST localhost:8080/api/snapshots/2025/D01/replay -H "X-Auth-Token: $P"
curl -s "localhost:8080/api/cases?cycle=2025&status=预警" -H "X-Auth-Token: $P"
```

## 8. 状态码约定

- 200 正常；201 创建成功（院系/账号/周期/规则草稿）
- 400 请求体或参数不合法（含材料、规则结构错误）
- 401 语义并入 403：缺失/无效令牌、无权操作
- 404 对象不存在
- 409 状态冲突（封存后修改、材料不齐封存、审批非草稿等）
