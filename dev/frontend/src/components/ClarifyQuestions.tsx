import { useState } from "react";
import { api } from "../api/client";

export interface ClarifyQuestionOption {
  key: string;
  text: string;
}

export interface ClarifyQuestion {
  id?: string;
  text: string;
  options: ClarifyQuestionOption[];
  recommend?: string;
  why?: string;
}

const BLOCK_RE = /```anvil-questions\s*\n?([\s\S]*?)```/i;

function normalizeQuestion(raw: unknown): ClarifyQuestion | null {
  if (!raw || typeof raw !== "object") return null;
  const item = raw as Record<string, unknown>;
  const text = String(item.text || "").trim();
  if (!text || !Array.isArray(item.options)) return null;
  const options: ClarifyQuestionOption[] = [];
  const seen = new Set<string>();
  for (const option of item.options) {
    if (!option || typeof option !== "object") continue;
    const opt = option as Record<string, unknown>;
    const key = String(opt.key || "").trim().toUpperCase();
    const label = String(opt.text || "").trim();
    if (!key || !label || seen.has(key)) continue;
    seen.add(key);
    options.push({ key, text: label });
  }
  if (options.length < 2) return null;
  const recommendRaw = String(item.recommend || "").trim().toUpperCase();
  const recommend = seen.has(recommendRaw) ? recommendRaw : undefined;
  return {
    id: item.id == null ? undefined : String(item.id),
    text,
    options: options.slice(0, 4),
    recommend,
    why: item.why == null ? undefined : String(item.why).trim() || undefined,
  };
}

export function parseClarifyQuestions(message: string): {
  text: string;
  questions: ClarifyQuestion[];
} {
  const match = message.match(BLOCK_RE);
  if (!match) return { text: message, questions: [] };
  let data: unknown;
  try {
    data = JSON.parse(match[1].trim());
  } catch {
    return { text: message, questions: [] };
  }
  const raw = (data as { questions?: unknown })?.questions;
  if (!Array.isArray(raw)) return { text: message, questions: [] };
  const questions = raw.map(normalizeQuestion).filter(
    (q): q is ClarifyQuestion => q !== null).slice(0, 3);
  if (questions.length === 0) return { text: message, questions: [] };
  const start = match.index ?? 0;
  const text = (message.slice(0, start) + message.slice(start + match[0].length)).trim();
  return { text, questions };
}

export function ClarifyQuestions({
  taskId, planner, questions, answered = false, clarifyStatus = "none",
  onAnswered,
}: {
  taskId: string;
  planner: string;
  questions: ClarifyQuestion[];
  /** 该条问题消息之后是否已经有人类回复；用于刷新/重进后仍保持锁定。 */
  answered?: boolean;
  /** 当前任务澄清状态；只有 in_progress 时允许回答。 */
  clarifyStatus?: string;
  onAnswered: () => void;
}) {
  const [selected, setSelected] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const allAnswered = questions.every((_, index) => Boolean(selected[index]));
  const clarified = sent || answered;
  const active = clarifyStatus === "in_progress";
  const locked = busy || clarified || !active;
  const lockNote = !active
    ? clarifyStatus === "confirmed"
      ? "简报已确认，澄清问题已锁定。"
      : clarifyStatus === "skipped"
        ? "已跳过澄清，问题已锁定。"
        : clarifyStatus === "stale"
          ? "简报已过期，请重新澄清。"
          : "澄清不在进行中，问题已锁定。"
    : clarified
      ? "已回答，等待 Cindy 下一轮。"
      : "";
  const buttonLabel = !active
    ? clarifyStatus === "confirmed"
      ? "已确认"
      : clarifyStatus === "skipped"
        ? "已跳过"
        : clarifyStatus === "stale"
          ? "已失效"
          : "已锁定"
    : clarified
      ? "已回答"
      : "发送回答给 Cindy";

  const send = async () => {
    if (!allAnswered || busy || sent || answered || !active) return;
    setBusy(true);
    setError(null);
    try {
      const lines = questions.map((question, index) => {
        const key = selected[index];
        const option = question.options.find((item) => item.key === key);
        return `Q${index + 1} ${question.text} → ${key}. ${option?.text || ""}`;
      });
      await api.mentionTask(
        taskId, planner || "cindy",
        `回答 Cindy 的澄清问题：\n${lines.join("\n")}`);
      setSent(true);
      onAnswered();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="clarify-questions">
      <div className="cq-head">澄清问题</div>
      {questions.map((question, index) => (
        <div className="cq-question" key={question.id || index}>
          <div className="cq-text">Q{index + 1}. {question.text}</div>
          <div className="cq-options">
            {question.options.map((option) => (
              <button
                key={option.key}
                type="button"
                className={`cq-option ${selected[index] === option.key ? "is-active" : ""}`
                  + `${question.recommend === option.key ? " is-rec" : ""}`}
                disabled={locked}
                onClick={() => setSelected((prev) => ({ ...prev, [index]: option.key }))}
              >
                <strong>{option.key}</strong>
                <span>{option.text}</span>
                {question.recommend === option.key && (
                  <em className="cq-rec">推荐</em>
                )}
              </button>
            ))}
          </div>
          {question.recommend && question.why && (
            <div className="cq-why">
              Cindy 推荐 {question.recommend}：{question.why}
            </div>
          )}
        </div>
      ))}
      <div className="cq-actions">
        <button className="primary" type="button"
          disabled={locked || !allAnswered} onClick={send}>
          {buttonLabel}
        </button>
        {!allAnswered && !locked && (
          <span className="muted">请为每个问题选择一个选项</span>
        )}
        {lockNote && <span className="muted cq-lock-note">{lockNote}</span>}
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
