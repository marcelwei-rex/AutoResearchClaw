# STAGE9_HT_SCIENTIFIC_AND_METRIC_AUTHORITY_ARCHITECTURE — 独立离线裁决

## A. 模型身份与独立性声明

本裁决由 Kimi（Moonshot AI）生成；具体模型版本号无法在本环境中确认，记为**未知**。本裁决与 AutoResearchClaw v2 的运行链路、provider（DeepSeek）、metric-authority 开发分支无任何实现或利益关联；全程离线，未执行任何代码、未调用任何数据接口，仅依据任务书陈述的事实进行推理裁决。

## B. 唯一根因裁决

**Primary root cause**：sealed bundle 的"任务粒度—domain—metric key"三元组没有任何单一权威源唯一声明，导致 domain 由脆弱的 topic 文本匹配决定（中文未命中而回落 generic_sandbox），metric key 由 config 单方命名而与 evaluator 权威脱节（detection_f1 ≠ f1）。

**Secondary root cause**：Stage9 缺少对实验设计的机器可验证不变量契约，LLM 在无数据集锚定的情况下套用 Planetoid/Cora 图节点分类模板，产生科学离题。

（合计约 145 字）

## C. 科研任务权威表

本轮 Silver 主任务裁决为：**node/gate-level localization（门级网表节点级硬件木马定位）**。理由：sealed bundle 指向 Trust-Hub / ISCAS-85，选定 evaluator `trojnet_iscas85_graphsage_localization-v2.json` 即 TrojNet 式 GraphSAGE 定位器，其数据结构（网表图 + 节点标签）天然对应节点级二分类定位；design-level binary detection 与 family-level identification 均无对应 evaluator 与标签结构支撑，禁止混入。

| 维度 | 裁决值 |
|---|---|
| task | node/gate-level trojan localization（网图节点二分类：trojan / non-trojan） |
| observation unit | 单个网表图（一个 design/netlist，含全部节点与边） |
| prediction unit | 单个节点（gate）的 trojan 概率与二值判定 |
| label unit | 单个节点的 trojan / non-trojan 标签（来自 sealed manifest 的 ground truth） |
| split unit | **design/netlist 整图**（图级划分；同一 design 的节点不得跨 train/val/test） |
| statistical independence unit | design/netlist（节点在同一图内拓扑相关，不构成独立样本） |
| leakage boundary | ① 同一 design 的任何节点/子图不得同时出现在不同 split；② scaler/特征归一化统计仅由 train split 拟合；③ threshold 只允许在 val split 上选择，禁止用 test 调阈值；④ 禁止跨 split 共享图级统计量（度分布、 motif 计数）作为特征来源 |

## D. metric authority 裁决

**Primary metric key：`f1`**（与 evaluator `trojnet_iscas85_graphsage_localization-v2.json` 的 metric authority 一致；config 必须向 evaluator 对齐，而非反向）。

- **数学定义**：对正类（trojan 节点）的二值 F1 = 2PR/(P+R)，P = TP/(TP+FP)，R = TP/(TP+FN)，在节点级预测上计算。
- **averaging**：binary（仅正类），禁止 macro/micro/weighted（本任务负类占绝对多数，macro 与 accuracy 类指标会稀释定位能力）。
- **threshold 规则**：固定 operating point，在 validation split 上按 F1 最大选定一次后锁定；test 只评估、不调参；若无 val split，则默认 0.5 并在报告中显式声明。
- **置信区间 / 重复实验**：≥ 3 个随机种子重复，报告 mean ± std；CI 以 **design（split unit）为抽样单位做 cluster bootstrap**（如 B=1000 次重采样 design 后聚合节点预测），禁止以节点为单位 bootstrap（违反独立性）。
- **Secondary metrics**：precision（正类）、recall（正类）、AUPRC；可附 AUROC 仅作参考。TP/FP/FN 原始计数必须随报告输出。
- **禁止作为 primary**：accuracy；macro/micro-F1；AUROC 单独；`detection_f1`（除非在 evaluator 权威内被定义为与本条完全等价——当前不是，故禁止）；任何 design-level 聚合指标（粒度不符）。

## E. domain routing 唯一方案

**裁决：方案 B —— sealed config 显式声明 canonical domain/task，作为唯一 authority。**

- **唯一 authority**：sealed config 中的 `canonical_domain`（= hardware_trojan）与 `canonical_task`（= node_level_localization）两字段。domain-selector-v2.json 的 topic 文本匹配**不再决定 domain**，仅允许作为冗余一致性提示。
- **topic 文本不能决定 domain**：中/英文措辞、同义词、拼写变体一律不参与路由；"硬件木马"与"hardware trojan"表达同一 bundle，路由结果必然一致，因为二者读的是同一个 sealed config。
- **generic_sandbox 静默 fallback 消除**：未声明 canonical 字段、或声明值不在 registry 允许集内 → **fail-closed**，Stage9 直接以 contract error 终止，禁止回落任何默认 domain。
- **冲突处理**：config 声明与 bundle manifest 的 dataset identity（Trust-Hub/ISCAS-85）不一致 → fail-closed，而不是二选一。manifest 只做一致性校验输入，**不构成第二套路由 authority**。
- **为何不采用其他方案**：A（补中文 selector 模式）语言脆弱、永不完备，措辞漂移即失效；C（按 manifest/dataset identity 路由）无法区分同一数据集上的不同任务粒度（Trust-Hub 同时支持 detection 与 localization）， authority 粒度不足；D 无必要——B 已是最小确定性方案。

