# 诊断续跑治理通道设计 v0.3

> **ACCEPTED / FROZEN DESIGN / NOT IMPLEMENTED**
> 冻结版本：v0.3（2026-07-21）；Codex 终审结论：ACCEPT。
> 冻结不代表生产能力已实现：当前 Stage 17 CFS/grounding 三批实施与
> 严格 F0 完成前，禁止进入 B1–B4 任何实施。
> 本文件冻结后的任何修改必须升版（v0.4+），并重新经过治理评审
> （Kimi 起草 → Codex 终审 → 对齐后冻结）。

- 版本：v0.3（2026-07-21）
- 基线：HEAD `7e56ccfc34107bc7e1f1b1c213518cc55a6b637a`
- 上游共识：2026-07-21 三方对齐冻结清单。评审历史：
  - v0.1 经 Codex 终审（MODIFY，P1×5 + P2×4）→ v0.2 逐项关闭；
  - v0.2 经 Codex 终审（MODIFY，P1×2 + P2×3）→ 本版逐项关闭：
    - P1-1 → §5.1 新增"版本化哈希策略（v1/v2）"正式设计；v0.2 开放
      问题 5 关闭，不再留待实施期裁决；
    - P1-2 → §4.2/§4.4/§4.6/§7/§9：`USE_BOUND_SNAPSHOT` 降为 v1
      保留值（无消费者）；Stage 19 失败后 admission 改
      `RUN_MECHANICAL_PROBE`（quality probe），v1 不执行
      canonical Stage 20；
    - P2 → A7 措辞修正（v2 hash 包含 purpose 绑定）、B1 回滚边界
      修正、§9.1/§9.2 语义重复清理。
- v0.2 关闭项存档：P1-1 → §5（purpose 真源移入 config 哈希链）；
  P1-2 → §4.3（bound snapshot 定义）；P1-3 → §4/§7/§9（两维度模型）；
  P1-4 → §12（批次重排）；P1-5 → §8.1（双报告冻结）；
  P2 → §2.2/§10.1/§8.2。
- 前置约束：当前 Stage 17 CFS/grounding 三批实施与严格 F0 完成前，
  本文档不进入任何生产实施。

---

## 1. 目标、非目标与威胁模型

### 1.1 目标

为研发期提供一条**隔离的、不可发布的、可累计错误的**诊断续跑通道：

- 非颠覆性失败（已证明的 transport/内容契约/质量判断）允许继续执行后续
  阶段，单次运行收集多个独立缺陷；
- 颠覆性失败（证据可信度、身份、授权、越界）仍然立即停止；
- 诊断运行的任何产物永远不能进入 release 语义；
- 正式验收形态不变：全新、严格、Stage 1→25 的 release 运行。

### 1.2 非目标

- 不放松任何现有 release 门禁、canonical replay、release_check 或
  independent reconstruction；
- 不修改 canonical artifact schema v1（零新增字段、零版本迁移）；
- **不修改 `pipeline_summary.json` 的成功语义**；
- 不引入 `continue_on_error: true` 式无分类开关；
- 不改变 Stage 内部门禁、parser、authority 生命周期；
- 不承诺"一次诊断运行发现全部问题"（客观预期：串行 5 次 → 2–3 次）。

### 1.3 威胁模型

| # | 威胁 | 现有防线 | 本设计新增防线 |
|---|------|---------|---------------|
| T1 | diagnostic run 被提升/resume 为 release run | release_check 拒绝 pipeline_validation | config 冻结 purpose + identity 对账（§5）+ resume 拒绝（§8.2）+ 对抗红测（§11） |
| T2 | 删除/替换 ledger 洗白诊断历史 | 无（ledger 是新组件） | ledger 非 authority；purpose 真源不依赖 ledger（§6.5） |
| T3 | 部分搬运 diagnostic artifact 进 release run 洗白 | 逐级 authority 绑定、hash replay | 不变；本设计不开新搬运口 |
| T4 | 失败伪装成成功（DONE/checkpoint/summary 伪装） | checkpoint 仅 DONE 触发 | 诊断结果模型独立于 StageStatus/pipeline_summary（§8） |
| T5 | 未知异常被误归为可续 | 无（当前全停） | unknown→STOP 硬规则（§2.3） |
| T6 | placeholder 伪造业务事实 | 现有编造禁令 | 无合法输入即 NOT_EXECUTED（§4.4） |
| T7 | Stage 24/25 在诊断 run 产生 release 形态产物 | release gate | 独立 probe runner，无 canonical publication（§9） |
| T8 | ledger/identity 写入越界（symlink、parent replacement） | BoundOutputNamespace fd 系列 | 复用同族 fd-relative 原语（§5.4、§6.4） |
| T9 | 同步篡改 run 内 identity+manifest 制造内部一致 | 无 | purpose 真源在 config 哈希链，run-local 文件均非 oracle（§5.1） |

