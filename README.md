# 高校使命漂移预警（服务端）

把使命、承诺目标、学科投入、招生结构、服务成果按周期封存为不可变快照，以**经批准且
按版本生效**的规则进行确定性评估，把可疑偏离转成**待核案件**；预警不自动定性为违规，
解释、豁免、整改均有期限与哈希链审计轨迹；规则升级只作用于后续周期，任何人重放旧周期
都得到逐位相同的结果。

## 为什么能给出这些保证

| 需求 | 实现机制 |
| --- | --- |
| 五要素按周期封存 | `snapshots` 五要素齐全才可封存；封存后由 SQLite 触发器禁止 UPDATE/DELETE；每份快照有 SHA-256 封存哈希 |
| 可疑偏离 → 待核案件 | 纯函数规则 DSL（无 `eval`，白名单操作符）命中即立案，状态 `alert` 明确标注"待核线索，不构成违规定性" |
| 解释/豁免/整改有期限 | 立案生成解释/豁免期限，核实确认后生成整改期限；逾期操作被拒；督导可依规延期并留痕 |
| 审计轨迹 | 案件主记录与审计时间线各成哈希链（每条含前驱哈希），`verify-chain` 可随时验证，篡改即断链 |
| 规则升级不溯及既往 | 规则只增不改；批准时绑定"生效周期"，且生效周期必须晚于最近已封存周期；退役/撤销同样只对未来周期 |
| 旧周期重放结果相同 | 评估不读时钟；`runs` 记录冻结引擎版本、规则集哈希、豁免集哈希、逐份快照哈希，算出 `result_hash`；重放必须与正式运行一致 |
| 离线改库也能发现 | 重放时重算每份封存快照的哈希，失配即中止；封存触发器只防在线篡改 |

## 目录

- `domain/contract.json`：领域角色、状态、不变量契约。
- `src/domain_contract/`：契约读取与校验（既有）。
- `src/mission_drift/`：服务端实现。
  - `storage.py`：SQLite schema、封存不可变触发器。
  - `hashing.py` / `clock.py`：规范化哈希、可注入时钟。
  - `rules_dsl.py`：安全规则 DSL（and/or/not/比较/between/in/pct_change + 趋势）。
  - `app.py`：用例编排（快照、规则、豁免、评估、案件状态机、审计链）。
  - `http_server.py`：零依赖 HTTP 接口（请求头鉴权）。
- `tools/seed_demo.py`：三个周期的使命漂移场景脚本。
- `tests/`：48 个回归测试（DSL、服务、HTTP 端到端）。

## 快速开始

```bash
# 端到端演示（自动建库）
python3 tools/seed_demo.py data/demo.db

# 启动 HTTP 服务
PYTHONPATH=src MD_DB_PATH=data/md.db MD_PORT=8080 python3 -m mission_drift.http_server
```

## HTTP 接口

身份头：`X-Actor-Role`（`planner`/`unit`/`supervisor`，亦接受中文角色名）、
`X-Actor-Name`、院系负责人还需 `X-Actor-Unit`。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/admin/units`、`/admin/cycles` | planner | 建院系、周期 |
| POST | `/cycles/{cid}/snapshots/{uid}/{kind}` | planner/unit | 提交/封存前修订五要素之一 |
| POST | `/cycles/{cid}/seal` | planner | 五要素齐全后封存 |
| POST | `/rules` | planner | 起草规则（DSL 校验） |
| POST | `/rules/{id}/approve`、`/retire` | planner | 批准生效 / 退役（绑定未来周期） |
| POST | `/exemptions`、`/exemptions/{id}/revoke` | supervisor | 授予/撤销豁免（周期边界） |
| POST | `/cycles/{cid}/run`、`/replay` | planner/supervisor | 正式评估（每周期一次）/ 重放比对 |
| GET | `/runs`、`/runs/{id}` | 不限 | 运行记录与冻结输入清单 |
| POST | `/cases/{id}/explain` | unit | 限期解释 |
| POST | `/cases/{id}/exemption-request` | unit | 限期申请豁免 |
| POST | `/cases/{id}/exemption-decision` | supervisor | 豁免成立则关闭，不成立继续 |
| POST | `/cases/{id}/verify` | supervisor | 核实：不成立关闭；成立进入整改并定期限 |
| POST | `/cases/{id}/rectification` | unit | 限期提交整改 |
| POST | `/cases/{id}/rectification-review` | supervisor | 验收通过关闭；退回可顺延期限 |
| POST | `/cases/{id}/extend-deadline` | supervisor | 对解释/豁免/整改期限依规延期 |
| GET | `/cases`、`/cases/{id}`、`/cases/{id}/timeline`、`/verify-chain` | 不限 | 案件查询与审计验证 |

`kind ∈ {mission, commitment, discipline_input, enrollment, service_output}`。

### 规则 DSL 示例

```json
{
  "id": "rule-basic-enroll-decline",
  "name": "基础学科招生占比持续走低",
  "input": {"share": {"path": "enrollment.basic_discipline_ratio"}},
  "condition": {"<": [{"ref": "share"}, 0.30]},
  "trend": {"metric": "share", "direction": "decreasing", "periods": 3},
  "severity": "high",
  "message": "占比 {share:.1%} 低于30%且连续三年下降"
}
```

支持跨周期基线：`{"path": "service_output.projects", "baseline": true}` 取上一封存周期；
基线数据不足时该规则不出警，避免首个周期误判。操作符：
`and or not > >= < <= == != in between pct_change`。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```

## 边界与取舍

- 鉴权用请求头模拟，生产部署应接入学校统一身份认证（Cas/OAuth）并落审计操作人。
- 单节点 SQLite + 请求级串行锁，满足部处内网年度批量场景；横向扩展需换 PostgreSQL，
  但规范化哈希与运行清单的确定性设计不受存储影响。
- 规则 DSL 刻意保持小而可枚举；复杂规则应拆成多条，保持人可读、可审批。
