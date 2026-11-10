/** run 日志是否只有 create_run 写入的启动头（没有 USER 提示词、也没有模型/工具事件）。 */
export function isHeaderOnlyRunLog(content: string | null | undefined): boolean {
  if (!content) return false;
  const lines = content.trim().split("\n").filter((line) => line.trim().length > 0);
  return lines.length === 1 && lines[0].startsWith("# run ");
}

/** 只有启动头时，解释“为什么没有内容”，避免误以为后台没有交互。 */
export const HEADER_ONLY_RUN_LOG_HINT =
  "该 run 目前只有启动头，没有交互输出：它可能仍在排队/尚未投递，"
  + "也可能是旧版本创建的历史 run（当时未采集 stdout/stderr）。"
  + "若角色正在其他 run 或切片工作，请查看当前运行中的会话；"
  + "任务时间线与活动记录仍可反映后台实际交互。";