---

## 2. 集中式封闭错误码注册表

### 2.1 注册表形态

新增单一模块（建议 `researchclaw/pipeline/diagnostic_taxonomy.py`，
生产期新建）持有封闭枚举 `DiagnosticErrorCode`。任何 stage、runner 或
工具不得在该模块之外发明错误码。

每个错误码携带四个显式属性：

```text
code                       封闭枚举成员
mechanism_source           transport | content_contract | quality | integrity
continuable                bool（是否允许 FAILED_CONTINUABLE）
requires_repair_exhausted  bool（content 类须先耗尽 bounded repair）
```

### 2.2 初始封闭集合（冻结前评审项）

| code | source | continuable | repair_exhausted | 说明 |
|------|--------|-------------|------------------|------|
| `provider_timeout` | transport | 是 | false | 调用超时，含 HTTP 408 |
| `provider_disconnect` | transport | 是 | false | 连接中断/对端关闭 |
| `provider_http_5xx` | transport | 是 | false | 服务端错误（5xx 全体） |
| `provider_http_4xx_other` | transport | **否** | false | 408/401/403/429 之外的 4xx；请求构造缺陷 |
| `provider_auth_error` | transport | **否** | false | 401/403；凭证/权限问题 |
| `provider_rate_limited` | transport | **否** | false | 429；限流，继续只会放大 |
| `empty_response` | content_contract | 是 | **true** | 经分类点确定性判定 |
| `truncated_response` | content_contract | 是 | **true** | finish_reason 证据 |
| `malformed_response` | content_contract | 是 | **true** | JSON parse 失败 |
| `schema_invalid_after_bounded_repair` | content_contract | 是 | **true**（定义即蕴含） | bounded repair 已耗尽 |
| `quality_reject_coherent` | quality | 是 | false | verdict/score 自洽的低分或 reject |
| `integrity_violation` | integrity | **否** | false | hash/identity/binding/replay 任何不一致 |
| `namespace_violation` | integrity | **否** | false | symlink/collision/parent replacement |
| `unknown` | — | **否** | false | 无法映射进上表的一切 |

HTTP 状态映射必须**互斥且按精确状态先行**：

```text
408      → provider_timeout
401/403  → provider_auth_error
429      → provider_rate_limited
5xx      → provider_http_5xx
其余 4xx → provider_http_4xx_other
无状态   → 不得臆造状态码，按连接/超时类证据分类，否则 unknown
```

禁止先被笼统 `http_4xx` 吞掉后再细分。

### 2.3 unknown→STOP 硬规则

> 只有命中封闭枚举且 `continuable == True`（且
> `requires_repair_exhausted` 条件已满足）的失败，才允许进入下游
> admission 裁决。任何无法映射的异常、未注册 code、注册表查询本身
> 失败、或 repair 未耗尽的 content 失败，一律按 FAILED_STOP 处理。

禁止的分类依据：异常消息字符串匹配；stage 编号或名称；宽泛
`except Exception` 后的主观推测。

允许的分类依据（均为确定性代码证据）：transport 层结构化结果（精确
HTTP status、连接异常类型、timeout 标记）；解析点分类
（empty/truncated/malformed，与 Stage 6/19/20 已实现模式同源）；
verdict/score 一致性矩阵（Stage 20 既有矩阵复用）。

---

## 3. 机制分类流程

```text
stage 返回 FAILED
  → runner 收集结构化 error context
  → DiagnosticErrorRegistry.classify(context)
      ├─ 无法映射 / repair 未耗尽 → unknown 类 → FAILED_STOP
      ├─ continuable=False → FAILED_STOP
      └─ continuable=True → FAILED_CONTINUABLE，进入 §4 下游 admission
           ├─ 失败发生于任何 authority 写入/binding 校验之后？
           │     是 → FAILED_STOP（机制证据优先于错误码）
           │     例：CFSIntegrityError、hash mismatch、replay 拒绝
           └─ 否 → 按 §4.4 表得出 admission
```

