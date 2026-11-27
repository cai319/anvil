import { useEffect, useMemo, useState } from "react";
import { api, type FeatureFlag } from "../../api/client";
import { Hint } from "../../components/Hint";

const KNOWN_GROUPS: { key: string; title: string; hint: string }[] = [
  { key: "knowledge", title: "知识", hint: "控制主题知识库、任务知识和经验捕获是否参与任务执行。" },
  { key: "context", title: "上下文", hint: "控制任务上下文如何构造、投影和审计。" },
  { key: "workspace", title: "任务隔离", hint: "控制每个任务的工作空间与磁盘清理。" },
  {
    key: "workflow",
    title: "工作流",
    hint: "控制 Cindy 首次规划前的澄清、任务简报与流程强度确认。",
  },
  { key: "collaboration", title: "协作与派遣", hint: "控制 Agent 之间的 @ 路由是否自动创建派遣子任务。" },
  {
    key: "evolution",
    title: "角色演化 / 运行时提案",
    hint: "控制角色 RSI 观测、反思、评估、自动回滚与运行时改进提案。",
  },
  {
    key: "security",
    title: "安全与隔离",
    hint: "控制角色执行与 PoC 运行是否使用 OS 级执行沙箱。",
  },
];

export function FlagsTab() {
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    api.listFeatureFlags()
      .then((data) => setFlags(data.flags ?? []))
      .catch((e) => setError(String(e)));
  }, []);

  const groups = useMemo(() => {
    const known = new Set(KNOWN_GROUPS.map((group) => group.key));
    const unknown = [...new Set(flags.map((flag) => flag.category))]
      .filter((category) => !known.has(category))
      .map((category) => ({
        key: category,
        title: category,
        hint: "后端返回的新分类，前端尚未登记说明。",
      }));
    return [...KNOWN_GROUPS, ...unknown];
  }, [flags]);

  const updateFlag = async (flag: FeatureFlag, value: boolean) => {
    setBusy(flag.key);
    setError(null);
    setNotice(null);
    try {
      const next = await api.setFeatureFlag(flag.key, value);
      setFlags((prev) => prev.map((item) => item.key === next.key ? next : item));
      setNotice(next.overridden
        ? `已${value ? "开启" : "关闭"}「${flag.title}」，立即生效并持久化。`
        : `「${flag.title}」已恢复默认，覆盖记录已清除。`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const enabled = flags.filter((flag) => flag.value).length;
  const overridden = flags.filter((flag) => flag.overridden).length;

  return (
    <div className="tooling-scroll">
      <section className="feature-panel">
        <div className="feature-panel-head">
          <div>
            <h2>功能开关</h2>
            <p>
              修改后立即生效并持久化，重启后保留；只作用于后续任务/运行，不会回改已有数据。
              悬停标题旁的「?」查看完整说明和关闭影响。
            </p>
          </div>
          <span className="feature-summary">
            {enabled}/{flags.length} 已开启
            {overridden > 0 ? ` · ${overridden} 项已覆盖` : ""}
          </span>
        </div>
        {error && <p className="error">{error}</p>}
        {notice && <p className="tooling-notice feature-notice">{notice}</p>}
        {flags.length === 0 && !error ? (
          <p className="muted">加载中…</p>
        ) : (
          <div className="feature-groups">
            {groups.map((group) => {
              const items = flags.filter((flag) => flag.category === group.key);
              if (items.length === 0) return null;
              const onCount = items.filter((flag) => flag.value).length;
              return (
                <section className="feature-group" key={group.key}>
                  <div className="feature-group-head">
                    <div className="feature-group-title">
                      <h3>{group.title}</h3>
                      <span className="chip feature-group-count">
                        {onCount}/{items.length} 开启
                      </span>
                    </div>
                    <p>{group.hint}</p>
                  </div>
                  <div className="feature-tiles">
                    {items.map((flag) => (
                      <div className={`feature-tile ${flag.value ? "is-on" : ""}`} key={flag.key}>
                        <div className="feature-copy">
                          <div className="feature-title-line">
                            <strong>{flag.title}</strong>
                            <Hint text={flag.description} />
                            <span className={`chip feature-source ${flag.overridden ? "is-custom" : ""}`}>
                              {flag.overridden ? "已覆盖" : `默认${flag.default ? "开启" : "关闭"}`}
                            </span>
                          </div>
                          <p>{flag.summary}</p>
                          {busy === flag.key && (
                            <div className="feature-state"><span className="muted">保存中…</span></div>
                          )}
                        </div>
                        <label className="feature-switch">
                          <input type="checkbox" checked={flag.value}
                            disabled={busy === flag.key}
                            aria-label={`${flag.title}：${flag.description}`}
                            onChange={(e) => { void updateFlag(flag, e.currentTarget.checked); }} />
                          <span className="feature-switch-track" aria-hidden="true">
                            <span className="feature-switch-thumb" />
                          </span>
                        </label>
                      </div>
                    ))}
                  </div>
                </section>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
