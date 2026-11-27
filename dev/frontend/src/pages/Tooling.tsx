import { useCallback, useState } from "react";
import { PageHeader } from "../components/ui";
import { ConfigTab } from "./tooling/ConfigTab";
import { FlagsTab } from "./tooling/FlagsTab";
import { ImportTab } from "./tooling/ImportTab";
import { ProposalsTab } from "./tooling/ProposalsTab";
import { TOOLING_TABS, readToolingRoute, writeToolingRoute, type ToolingTab } from "./tooling/toolingRoute";

export function Tooling() {
  const initial = readToolingRoute();
  const [tab, setTab] = useState<ToolingTab>(initial.tab);
  const [ripId, setRipId] = useState<string | null>(initial.ripId);
  const [visited, setVisited] = useState<Set<ToolingTab>>(
    () => new Set<ToolingTab>([initial.tab, "proposals"]),
  );
  const [proposalCount, setProposalCount] = useState(0);

  const selectTab = useCallback((next: ToolingTab) => {
    setTab(next);
    setRipId(null);
    setVisited((prev) => new Set(prev).add(next));
    writeToolingRoute(next);
  }, []);

  const selectProposal = useCallback((id: string | null) => {
    setTab("proposals");
    setRipId(id);
    setVisited((prev) => new Set(prev).add("proposals"));
    writeToolingRoute("proposals", id);
  }, []);

  const onActiveCount = useCallback((count: number) => setProposalCount(count), []);

  return (
    <div className="tooling-page">
      <PageHeader
        kicker="TOOLING"
        title="工具治理"
        subtitle="工具配置 · 工具导入 · 功能开关 · 运行时提案"
      />

      <div className="tabs tooling-tabs" role="tablist" aria-label="工具页子页面">
        {TOOLING_TABS.map((item) => (
          <button
            key={item.key}
            role="tab"
            aria-selected={tab === item.key}
            className={tab === item.key ? "active" : ""}
            onClick={() => selectTab(item.key)}
          >
            {item.label}
            {item.key === "proposals" && proposalCount > 0 && (
              <span className="tab-count">{proposalCount}</span>
            )}
          </button>
        ))}
      </div>

      <div className="tooling-tab-stage">
        <div className="tooling-tab-panel" hidden={tab !== "config"}>
          {visited.has("config") && <ConfigTab />}
        </div>
        <div className="tooling-tab-panel" hidden={tab !== "import"}>
          {visited.has("import") && <ImportTab />}
        </div>
        <div className="tooling-tab-panel" hidden={tab !== "flags"}>
          {visited.has("flags") && <FlagsTab />}
        </div>
        <div className="tooling-tab-panel" hidden={tab !== "proposals"}>
          <ProposalsTab
            ripId={ripId}
            onSelectProposal={selectProposal}
            onActiveCount={onActiveCount}
          />
        </div>
      </div>
    </div>
  );
}
