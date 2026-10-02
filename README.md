# 高校使命漂移预警

围绕"使命漂移"治理的完整服务端：按周期封存使命与办学材料快照，用**经批准且版本化**的规则
把可疑偏离转成**待核案件**；预警不是违规定性，解释、豁免、整改均设期限并全程留痕；
规则升级只作用于后续周期，任何人重放旧周期都能得到逐字节一致的结论。

## 领域契约（`domain/contract.json`）

- 角色：发展规划处（planning）、院系负责人（department）、校级督导（supervisor）
- 案件状态：监测 → 预警 → 核实 → 整改 → 关闭
- 不变量：使命快照、预警案件、限期整改、周期重放

## 关键设计

1. **按周期封存的快照**。五类材料（`mission` 使命、`commitments` 承诺目标、
   `investment` 学科投入、`enrollment` 招生结构、`service` 服务成果）可在开放周期内
   反复修订；封存后不可改。封存时计算材料哈希、发现哈希，并把上一封存快照哈希串入
   `chain_hash`，形成院系维度的防篡改链。
2. **规则版本化 + 批准后冻结**。规则集为 draft → approved：批准后不可修改，只能新建版本；
   起草（规划处）与批准（督导）职责分离。封存时把当时生效版本**钉死**在快照上。
3. **纯函数规则引擎**。`engine.evaluate(材料, 规则)` 无 IO、无时钟、无随机，
   发现按 `(rule_id, subject)` 排序，输出稳定哈希——重放确定性的根基。
4. **预警≠违规**。规则命中只产生状态为"监测/预警"的待核案件；只有督导走核实流程后
   裁定 `rectify` 才进入整改；也可裁定 `unsubstantiated`（不成立）或 `exempt`（限期豁免）。
5. **期限与逾期留痕**。解释期限、整改期限来自封存时钉死版本的 settings；
   逾期提交不被拒收，但事件中如实记录 `overdue`，列表支持 `overdue_only` 筛选。
6. **豁免不自动续期**。豁免有截止日且受 `exemption_max_days` 上限约束；仍在有效期内、
   同规则同主题的后续周期案件封存时自动按豁免关闭并记录"豁免沿用"，过期自动失效。
7. **审计**。案件内有只增的 `case_events` 时间线；全库有哈希链 `audit_log`，
   可通过 `/api/audit/verify` 与快照 replay 检测任何篡改。

零第三方依赖：Python ≥ 3.11 标准库 + SQLite。

## 目录

- `domain/contract.json`：领域角色、状态、不变量与样例。
- `src/domain_contract/`：契约读取与校验（既有）。
- `src/mission_drift/`：服务端
  - `storage.py`：SQLite schema 与连接（串行写锁）
  - `engine.py`：材料/规则校验与纯函数评估引擎
  - `services.py`：业务编排（周期、规则版本、封存、案件工作流、重放、审计）
  - `api.py`：HTTP 路由（http.server，令牌鉴权）
  - `clock.py`：时钟抽象（生产系统时钟 / 测试固定时钟）
  - `serve.py`：服务启动入口
- `tools/bootstrap.py`：离线建立首批规划处/督导账号
- `tools/check_contract.py`：契约摘要检查
- `tests/`：契约回归、服务层 15 例、HTTP 端到端 2 例
- `docs/API.md`：接口手册（含完整 curl 走查）

## 快速开始

```bash
python3 tools/bootstrap.py --db mission_drift.sqlite3     # 初始账号（令牌见输出）
PYTHONPATH=src python3 -m mission_drift.serve --port 8080 # 起服务
```

随后按 `docs/API.md` 建院系/账号/周期 → 起草并批准规则 → 院系提交五类材料 →
规划处封存 → 走案件工作流 → 重放核验。

## 验证

```bash
python3 -m unittest discover -s tests -v       # 全部测试（当前 18 例）
python3 -m compileall -q src tools tests       # 编译检查
python3 tools/check_contract.py domain/contract.json
```

## 确定性边界

重放一致性只依赖：快照封存的五类材料、快照钉死的规则版本、以及纯函数引擎。
案件编号、案件状态等后续工作流数据不参与重放结论，但 replay 会额外比对
"应生成的案件键集合（规则+主题+级别）"与库内实际案件是否一致。
系统时间只影响期限计算与时间戳，不影响任何命中结论。