原则：**stage 位置只决定审慎级别，运行时机制证据决定最终分类。**
authority 邻近 stage 的未知失败一律 STOP；其已被证明的
transport/content 失败允许进入 admission 裁决。

---

## 4. 两维度裁决模型

### 4.1 维度一：当前 stage 失败结果

| 结果 | 语义 |
|------|------|
| `FAILED_CONTINUABLE` | 命中可续错误码且未触碰 authority 写入；run 可继续 |
| `FAILED_STOP` | 立即终止 run（现行 release 行为对一切失败即此态） |

stage 自身只返回真实 `FAILED`；上述结果是 runner 侧的分类输出，
不回写 StageStatus。

### 4.2 维度二：下游 admission

对 `FAILED_CONTINUABLE` 的 stage，主循环推进到下一 stage 时，
按下表决定其准入方式：

| admission | 语义 |
|-----------|------|
| `USE_BOUND_SNAPSHOT` | **v1 保留值，无消费者**：以下一 stage 消费 §4.3 预捕获快照的方式执行；当前不存在 executor input-injection 接口（§4.6），v1 任何 canonical stage 不得使用本 admission |
| `RUN_MECHANICAL_PROBE` | 不执行 canonical stage；由 §9 独立 probe runner 做机械验证 |
| `NOT_EXECUTED_MISSING_INPUT` | 无合法输入，跳过并记录 |
| `STOP` | 终止 run |

admission 在**每个 stage 入口**重新评估：上一 stage 的失败会沿
输入依赖向后传播，后续 stage 各自按输入是否完整独立裁决。

### 4.3 bound snapshot 定义（冻结）

> last trusted input 必须是在**本次 stage 调用进入之前**捕获的
> immutable bound snapshot，且同时绑定：
> 同一 run identity、config semantic hash、source path/sha256、
> 以及捕获时的 reader/writer epoch。

- 快照的实体内容即**失败 stage 的输入 bundle**（stage 入口在
  held epoch 下捕获），不是失败调用的任何输出；
- 失败调用产生的任何 live output、部分写入、临时文件，
  一律经既有 cleanup 失效，**不得**作为 fallback；
- Stage 9/12/13/14/20 的输出是 authority 链根：这些 stage 失败后，
  **默认禁止复用同阶段任何旧产物**，下游 admission 只能
  `NOT_EXECUTED_MISSING_INPUT` 或 `RUN_MECHANICAL_PROBE`；
- Stage 20 coherent reject 后，**绝不复用旧 quality manifest**；
- 无法提供满足上述绑定的预捕获快照 → `NOT_EXECUTED_MISSING_INPUT`。

### 4.4 逐 stage admission 默认表（本 stage 失败 → 下一 stage 准入）

