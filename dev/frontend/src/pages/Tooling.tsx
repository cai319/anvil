import { useEffect, useState } from "react";
import { api, type McpServer, type Skill } from "../api/client";

export function Tooling() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [presets, setPresets] = useState<Record<string, { max_tool_tokens: number }>>({});
  const [error, setError] = useState<string | null>(null);

  // skill form
  const [skillName, setSkillName] = useState("");
  const [skillDesc, setSkillDesc] = useState("");
  const [skillContent, setSkillContent] = useState("");

  // mcp form
  const [mcpName, setMcpName] = useState("");
  const [mcpTransport, setMcpTransport] = useState("stdio");
  const [mcpCommand, setMcpCommand] = useState("");
  const [mcpArgs, setMcpArgs] = useState("");
  const [mcpUrl, setMcpUrl] = useState("");
  const [probe, setProbe] = useState<Record<string, string>>({});

  const refresh = () =>
    Promise.all([api.listSkills(), api.listMcpServers(), api.listToolPresets()])
      .then(([s, m, p]) => {
        setSkills(s);
        setServers(m);
        setPresets(p);
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
  }, []);

  const createSkill = async () => {
    if (!skillName.trim()) return;
    try {
      await api.createSkill({
        name: skillName.trim(),
        description: skillDesc.trim(),
        content: skillContent,
      });
      setSkillName("");
      setSkillDesc("");
      setSkillContent("");
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const createMcp = async () => {
    if (!mcpName.trim()) return;
    try {
      await api.createMcpServer({
        name: mcpName.trim(),
        transport: mcpTransport,
        command: mcpCommand.trim() || null,
        args: mcpArgs.split(/\s+/).filter(Boolean),
        url: mcpUrl.trim() || null,
      });
      setMcpName("");
      setMcpCommand("");
      setMcpArgs("");
      setMcpUrl("");
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const testServer = async (server: McpServer) => {
    try {
      const r = await api.testMcpServer(server.id);
      setProbe((prev) => ({
        ...prev,
        [server.id]: r.ok
          ? `握手成功 · ${r.tools.length} 个工具：${r.tools.map((t) => t.name).join(", ")}`
          : `握手失败：${r.detail ?? "未知原因"}`,
      }));
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      <h1>工具治理</h1>
      {error && <p className="error">{error}</p>}

      <section>
        <h2 className="pane-title">工具预设（tools.yaml）</h2>
        <ul className="drafts">
          {Object.entries(presets).map(([name, p]) => (
            <li key={name}>
              <strong>{name}</strong> <span className="muted">max_tool_tokens={p.max_tool_tokens}</span>
            </li>
          ))}
          {Object.keys(presets).length === 0 && <li className="muted">未发现预设。</li>}
        </ul>
      </section>

      <section>
        <h2 className="pane-title">Skills · {skills.length}</h2>
        <div className="toolbar">
          <input value={skillName} onChange={(e) => setSkillName(e.target.value)} placeholder="skill name" />
          <input value={skillDesc} onChange={(e) => setSkillDesc(e.target.value)} placeholder="一句话描述" style={{ flex: 1 }} />
          <button onClick={createSkill} disabled={!skillName.trim()}>注册 skill</button>
        </div>
        <textarea
          value={skillContent}
          onChange={(e) => setSkillContent(e.target.value)}
          placeholder="SKILL.md 内容（可留空，之后编辑）"
          rows={4}
          style={{ width: "100%" }}
        />
        <ul className="drafts">
          {skills.length === 0 && <li className="muted">暂无注册 skill —— 运行时自身的 skill 目录仍可直接被角色勾选。</li>}
          {skills.map((s) => (
            <li key={s.id}>
              <strong>{s.name}</strong> <span className="muted">{s.description}</span>
              <button className="small danger" onClick={async () => { await api.deleteSkill(s.id); await refresh(); }}>删除</button>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2 className="pane-title">MCP Servers · {servers.length}</h2>
        <div className="toolbar">
          <input value={mcpName} onChange={(e) => setMcpName(e.target.value)} placeholder="name" />
          <select value={mcpTransport} onChange={(e) => setMcpTransport(e.target.value)}>
            <option value="stdio">stdio</option>
            <option value="http">http</option>
          </select>
          {mcpTransport === "stdio" ? (
            <>
              <input value={mcpCommand} onChange={(e) => setMcpCommand(e.target.value)} placeholder="command" />
              <input value={mcpArgs} onChange={(e) => setMcpArgs(e.target.value)} placeholder="args（空格分隔）" style={{ flex: 1 }} />
            </>
          ) : (
            <input value={mcpUrl} onChange={(e) => setMcpUrl(e.target.value)} placeholder="http url" style={{ flex: 1 }} />
          )}
          <button onClick={createMcp} disabled={!mcpName.trim()}>注册 MCP</button>
        </div>
        <ul className="drafts">
          {servers.length === 0 && <li className="muted">暂无 MCP server。</li>}
          {servers.map((s) => (
            <li key={s.id}>
              <strong>{s.name}</strong>{" "}
              <span className="muted mono">{s.transport} {s.command ?? s.url} {s.args?.join(" ")}</span>{" "}
              <span className="muted">工具 {s.tools?.length ?? 0}</span>
              <div className="card-actions">
                <button className="small" onClick={() => testServer(s)}>握商探测</button>
                <button className="small danger" onClick={async () => { await api.deleteMcpServer(s.id); await refresh(); }}>删除</button>
              </div>
              {probe[s.id] && <div className="muted">{probe[s.id]}</div>}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
