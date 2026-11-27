import { useEffect, useState } from "react";
import {
  api,
  type IssueTrackerSettings,
  type IssueTrackerTestResult,
} from "../../api/client";
import { Hint } from "../../components/Hint";

type Preset = { max_tool_tokens: number; allowlist: unknown[] };

export function ConfigTab() {
  const [tracker, setTracker] = useState<IssueTrackerSettings | null>(null);
  const [trackerUrl, setTrackerUrl] = useState("");
  const [trackerMaxChars, setTrackerMaxChars] = useState("512");
  const [trackerBusy, setTrackerBusy] = useState<"save" | "test" | null>(null);
  const [trackerNotice, setTrackerNotice] = useState<string | null>(null);
  const [trackerError, setTrackerError] = useState<string | null>(null);
  const [trackerTest, setTrackerTest] = useState<IssueTrackerTestResult | null>(null);
  const [presets, setPresets] = useState<Record<string, Preset>>({});
  const [presetsError, setPresetsError] = useState<string | null>(null);

  useEffect(() => {
    api.getIssueTrackerSettings()
      .then((data) => {
        setTracker(data);
        setTrackerUrl(data.server_url || "");
        setTrackerMaxChars(String(data.field_max_chars ?? 512));
      })
      .catch((e) => setTrackerError(String(e)));
    api.listToolPresets()
      .then(setPresets)
      .catch((e) => setPresetsError(String(e)));
  }, []);

  const trackerUrlValid = !trackerUrl.trim()
    || /^https?:\/\/[^/\s]+/i.test(trackerUrl.trim());
  const trackerMaxCharsValue = Number(trackerMaxChars);
  const trackerMaxCharsValid = /^\d+$/.test(trackerMaxChars.trim())
    && trackerMaxCharsValue >= 64 && trackerMaxCharsValue <= 4096;

  const saveTracker = async () => {
    setTrackerBusy("save");
    setTrackerError(null);
    setTrackerNotice(null);
    setTrackerTest(null);
    try {
      const data = await api.saveIssueTrackerSettings(
        trackerUrl.trim(), trackerMaxCharsValue);
      setTracker(data);
      setTrackerUrl(data.server_url || "");
      setTrackerMaxChars(String(data.field_max_chars));
      setTrackerNotice(data.configured
        ? `问题跟踪服务器已保存：${data.server_url}（字段上限 ${data.field_max_chars}）`
        : `问题跟踪服务器地址已清空（字段上限 ${data.field_max_chars}）。`);
    } catch (e) {
      setTrackerError(String(e));
    } finally {
      setTrackerBusy(null);
    }
  };

  const testTracker = async () => {
    setTrackerBusy("test");
    setTrackerError(null);
    setTrackerNotice(null);
    setTrackerTest(null);
    try {
      const result = await api.testIssueTracker(trackerUrl.trim() || undefined);
      setTrackerTest(result);
      if (result.ok) {
        const detail = [
          result.status !== undefined ? `HTTP ${result.status}` : "",
          result.latency_ms !== undefined ? `${result.latency_ms}ms` : "",
        ].filter(Boolean).join(" · ");
        setTrackerNotice(detail ? `连接成功（${detail}）` : "连接成功");
      } else {
        setTrackerError(result.message || "连接失败");
      }
    } catch (e) {
      setTrackerError(String(e));
    } finally {
      setTrackerBusy(null);
    }
  };

  return (
    <div className="tooling-scroll">
      <section className="tool-import-card tooling-tracker-card">
        <div className="tool-import-head">
          <div>
            <h2>问题跟踪服务器</h2>
            <p>
              配置后，可在任务问题板把 finding 提交到开发问题跟踪系统。
              填写服务根地址即可，例如 <code>http://xxx.xxx.xxx.xxx/api</code> 或{" "}
              <code>http://xxx.xxx.xxx.xxx:5000</code>；系统会自动追加{" "}
              <code>/draft/bulkCreate</code>，无需填写完整接口路径。
              测试连接仅检查接口可达性，不会创建草稿。
              该服务由肖建含（工号 00919930）维护。
            </p>
          </div>
          <span className={`chip ${tracker?.configured ? "is-on" : ""}`}>
            {tracker?.configured ? "已配置" : "未配置"}
          </span>
        </div>
        <div className="tooling-tracker-form">
          <label>
            服务器地址
            <input
              type="url"
              value={trackerUrl}
              placeholder="http://xxx.xxx.xxx.xxx/api"
              onChange={(e) => {
                setTrackerUrl(e.target.value);
                setTrackerNotice(null);
                setTrackerTest(null);
              }}
            />
          </label>
          <label>
            <span className="tooling-label-line">
              单字段最大字符数
              <Hint text="只影响提交到外部问题跟踪系统的内容，不会修改本地 finding。description_md 承载完整分析，不限制长度；风险描述、模块/函数、前置条件、攻击路径、触发点、修复建议等其他文本字段超过该值时，会先由 LLM 在保留原意的前提下压缩；如果 LLM 调用失败或超时，才按该上限硬截断，确保提交不中断。risk_type、severity、fix_priority 属于枚举值，不做 LLM 改写，但最终同样受截断兜底。可设置 64–4096。" />
            </span>
            <input
              type="number"
              min={64}
              max={4096}
              step={1}
              value={trackerMaxChars}
              onChange={(e) => {
                setTrackerMaxChars(e.target.value);
                setTrackerNotice(null);
              }}
            />
          </label>
        </div>
        <div className="card-actions">
          <button type="button" className="primary"
            disabled={trackerBusy !== null || !trackerUrlValid || !trackerMaxCharsValid}
            onClick={() => { void saveTracker(); }}>保存</button>
          <button type="button"
            disabled={trackerBusy !== null || !trackerUrlValid
              || (!trackerUrl.trim() && !tracker?.configured)}
            onClick={() => { void testTracker(); }}>测试连接</button>
          {trackerBusy === "save" && <span className="muted">保存中…</span>}
          {trackerBusy === "test" && <span className="muted">测试中…</span>}
        </div>
        {tracker?.configured && tracker.server_url !== trackerUrl.trim() && (
          <p className="muted">当前已保存：{tracker.server_url}</p>
        )}
        {trackerError && <p className="error">{trackerError}</p>}
        {trackerNotice && <p className="tooling-notice">{trackerNotice}</p>}
        {trackerTest && (
          <p className="muted">
            端点：<code>{trackerTest.endpoint}</code>
            {trackerTest.status !== undefined ? ` · HTTP ${trackerTest.status}` : ""}
            {trackerTest.latency_ms !== undefined
              ? ` · ${trackerTest.latency_ms}ms`
              : trackerTest.elapsed_ms !== undefined
                ? ` · ${trackerTest.elapsed_ms}ms`
                : ""}
            {trackerTest.error_type ? ` · ${trackerTest.error_type}` : ""}
          </p>
        )}
      </section>

      <section className="tool-import-card">
        <div className="tool-import-head">
          <div>
            <h2>工具预设</h2>
            <p>来自 <code>tools.yaml</code>，只读。预设实际在角色工坊绑定。</p>
          </div>
          <a className="small button-like" href="/runtimes">打开运行时页</a>
        </div>
        {presetsError && <p className="error">{presetsError}</p>}
        {Object.keys(presets).length === 0 && !presetsError ? (
          <p className="muted">未发现预设。</p>
        ) : (
          <ul className="tool-presets-list">
            {Object.entries(presets).map(([name, preset]) => (
              <li key={name}>
                <div>
                  <strong>{name}</strong>
                  <span className="muted">
                    {" "}max_tool_tokens={preset.max_tool_tokens}
                    {" · "}allowlist {preset.allowlist?.length ?? 0} 项
                  </span>
                </div>
                {preset.allowlist?.length > 0 && (
                  <details>
                    <summary className="muted">查看 allowlist</summary>
                    <pre>{JSON.stringify(preset.allowlist, null, 2)}</pre>
                  </details>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <p className="muted tooling-config-link">
        运行时定义、探测和健康检查在 <a href="/runtimes">运行时</a> 页面维护。
      </p>
    </div>
  );
}