## F. Stage9 最小反漂移契约（机器可验证不变量）

以下 8 条必须在无真实 API 的离线 contract 校验中全部通过，任一失败即 fail-closed：

1. **数据集锚定**：Stage9 输出引用的每个 dataset identifier 必须逐字命中 sealed manifest 的 benchmark allowlist；出现 Cora / CiteSeer / PubMed / Planetoid 等词表外名称（lexical denylist）即拒绝。
2. **特征维度推导**：input/feature dimension 必须由加载的 artifact 在运行时推导；静态扫描 Stage9 产物，禁止出现硬编码数值 input_dim（如 1433）。
3. **baseline 白名单**：声明的 baseline 集合 ⊆ bundle 允许的 HT baseline 集合（含 TrojNet GraphSAGE 及 manifest 声明项）。
4. **粒度一致**：experiment/aggregation unit 必须等于 config 声明的 `canonical_task` 粒度（node-level）；禁止 design-level 聚合混入。
5. **split 粒度**：split 必须在 design/netlist 级声明；出现节点级随机划分即拒绝。
6. **metric key 一致**：`primary_metric.key` 必须存在于解析后 evaluator 的 metric authority 键集内。
7. **threshold 来源**：threshold 与 averaging 必须取自 contract 字段，禁止由模型自由文本指定。
8. **evaluator 绑定**：Stage9 引用的 evaluator id 必须等于 sealed config 指定项，禁止自选 evaluator。

**职责划分**：①–⑧ 全部由确定性 contract 阻止；prompt 约束仅负责引导叙述语气与 HT 术语（不承载任何正确性责任）；方法论的科学合理性（如 GraphSAGE 层数选择是否恰当）由人工审查发现，不做机器拦截。

## G. 必须修 / 延期 / 停止清单

**必须修（最小范围）**
- `sealed config`：增补 `canonical_domain` / `canonical_task` 两个声明字段；将 `primary_metric.key` 从 `detection_f1` 改为 `f1`（向 evaluator 对齐）。
- domain router：改为只读 sealed config 声明；删除/阻断 generic_sandbox 静默 fallback，改为 fail-closed error。
- Stage9 contract validator：落地 F 节 8 条不变量的离线校验。
- 工作区卫生：metric-authority 子系统的未提交开发状态必须了结——要么经审查后提交最小相关改动，要么明确排除，保证 committed tree == 运行 tree。

**可延期**
- 双语/多语 topic selector 扩充；任何新 schema、新 gate、新 lifecycle、registry 抽象层；通用"防漂移平台"；`detection_f1` 别名注册（若未来确有多任务复用再议）。

**必须停止**
- 第四次 fresh Silver、resume/from-stage/splice；以测试数量或 schema 数量作为进度理由；为本次 HT 问题建设通用平台；任何 Route C 恢复动作。

## H. 离线 fixpoint 验收标准

全部满足前不得申请下一次 Silver，每条须有可出示证据：

1. **路由一致性**：同一 sealed bundle，topic 分别以中文（硬件木马/门级网表）与英文（hardware trojan）表述，离线路由结果逐字节相同，均命中 hardware_trojan。
2. **粒度唯一**：sealed bundle 解析出的 task granularity 唯一且等于 node_level_localization，无候选冲突。
3. **metric 一致**：config `primary_metric.key=f1` 在 evaluator 权威键集内命中；`detection_f1` 路径在 HT domain 下 fail-closed。
4. **fallback 不静默**：构造缺声明/未知 domain/冲突声明三类输入，均产生显式 contract error，无一进入 generic_sandbox。
5. **漂移拒绝**：构造含 Cora/CiteSeer/PubMed/硬编码 input_dim 的 Stage9 模拟输出，contract 校验逐条拒绝并定位到具体不变量编号。
6. **无 API 全链路**：在无真实 provider 的 stub 模式下跑通 Stage9 全 contract 校验（pre-LLM 与 post-LLM 两道）。
7. **树一致**：`git status` 干净或仅剩与本修复无关且被显式排除的文件；committed tree 与运行 tree hash 一致。
8. **脏文件隔离**：unrelated dirty files 有明确排除清单，不被纳入运行证据。

## I. 最大实施预算

- production LOC ≤ **300**；tests LOC ≤ **400**（合计 ≤ 700，超出即触发审查）。
- correction 轮次 ≤ **2**；第 3 次修复需求出现即 STOP 并回到裁决。
- 硬 STOP 条件：① 需要新增 registry 抽象/lifecycle 才能表达本契约；② 修复触及 Stage9 以外的 stage 语义；③ 任何"顺手"重构使 diff 超出上述 LOC；④ 离线 fixpoint 8 条中任一条无法在不扩大预算的前提下达成。

## J. 是否允许申请下一次Silver

**ELIGIBLE_TO_REQUEST_ONE_NEW_SILVER_AFTER_OFFLINE_FIXPOINT**

（仅表示 H 节 8 条全部离线满足后，可以向用户申请一次新 Silver；不构成运行授权。若 fixpoint 无法在本预算内闭合，或闭合过程暴露出 contract/registry 需要结构性扩建，则应永久停止本回顾性验收。）

> **注**：本节判语已被 `STAGE9_HT_RESOURCE_CORRECTION.md` 正式撤回，更正为
> `HOLD_NO_NEW_SILVER`。保留原文仅为治理链完整性。

## K. 最终终态

INDEPENDENT_STAGE9_HT_ARCHITECTURE_ARBITRATION_ONLY_NO_IMPLEMENTATION_NO_RUN