| Stage | authority 类别 | 默认 admission | 备注 |
|-------|---------------|----------------|------|
| 1 TOPIC_INIT | run 身份根 | STOP | 根阶段失败即终止 |
| 2 PROBLEM_DECOMPOSE | 无 | NOT_EXECUTED_MISSING_INPUT | 级联：3 缺输入 |
| 3 SEARCH_STRATEGY | 无 | NOT_EXECUTED_MISSING_INPUT | |
| 4 LITERATURE_COLLECT | evidence | NOT_EXECUTED_MISSING_INPUT | SKIP_FORBIDDEN；禁复用旧候选 |
| 5 LITERATURE_SCREEN（GATE） | evidence | NOT_EXECUTED_MISSING_INPUT | gate 拒绝记 `quality_reject_coherent` |
| 6 KNOWLEDGE_EXTRACT | evidence | NOT_EXECUTED_MISSING_INPUT | 零卡片时 17 grounding 缺失 |
| 7 SYNTHESIS | 无 | NOT_EXECUTED_MISSING_INPUT | |
| 8 HYPOTHESIS_GEN | 无 | NOT_EXECUTED_MISSING_INPUT | |
| 9 EXPERIMENT_DESIGN（GATE） | contract 根 | NOT_EXECUTED_MISSING_INPUT | contract 完整性失败→FAILED_STOP；禁复用旧 contract |
| 10 CODE_GENERATION | 生成代码 | NOT_EXECUTED_MISSING_INPUT | |
| 11 RESOURCE_PLANNING | 无 | NOT_EXECUTED_MISSING_INPUT | |
| 12 EXPERIMENT_RUN | metric authority | NOT_EXECUTED_MISSING_INPUT | 禁复用旧结果集 |
| 13 ITERATIVE_REFINE | authority | NOT_EXECUTED_MISSING_INPUT | 同上 |
| 14 RESULT_ANALYSIS | canonical evidence | NOT_EXECUTED_MISSING_INPUT | evidence 对账失败→FAILED_STOP |
| 15 RESEARCH_DECISION | 决策 | NOT_EXECUTED_MISSING_INPUT | 禁止以失败触发 pivot/refine |
| 16 PAPER_OUTLINE | 草稿 | NOT_EXECUTED_MISSING_INPUT | |
| 17 PAPER_DRAFT | fact closure | NOT_EXECUTED_MISSING_INPUT | closure/identity 失败→FAILED_STOP |
| 18 PEER_REVIEW | review authority | NOT_EXECUTED_MISSING_INPUT | |
| 19 PAPER_REVISION | revision binding | RUN_MECHANICAL_PROBE（quality probe 读取预捕获 Stage 17/18 bundle，§9.2-4） | v1 不执行 canonical Stage 20；probe 结果只进 ledger，不生成 quality report/manifest |
| 20 QUALITY_GATE（GATE） | quality authority | `quality_reject_coherent`→RUN_MECHANICAL_PROBE（21–25 探针化）；verdict/score 矛盾→FAILED_STOP | 绝不复用旧 quality manifest |
| 21 KNOWLEDGE_ARCHIVE | archive | RUN_MECHANICAL_PROBE | 现为 NONCRITICAL 唯一成员 |
| 22 EXPORT_PUBLISH | export | RUN_MECHANICAL_PROBE | 打包 dry-run，产物 NON_PUBLISHABLE |
| 23 CITATION_VERIFY | citation closure | RUN_MECHANICAL_PROBE | registry 封存失败→FAILED_STOP |
| 24 TRUTH_AUDIT | release audit | RUN_MECHANICAL_PROBE（§9 独立 probe runner） | 无 canonical publication |
| 25 DEAI_AUDIT | release audit | RUN_MECHANICAL_PROBE（同上） | 同上 |

### 4.5 占位禁令

- SKIP_FORBIDDEN_STAGES（stages.py L133，16 个 stage）的任何成员不得用
  通用 stub 冒充有效产物；
- 禁止生成伪 card、伪 review、伪 quality report、伪 manifest 来满足
  下游文件契约；定义不出 §4.3 合法快照的 stage 只能
  `NOT_EXECUTED_MISSING_INPUT`；
- 占位内容本身若属编造，等同于违反既有 fabrication 禁令，按
  `integrity_violation` → FAILED_STOP 处理。

### 4.6 为什么 v1 没有 `USE_BOUND_SNAPSHOT` 消费者（冻结）

runner 主循环只调用 `execute_stage(stage, run_dir=..., config=...)`；
下游 canonical stage（如 Stage 20）自行从 run 目录重建上游 binding
（_review_publish.py L1294 区域），不存在接收 runner 预捕获快照的
接口。在未设计正式的 executor input-injection API 之前，任何"以下一
canonical stage 消费 bound snapshot"的写法都不可实现——强行续跑
只会让下一 stage 因缺少合法 binding 再次失败。故 v1 冻结：

- `USE_BOUND_SNAPSHOT` 保留为枚举值但无任何消费者；
- 上游失败后，下游 canonical stage 一律 `RUN_MECHANICAL_PROBE`
  或 `NOT_EXECUTED_MISSING_INPUT`；
- Stage 19 失败的替代形态是 §9.2-4 的 quality probe：读取预捕获的
  Stage 17/18 bundle，验证 provider transport、response schema 与
  内容审查能力；probe 结果只进 ledger，**不生成** Stage 20
  quality report / quality_gate_manifest；
- 未来若要启用 snapshot 注入，须单独设计并终审 input-injection
  API，且仍须满足 stage 内零 diagnostic 分支。

---

## 5. 不可变 diagnostic run identity

### 5.1 purpose 真源：semantic config 冻结字段

`run_purpose` **不是** run 内任何文件自证的字段。生产期在 config
新增冻结字段（`config.py`，与既有 `claim_scope` 同级）：

```text
run_purpose  = "release" | "diagnostic"    # 默认 release
```

该字段纳入 `semantic_config_sha256`
（canonical_experiment_evidence.py L161 既有语义哈希）。由此：

- purpose 进入既有 config → contract → evidence 的哈希绑定链；
- diagnostic run 强制 `claim_scope=pipeline_validation`
  （identity 验证时硬性检查，缺失或不为该值即拒绝）；
