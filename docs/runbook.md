# 切换当晚数据操作手册（Cutover night runbook）

适用对象：会员空间平台切换当晚的值班人。整个窗口按固定顺序执行，**每一步都有回读判据**，
判据不成立就停在那一步，不进下一步。

角色：`commander`（下令）／`dba`（执行本手册脚本）／`app`（服务方，冻结与解冻）。
时间窗：建议 90 分钟（冻结 15 + 增量 20 + 核对 15 + 解冻与验收 40）。

---

## 0. 开工前检查（T-60 分钟）

| 检查 | 命令 | 通过判据 |
| --- | --- | --- |
| 两套库都在线 | `pg_isready -h <source> -p 55432 -U app` / `-p 55435` | 两条都 `accepting connections` |
| 目标库版本一致 | `psql -c "select version()"` 两侧 | 都是 PostgreSQL 16.x |
| 脚本可跑 | `python -m scripts.cutover_incremental --dry-run` | 退出码 0，只打印各表 `pending=` |
| 上次全量时间点 | `select value from migration_state where key='last_full_migration_at'` | 有值，且早于本次窗口 |

`last_full_migration_at` 是**增量识别的下界**：只迁移 `updated_at` 严格大于它的行。

---

## 1. 冻结写入（T0）

1. `app` 部署网关片段：`gateway/nginx-freeze.conf` → `nginx -t && nginx -s reload`。
2. 命令返回 0 的那一刻就是 **`freeze_at`**，立刻落盘：
   ```bash
   python -m scripts.cutover_incremental --record-freeze-at "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
   ```
3. 回读判据（三条全过才算冻结成功）：
   - `curl -X POST https://<host>/api/v1/companies` → **503**，body 含 `service_temporarily_frozen` 与维护提示；
   - `curl https://<host>/api/v1/companies` → **200**；
   - `curl https://<host>/api/v1/freeze/status` → `{"freeze_enabled": true, ...}`。

应用层同样有一道闸：`FREEZE_ENABLED=true` 时 `app/main.py` 的中间件对四种写方法返 503，
网关与应用的提示文案一致，避免只关一层时出现两种文案。

---

## 2. 增量识别与导入（T+15）

```bash
# 2.1 只报数，不动目标库（连接以 READ ONLY 打开）
python -m scripts.cutover_incremental --dry-run > evidence/cutover_dry_run.log

# 2.2 确认数字与业务方预期同量级（异常大＝有批量任务在写，先查再动）

# 2.3 正式导入：按依赖顺序，逐表计时，日志落 evidence/cutover_incremental.log
python -m scripts.cutover_incremental --batch-id "cutover-$(date -u +%Y%m%dT%H%M)" --batch-size 2000
```

表顺序（前者是后者的外键依赖，顺序不可调换）：
`orgs → sites → roles → users → user_roles → member_companies → contacts → tags → tag_links → activities → registrations → opportunities → kb_articles`

每行按 `id` UPSERT，软删除行（`deleted_at` 非空）同样迁移；`migration_batches` 账本记录
（batch_id, table, rows, checksum, duration_ms），同批次重跑直接命中账本、不再写数据。

**回读判据**：日志每表都有 `applied=` 与 `duration=`，末行 `TOTAL applied=<n> duration=<s>`；
出现 `ERROR` 或退出码非 0 ⇒ 停，先看日志再决定重跑（重跑是安全的）。

---

## 3. 序列与冗余字段（T+35）

增量脚本在数据落库后自动执行，不需要另起命令：

- 序列同步：`users/member_companies/contacts` 的 `legacy_no` 序列 `setval` 到当前最大值；
- 冗余计数重算：`member_companies.contact_count`、`activities.registration_count`
  由目标库自身重新统计（只计未软删子行）。

**回读判据**：日志出现 `sequence sync —` 与 `counter recompute —`；两表计数不一致的行数为 0。

---

## 4. 核对放行（T+50）

```bash
python -m scripts.cutover_verify            # 人读版
python -m scripts.cutover_verify --json     # 存档版
```

逐表比较**行数 + 关键字段校验和**。`member_companies.contact_count` 与
`activities.registration_count` 属于目标库重算的派生字段，按「重算值是否等于活行数」单独判定，
不与源库的漂移值直接比。

**回读判据**：末行 `RELEASE GATE: PASS — 13 tables, 0 differences`，退出码 0。
**只要有一张表 DIFF，就不放行**：先看该表差异行数，定位是漏迁移（`updated_at` 落在
`freeze_at` 之后）还是被冻结后仍写入（说明冻结没生效，回第 1 步）。

---

## 5. 解冻与验收（T+65）

1. `app` 撤下网关片段：`rm /etc/nginx/conf.d/cutover-freeze.conf && nginx -t && nginx -s reload`。
2. 应用侧 `FREEZE_ENABLED=false` 重启（或热加载配置）。
3. 回读判据：`curl -X POST ...` 恢复 **201/200**；`/api/v1/freeze/status` → `freeze_enabled: false`。
4. 抽检 3 家会员企业、5 名联系人：在源库与目标库分别按 `id` 查询，字段逐项一致。
5. 归档：`evidence/drill_run.log`、`evidence/cutover_incremental.log`、`evidence/cutover_verify.json`
   与本次 `freeze_at` 一起进切换记录。

---

## 6. 回滚（任一判据不成立时）

| 情况 | 动作 |
| --- | --- |
| 增量中途失败 | 直接重跑同一 `--batch-id`（幂等，账本会跳过已完成批次） |
| 核对差异非 0 且无法在窗口内修 | 目标库整体丢弃重建（目标库是本窗口内新建的，源库全程只读），按第 2 步重来 |
| 解冻后应用异常 | 重新挂网关片段回到冻结态，读写全部停在维护提示，不影响已在目标库的数据 |

目标库是可抛弃的：`docker compose down -v`（本地演练）或云上重建实例；源库在本流程中
**从不会被写入**（`--dry-run` 更以 READ ONLY 事务打开）。

---

## 7. 本地演练（同一套脚本，两套 PostgreSQL 16）

```bash
docker compose up -d                                  # source 55432 / target 55435
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m scripts.run_drill                 # 全链路 + evidence/drill_run.log
.venv/bin/python -m scripts.run_freeze_drill           # 冻结窗口真 HTTP + evidence/freeze_run.log
.venv/bin/python -m pytest -v                          # 验收用例
```

演练数据全部虚构：1,000 家会员企业、5,000 名联系人，另有组织/站点/用户/角色/标签/
活动/报名/商机/知识库的陪衬行，便于验证依赖顺序与冗余计数。
