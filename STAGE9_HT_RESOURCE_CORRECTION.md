# STAGE9_HT_RESOURCE_CORRECTION — 科学资源修正裁决（终态存档）

> 本文件是对 `STAGE9_HT_ARBITRATION.md`（首轮架构裁决）的事实修正，已被主裁决接受。
> 存档不构成实施、运行、commit（提交）、push（推送）或新 Silver（银级运行）授权。
> 存档完成后进入 HOLD（冻结），不继续设计或执行。

## 终态

```text
ACCEPT_KIMI_SCIENTIFIC_RESOURCE_CORRECTION
STOP_INPUT_INSUFFICIENT_FOR_NODE_LOCALIZATION
HOLD_STAGE9_IMPLEMENTATION
HOLD_ANY_NEW_SILVER
```

STOP（停止）的实际范围按**全局科研资源不足**理解：当前 4 个独立 base designs
（基础设计）既不足以支持 node-level localization（节点级定位）的论文级泛化，
也不足以支持 design-level detection（设计级检测）的论文级泛化。

## 核实来源（离线，只读）

- `TOPIC.md`（主题文件）：领域为"门级网表（gate-level netlist）中的硬件木马检测"；
  待研究问题三条全部围绕 adaptive attacker（自适应攻击者）威胁模型。
- `MANIFEST.csv`（清单文件）：10 个 Trojan variants（木马变体），仅 4 个 base designs
  （s15850、s35932、s38417、s38584）；同一 base design 的 T100/T200/T300 共享相同
  TjFree（无木马网表），sha256（哈希指纹）逐条证实：
  - s35932 TjFree：`727060c9…`（×3）
  - s38417 TjFree：`c98fecfb…`（×3）
  - s38584 TjFree：`1ea8fc44…`（×3）
- 无独立节点标签文件，仅声明 `has_trojan_signal_doc=TjIn_vs_TjFree_diff`
  （含木马/无木马差分信号文档）；无 baseline allowlist（基线白名单）。

## 对首轮裁决的逐条修正

1. **任务裁决撤回**：首轮从 evaluator（评估器）文件名倒推 node-level localization
   为唯一主任务，属循环论证，撤回。且首轮完全遗漏了 TOPIC 的核心——自适应攻击者
   三问（静态检测器性能变化、防御机制、检测下界），属审题性失误。
2. **统计方案撤回**：首轮"≥3 seeds（随机种子）+ cluster bootstrap（聚类自助法）+
   train/val/test（训练/验证/测试三划分）"方案在 4 个独立单位下不成立，撤回。
   按 variant（变体）切分必然泄漏（同族共享无木马母图）；按 base design 切分仅 4
   个独立样本。
3. **标签表述更正**：首轮称 label（标签）"来自 sealed manifest 的 ground truth
   （真实标签）"为事实错误。节点标签需 TjIn/TjFree 确定性差分推导，可辨识性未验证，
   属未闭合的 label authority（标签权威）前置验收。
4. **baseline 不变量更正**：首轮"baseline ⊆ bundle 白名单"引用了不存在的权威，
   不可执行。如需保留，应改为在 contract（契约）中显式声明并记录来源。
5. **权威方向更正**：必须是 TOPIC → 任务定义 → 选择/校验 evaluator；不允许尚未
   稳定的 registry（注册表）反向控制研究问题。
6. **J 节判语撤回**：首轮输出 `ELIGIBLE_TO_REQUEST_ONE_NEW_SILVER_AFTER_OFFLINE_FIXPOINT`
   过宽。在"不允许第四次 fresh Silver"边界无新证据解除的情况下，应为
   `HOLD_NO_NEW_SILVER`（保持不申请新银级）。本轮正式撤回该判语。

## 候选任务审计表

| 维度 | design-level detection（设计级检测） | node-level localization（节点级定位） |
|---|---|---|
| 标签 | 真实存在（TjIn/TjFree 成对） | 不存在，需差分推导，可辨识性未验证 |
| 独立单位 | 4 个 base designs | 同左，4 个 |
| 合法 split（划分） | 仅 leave-one-base-design-out（留一基础设计法） | 同左 |
| baseline（基线） | 无白名单，须契约显式声明 | 同左 |
| primary metric（主指标） | 图级二分类 F1（F1 值） | 节点级 f1（标签未验证前无意义） |
| 论文级可信 | 否，n=4 仅支持描述性结论 | 否，且多一重标签权威未闭合 |

## 最小可证伪 label-authority 验收（备查，不授权实施）

1. 每个 variant 对 TjIn/TjFree 做确定性差分，输出节点级差异集合；
2. 差分非空且规模在人工可审查范围内；
3. 与 Readme.txt/PDF 的木马结构描述人工抽查一致；
4. 同输入两次运行输出逐字节一致（确定性）；
5. 任一 variant 差分为空或与文档矛盾即剔除并记录；剔除率超阈值（如 >20%）
   整体 fail-closed（失败即关闭）。

## 唯一结论

`STOP_INPUT_INSUFFICIENT_FOR_NODE_LOCALIZATION`

附注：不足是全局性的（detection 同受 n=4 约束）。未来若要 GO（放行），
前提是 label-authority 验收通过、研究目标降级为描述性证据标准、且用户显式
解除 final-run（终态运行）边界，三者缺一不可。

## 仍然有效的首轮裁决内容（未被本次修正推翻）

- domain routing（领域路由）：sealed config 显式声明 canonical domain/task
  （规范领域/任务）作为唯一 authority（权威源），topic 文本不参与路由，
  unknown（未知）或冲突时 fail-closed，消除 generic_sandbox（通用沙箱）静默回退；
- Stage9 反漂移契约中不依赖具体任务的部分：数据集锚定 sealed manifest allowlist
  （允许清单）、Cora/CiteSeer/PubMed denylist（禁止清单）、特征维度禁止硬编码、
  evaluator 绑定；
- 上述内容当前同样处于 HOLD，不构成实施授权。

INDEPENDENT_SCIENTIFIC_RESOURCE_CORRECTION_ONLY_NO_IMPLEMENTATION_NO_RUN