- **T9 防线**：攻击者同步篡改 run 内 identity/manifest/ledger 制造
  内部一致也无意义——purpose 的期望值由可信 canonical config
  独立派生，run-local 文件均非 oracle。

#### 版本化哈希策略（v1/v2，冻结；关闭 v0.2 开放问题 5）

现状：`semantic_config_sha256(config) = sha256_text(canonical_json_text(config.to_dict()))`
（canonical_experiment_evidence.py L162，docstring 已标注
"semantic policy v1"），`to_dict()` 为完整 asdict 投影。若直接新增
默认字段 `run_purpose="release"`，全部存量 config 的 v1 hash 都会
改变，"纳入 hash"与"存量 hash 不变"不可兼得。为此冻结版本化策略：

- **policy v1（存量兼容）**：仅支持历史 release 运行。计算时使用
  旧字段投影，排除新增的 `run_purpose`；旧 hash 可按 v1 精确重建。
- **policy v2（新增）**：投影包含 `run_purpose`。**所有新建 run
  （release 与 diagnostic）必须使用 semantic policy v2**；
  **policy v1 仅用于历史 release 重建，禁止创建新的 v1 run**。
- **v1 运行绝不允许 diagnostic purpose**：可信 config 若要求
  diagnostic，任何 v1 artifact 立即拒绝（防降级攻击）。
- **reconstruction 分派**：按 evidence 已携带的
  `config_semantic_policy_version`（stage12/13/14 既有字段）调用
  对应投影；解析器须同时支持受信任的 v1/v2，未知版本一律拒绝。
- **canonical artifact schema v1 零新增字段**：版本信息复用既有
  `config_semantic_policy_version` 通道，不新增任何 artifact 字段。

### 5.2 `run_identity.json`：被验证的记录，不是真源

run 创建时（**先于任何 provider 预检与 Stage 1**，见 §10.1）写入
run 根目录 `run_identity.json`：

```text
schema_version                 = 1
run_id                         = <创建时分配>
run_purpose                    = 从冻结 config 读出的值（记录，非裁决）
claim_scope                    = 从冻结 config 读出的值
created_utc                    = 创建时间
config_semantic_policy_version = 1 | 2（diagnostic 恒为 2）
config_semantic_sha256         = 按 §5.1 对应 policy version 投影计算
```

`run_identity_sha256 = sha256_text(canonical_authority_json_text(上述内容))`，
写入 run_manifest.json（release_artifacts L473 `extra` 通道）与
ledger 首行（§6.2）。该文件的角色：让运行期与事后审计**有记录可对账**；
purpose 的真源永远是 §5.1 的 config 哈希链。

### 5.3 独立重建与验证

release_check / independent reconstruction 验收任何 run 前：

1. 从可信 canonical config snapshot 读出
   `config_semantic_policy_version`，按对应投影重算
   `semantic_config_sha256`，并读出期望 `run_purpose` /
   `claim_scope`（diagnostic 期望恒为 v2；v1 artifact 配
   diagnostic 期望 → 立即拒绝）；
2. 通过 held run fd 重读 `run_identity.json`，重算
   `run_identity_sha256`，与 run_manifest 绑定值对账；
3. 期望 purpose != "release"、identity 缺失、或任一哈希对账失败
   → 拒绝验收；
4. run-local manifest 不得作为 purpose 的唯一 oracle（上述第 1 步
   必须独立成立）。

diagnostic run **永远不能转为 release run**：不存在任何修改
`run_purpose` 的合法操作；变更即新 run（新 config hash、新 identity）。

### 5.4 写入安全

identity 文件创建使用 O_NOFOLLOW + O_EXCL 新文件语义（复用
BoundOutputNamespace 同族原语）；创建后运行期内不再重写；
collision/symlink/parent replacement → 创建失败即 run 不启动。

---

## 6. canonical namespace 外 append-only diagnostic ledger

### 6.1 位置

`<run_root>/diagnostic_ledger.jsonl`（run 根目录，**不在**任何 stage-XX
canonical namespace 内，不参与 exact-set replay，不进入任何 manifest 的
authority 绑定）。

### 6.2 记录格式（JSONL，每行一事件，字段白名单）

```text
schema_version         = 1
event_seq              = 单调递增整数（写入方维护）
event_type             = run_identity | classified_failure |
                         admission_decision | stage_not_executed |
                         probe_result
stage                  = int | null
error_code             = §2 注册表 code | null
mechanism_source       = transport | content_contract | quality | integrity | null
stage_outcome          = FAILED_CONTINUABLE | FAILED_STOP | null
admission              = USE_BOUND_SNAPSHOT | RUN_MECHANICAL_PROBE |
                         NOT_EXECUTED_MISSING_INPUT | STOP | null
run_identity_sha256    = §5.2 绑定值
recorded_utc           = 时间戳
```

