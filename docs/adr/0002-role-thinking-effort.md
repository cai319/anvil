# ADR-0002: Role-level thinking effort

- **Status**: Accepted
- **Date**: 2026-11-27
- **Related**: `docs/superpowers/specs/2026-11-27-cindy-clarification-spec.md`, `GLOSSARY.md`

## Context

Claude Code 2.1.x 支持 `--effort <level>`，运行时 YAML 也已声明
`capabilities.thinking_effort: true`。Anvil 的角色运行时配置（`definition.runtime`）
目前只有 `id` 和 `permission_mode`，没有角色级的 reasoning effort；所有角色只能继承
运行时/宿主机默认值。

需求：每个角色的设置里暴露思考强度；Cindy（内置规划助理）默认最高，其他角色默认高。

## Decision

1. **字段**：角色定义新增 `definition.runtime.effort`，取值
   `low | medium | high | max`（展示为 低 / 中 / 高 / 最高）。
2. **默认值**：
   - Cindy / 任意 `kind=onboarding` 角色：`max`；`ensure_onboarding()` 幂等补齐，
     但保留人工已设置的有效值。
   - 其他角色：`high`；`roles.create()` 写入显式默认，`effective_config()` 对缺失值
     注入默认，存量角色无需批量迁移。
3. **运行时注入**：Claude Code 运行时 YAML 在 `invocation.args`、`session.args_new`、
   `session.args_resume` 中加入 `--effort {{ effort }}`；渲染上下文由
   `_render_invocation` / `_render_persistent` 从 `effective.projection.runtime.effort`
   （回退角色定义）解析。不支持 `thinking_effort` 的运行时（codex/gemini）不读取该字段。
4. **API / UI**：`PUT /roles/{slug}/runtime` 接受可选 `effort`，并保留
   `permission_mode`、`model_pool` 等其它 runtime 字段；角色 → 运行时页签提供
   思考强度下拉与「保存运行时设置」。
5. **不改动**：任务级、run 级不单独覆盖 effort；角色设置是唯一入口。运行中的进程沿用
   启动时的参数，下一次 spawn/resume 生效。

## Consequences

**Positive**

- 角色级 effort 可审计、可配置；Cindy 的规划/澄清默认获得最高思考强度。
- 默认值明确：新角色 high，Cindy max；存量角色通过 effective config 获得默认，无需迁移。
- 运行时能力声明与实现一致；非 Claude Code 运行时不需改 YAML。

**Negative / risks**

- 旧版 Claude Code 若不识别 `--effort`，spawn 会失败；运行时发现/健康检查需确保
  版本支持（当前本机 2.1.332 已验证）。必要时在运行时 YAML 或版本探测中加入版本门槛。
- 修改 effort 不会热切换已有常驻进程；需重启会话或等待下一次 idle release 后恢复。
- `permission_mode` 之前会被「保存绑定」重置；本 ADR 的实现一并修复为保留原字段。

## Alternatives considered

1. **环境变量（如 `CLAUDE_CODE_EFFORT`）**：不按角色区分，且 Anvil 无法按角色注入
   不同值；放弃。
2. **全局默认 + 任务级覆盖**：与现有角色运行时配置割裂；任务不是思考强度的自然归属。
3. **仅前端提示、不改 invocation**：不会真正影响运行时；放弃。
