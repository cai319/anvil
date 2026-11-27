# ADR-0001: Clarify before Cindy plans

- **Status**: Accepted
- **Date**: 2026-11-27
- **Deciders**: human owner, Cindy planning design session
- **Related**: `docs/superpowers/specs/2026-11-27-cindy-clarification-spec.md`, `GLOSSARY.md`

## Context

Cindy（Anvil 的 onboarding planner）今天的规划链路是：

```
dispatch → planner run → workflow draft → human approve → workflow
```

规划前没有澄清闸门。人类只能在草案产出后审批或驳回；意图偏差往往到那时才暴露，
返工成本高。同时，系统已有 `workflow weight = light | standard | strict`，
但前端入口在工作流页签，只有 workflow 已生成后才出现；运行中修改只影响下一次执行。
这是一次**事后**决策，无法影响本次 workflow 的切片粒度与严格度。

需求是：Cindy 在首次规划任务前，通过分轮提问逼近真实目的，判断投入强度，
产出可确认的任务简报，再开始规划工作流。

## Decision

1. **触发面收窄**：澄清只发生在 Cindy 担任 planner 的**首次规划阶段**。
   任务一旦有过任何 workflow、已经执行过、或状态非 `todo`，澄清子系统完全惰性；
   不判 stale、不设门禁、不改变 dispatch / revision / re-plan 行为。
2. **协议 + 门禁，而不是注入 skill**：新增 `[anvil:clarify]` run 与
   `CLARIFY_PROTOCOL`；服务端在澄清未确认时拒绝创建 workflow /
   workflow_revision 草案。不依赖 Claude Code 的 skill 发现机制。
3. **任务简报（Intent Brief）**：结构化记录目标、真实意图、非目标、约束、验收、
   假设、开放问题、流程强度、计划形态。DB 是真相源，任务工作区投影
   `intent-brief.json/.md`；human 可编辑，版本化。
4. **强度在澄清中确认**：Cindy 提议 `light | standard | strict` 与 `plan_shape`
   （flat/slices/auto），human 在简报卡确认。确认时写
   `tasks.workflow_weight`（source=human），沿用现有 `weights.resolve()` 优先级；
   已确认 weight 同步写入 workflow 草案树，`plan_shape` 由服务端校验结构
   （flat 拒绝 plan_slices/loop，slices 要求 plan_slices+loop，auto 不约束）。
   工作流页的强度入口降级为「高级 → 仅影响下一次执行」。
5. **确认是结构化人类动作**：简报卡按钮是唯一状态迁移入口；聊天文本「确认」
   不会直接改状态，Cindy 只重贴卡片。
6. **Cindy 专属**：其他角色担任 planner 时不启用内置澄清；用户可在角色定义里
   自行给角色勾选 skill。
7. **确认权**：系统没有人类身份表，human 是单一共享 token 主体；任何 human
   token 持有者都可回答与确认，确认记录进版本表；冲突必须显式裁决。

## Consequences

**Positive**

- 意图偏差在规划前暴露，降低草案驳回与 workflow 返工。
- 简报可审计、可复用；重规划直接复用，不重复审讯。
- 强度决策前移到澄清阶段，能影响本次 workflow 的严格度与计划形态。
- 服务端门禁保证「未确认简报不可能产出 workflow」，不依赖模型自觉。
- 已运行任务零影响，风险局部化。

**Negative / costs**

- 首次规划多一个澄清阶段，轻量任务需要「跳过」兜底。
- 新增 tasks 字段、版本表、API、run kind、协议、UI 卡片。
- 多 human 场景无法按人授权（无身份系统），只能依赖显式冲突裁决。
- Cindy 的澄清轮次会占用常驻会话上下文；需要轮次上限与简报收敛。

## Alternatives considered

1. **只注入 grill-me skill / 只改 prompt**：不可靠。Anvil 没有内置 skill 机制，
   planner prompt 独立组装，模型可以选择不提问；无法保证「先澄清后规划」。
2. **在工作流页签做事后强度选择**：已是现状，问题正是「workflow 已生成」，
   只能影响下一次执行。不满足需求。
3. **所有任务强制走 Cindy 规划**：会让单角色小任务多出规划 + 草案审批两步，
   改变现有 dispatch 语义；v1 不做。
4. **给所有角色内置澄清**：用户明确要求 Cindy 专属；其他角色由用户自选 skill。
5. **结构化问题实体/表单**：工作量大，且对话体验变重；v1 只结构化简报与档位，
   问题保持 @ 文本。

## Follow-ups

- 实施按 Spec §12 的 P1–P8；版本号提交前确认。
- 观察澄清轮次、跳过率、草案驳回率、workflow 修订率。
- 若未来需要多 human 权限，先补人类身份系统，再收紧确认权。