严禁字段：raw response、prompt 全文、reasoning_content、API key、
正文片段、任何自由文本 message（错误细节以 code + source 表达）。

### 6.3 生命周期

- 创建：run 创建时紧随 identity 写入首行 `run_identity` 事件；
- 追加：每次分类/裁决/跳过/探针结果一行；
- 封存：run 结束时 ledger_sha256 写入 run_manifest.extra（参考通道，
  非 authority）；
- 运行期内不删除、不截断、不改写既有行（append-only）。

### 6.4 攻击处理

| 攻击 | 处理 |
|------|------|
| 创建时 symlink/FIFO/目录 collision | O_NOFOLLOW + O_EXCL，创建失败即 STOP |
| 运行期 parent replacement | fd-relative 写入；检测即 FAILED_STOP |
| 读取方发现 event_seq 重复/回退 | 视为篡改，ledger 整体不可信（记录但不改变 identity 结论） |
| 非有限数值 / 非白名单字段 | canonical writer 拒绝写入该行并 STOP 当前写入操作 |
| 删除/替换 ledger | 见 §6.5：不影响 identity；run_manifest 中 ledger_sha256 对账失败仅证明 ledger 被篡改 |

### 6.5 关键不变量

> **ledger 不是 authority。** 删除 ledger 不能消除 diagnostic identity；
> release 拒绝由 §5.1 config 哈希链 + claim_scope 双重防线保证，
> 不依赖 ledger 存在与否。ledger 的唯一用途是诊断可追溯性。

---

## 7. runner 层续跑控制流

改造点唯一：`researchclaw/pipeline/runner.py` 主循环 L921–925 的
FAILED 分支。控制流：

```text
result.status == FAILED
  ├─ release 模式：现行行为不变（NONCRITICAL 规则 → break）
  └─ diagnostic 模式：
       classify（§2/§3）→ stage_outcome
         ├─ FAILED_STOP → 记 ledger → break（与现行一致）
         └─ FAILED_CONTINUABLE → 记 ledger → 主循环推进
              每个后续 stage 入口评估 admission（§4.2/§4.4）：
                ├─ USE_BOUND_SNAPSHOT → v1 无消费者（§4.6），不得出现
                ├─ RUN_MECHANICAL_PROBE → 交 §9 probe runner
                ├─ NOT_EXECUTED_MISSING_INPUT → 跳过并记 ledger
                └─ STOP → 记 ledger → break
```

硬性约束：

- stage 内部**零** diagnostic 分支；stage 返回真实 FAILED；
- 分类与 admission 裁决只发生在 runner/orchestrator；
- gate stage 在 diagnostic 模式下不跳过 HITL 语义（HITL abort 仍然
  有效），gate 拒绝按 `quality_reject_coherent` 分类后可续跑；
- `skip_noncritical` 既有 flag 与诊断模式互不依赖；
- 续跑不触发 `_write_checkpoint` / `_write_heartbeat`（二者仅 DONE
  触发，见 runner L877–896），失败 stage 绝不留下成功形态痕迹。

---

## 8. 诊断结果模型与既有状态机的隔离

### 8.1 双报告冻结（P1-5）

- `pipeline_summary.json`：**成功语义零改动**，继续真实记录 FAILED
  与 run 不完整；不得出现 `failed_continued` 或任何续跑痕迹；
- 新增 `diagnostic_summary.json`：**非 authority** 独立文件，承载
  `failed_continued` 标记、逐 stage 分类/admission 汇总、
  未执行 stage 清单与探针结果索引；
- release_check / reconstruction **永远不得**把 diagnostic_summary
  当作完成证据；它不参与任何 authority 绑定。

### 8.2 隔离矩阵

| 机制 | 规则 |
|------|------|
| StageStatus | 不新增任何枚举值；无 `DEGRADED_CONTINUE` 之类准成功态 |
| checkpoint | 仅 DONE 触发，续跑失败 stage 不写 checkpoint；禁止复用 `DONE + decision="degraded"` 伪装 |
| heartbeat | 仅 DONE 触发，同上 |
| resume | **v1 全面禁止 diagnostic resume**：resume 入口先按 §5.3 重建 identity，`run_purpose=diagnostic` 一律拒绝 resume；release resume 语义不变 |
| run_manifest | diagnostic run 的 manifest `expected_final_stage` 语义不变，但 reviewer/release gate 按 §5.3 独立派生 purpose 后拒绝发布 |

---

## 9. Stage 24/25 机械探针与独立 probe runner

### 9.1 独立 probe runner

`RUN_MECHANICAL_PROBE` admission 由**独立 probe runner**执行
（生产期新模块，如 `researchclaw/pipeline/diagnostic_probes.py`，
由 runner 显式调用），**不经过** `execute_stage(Stage.TRUTH_AUDIT)`
或 `execute_stage(Stage.DEAI_AUDIT)`：

- 不产生 `claims.json`/`citations.json`/`truth_audit.json`/
  `deai_audit.json` 等 canonical artifact；
- 不进入 run_manifest 的 artifact_digests；
- 探针结果**不得**在 pipeline 结果、summary 或任何记录中呈现为
  "Stage 24/25 已执行"（ledger 记账格式见 §9.2-5）。

### 9.2 允许的机械探针（全部只读/临时目录）

1. upstream artifact JSON well-formedness 与 schema 校验
   （paper_draft、citations、quality report 等，对既有 schema）；
2. hash-chain 重算 dry-run（只读 replay，不写任何 authority）；
3. export 打包 dry-run 至临时目录（验证打包机械兼容性，产物标记
   NON_PUBLISHABLE 且运行结束即弃）；
4. quality probe（Stage 19 失败后替代 canonical Stage 20，§4.4）：
   读取预捕获的 Stage 17/18 bundle，验证 provider transport、
   response schema 与内容审查能力；结果只进 ledger，**不生成**
   Stage 20 quality report / quality_gate_manifest；
5. 探针结果以 `probe_result` 事件写入 ledger。

禁止：任何形式的 canonical publication、manifest 提交点、外部发布动作。

---

## 10. provider/auth 基础探针与三类 schema 探针

### 10.1 顺序约束

**run identity 必须先于任何 provider 探针创建**（§5.2）：
预检失败时 run 已具备可信诊断身份与 ledger 锚点，失败可立即入账。

### 10.2 两层预检（每次 run 一次，identity 之后、Stage 1 之前）

| 层 | 内容 | 调用上界 |
|----|------|---------|
| L1 基础探针 | auth + transport + 最小 JSON echo | ≤1 次 outbound，max_tokens ≤ 64 |
| L2 schema 族探针 | ① 普通 JSON 对象 ② sectional planner JSON ③ quality verdict JSON | 每族 ≤1 次 outbound，max_tokens ≤ 256 |

**每 run 预检总 outbound ≤ 4。** 不逐 stage 预检；stage 级 prompt/schema
验证由本地 deterministic fixture 承担。

### 10.3 探针结果处置

- release 模式：L1 失败 → Stage 1 前 fail-fast（分钟级止损）；L2 失败 →
  按 §2 分类决定 fail-fast 或降级提示；
- diagnostic 模式：探针失败不阻止启动，但记入 ledger，后续所有
  LLM stage 预期降级；
- 探针失败分类复用 §2 注册表；`provider_auth_error`/
  `provider_rate_limited` 两种模式下均 STOP。

---

## 11. 对抗红测清单（实施前置）

全部使用确定性 fake transport / 合成 run，不依赖真实 API。

| # | 场景 | 期望 |
|---|------|------|
| A1 | diagnostic run 以 release 身份验收 | release_check / reconstruction 经 config 派生 purpose 后拒绝 |
| A2 | resume diagnostic run（任何形式） | resume 入口一律拒绝（v1 禁止 diagnostic resume） |
| A3 | 删除 ledger 后验收 | 仍拒绝（identity 防线独立于 ledger） |
| A4 | 替换/篡改 ledger（seq 回退、非白名单字段） | 读取方判篡改；identity 结论不变 |
| A5 | 修改 run_identity.json 的 run_purpose | 与 config 派生期望值及 hash 双重对账失败 → 拒绝 |
| A6 | 同步篡改 run 内 identity+manifest 制造内部一致 | config 哈希链独立派生 purpose → 拒绝（T9） |
| A7 | 整体复制 diagnostic run 目录后翻 purpose | v2 hash 包含 purpose 绑定；复制目录脱离可信 config，派生对账失败 → 拒绝 |
| A8 | 部分搬运 diagnostic artifact 进新 release run | 逐级 authority replay 断链 → 拒绝（现有机制回归） |
| A9 | 失败 stage 留下 DONE/checkpoint/heartbeat/summary 成功痕迹 | 断言零成功形态产物；pipeline_summary 诚实记 FAILED |
| A10 | 未注册异常（如新异常类型） | 分类为 unknown → FAILED_STOP |
| A11 | repair 未耗尽的 content 失败被放行续跑 | 判定违反 requires_repair_exhausted → FAILED_STOP |
| A12 | placeholder 伪造（伪 card/伪 report 喂下游） | integrity_violation → FAILED_STOP |
| A13 | diagnostic run 的 Stage 24/25 | 无 canonical publication；probe 结果不呈现为 stage 已执行 |
| A14 | HTTP 状态误分（408→4xx、429→5xx 等） | 注册表互斥映射红测逐一拒绝 |
| A15 | v1 policy artifact 配 diagnostic purpose 的可信 config | 版本分派判降级攻击 → 立即拒绝（§5.1） |

---

## 12. 分批实施顺序（P1-4 重排）

> 前置门禁：Stage 17 CFS/grounding 三批 + 严格 F0 全部完成。
> **B1–B3 全部完成前，任何真实续跑一律禁止（含 flag 开启）。**
>
> 探针依赖规则：**B4 完成前**，任何 `RUN_MECHANICAL_PROBE` admission
> 必须**确定性降为 `NOT_EXECUTED_MISSING_INPUT`**；不得尝试调用
> 不存在的 probe runner。涉及 probe 的真实续跑必须等待 B4 完成。

| 批次 | 内容 | 文件范围（生产期） | 验收条件 | 回滚边界 |
|------|------|------------------|---------|---------|
| B1 | run identity：config 冻结 `run_purpose` 字段 + v1/v2 版本化投影 + identity 写入/对账 + release_check/reconstruction 拒绝 | `config.py`、`canonical_experiment_evidence.py`（版本分派）、`release_artifacts.py`（identity 写入）、release_check/reconstruction 读取点、新测试 | §11 A1/A2/A5/A6/A7/A15 绿 | flag 关闭后禁止新建 diagnostic run；已生成 identity/ledger 不删除、不回退 |
| B2 | 错误分类注册表 + ledger writer（**只记录，不续跑**） | `diagnostic_taxonomy.py`（新）、ledger writer（新或并入 taxonomy）、新测试 | §11 A3/A4/A10/A11/A14 绿；release 行为零变化回归 | 同 B1 原则 |
| B3 | runner continuation + 两维度 admission + 双报告 | `runner.py`（L921 分支）、`diagnostic_summary.json` writer（新）、新测试 | §11 A8/A9/A12 绿；B1/B2 全部在位才允许开启 | 同 B1 原则 |
| B4 | 机械探针 probe runner + 两层 provider 预检 | `diagnostic_probes.py`（新）、runner 启动钩子（identity 之后）、新测试 | §11 A13 绿；outbound ≤4 红测绿；release fail-fast 验证 | 同 B1 原则 |

每批验收同时要求：既有全部 canonical/production-chain 测试不回归，
且严格 release F0 语义不变化。

---

## 明确不变量（冻结共识）

1. **canonical artifact schema v1 零改动。**
2. **diagnostic run 永远不能转为 release run。**
3. **ledger 不是 authority；删除 ledger 也不能消除 diagnostic
   identity（purpose 真源在 config 哈希链）。**
4. **没有合法输入的阶段只能 NOT_EXECUTED_MISSING_INPUT，
   禁止制造占位业务事实。**
5. **当前 CFS/grounding + 严格 F0 完成前，不进入生产实施。**
6. **`pipeline_summary.json` 成功语义零改动；`failed_continued`
   只存在于非 authority 的 `diagnostic_summary.json`。**
7. **semantic policy v1 运行绝不允许 diagnostic purpose；
   diagnostic run 强制 policy v2 + claim_scope=pipeline_validation。**

---

## 尚未闭合的问题

1. ledger 写入的 fsync 节奏（每事件 fsync vs run 末批量）与崩溃一致性
   取舍。
2. diagnostic run 的命名/目录约定（是否强制 run_id 前缀）——仅可用性，
   不得作为安全机制。
3. gate stage 在 diagnostic 模式下的 HITL 交互形态（非交互环境如何
   处理 BLOCKED_APPROVAL）。
4. L2 schema 探针失败后的二次探针规则（同 run 内 provider 恢复是否
   允许重探，及成本上界）。
