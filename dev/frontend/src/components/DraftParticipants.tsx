import { useEffect, useState } from "react";
import { api } from "../api/client";

export function DraftParticipants({ draftId }: { draftId: string }) {
  const [data, setData] = useState<{
    add: string[]; missing: string[];
  } | null>(null);
  useEffect(() => {
    api.getDraftParticipants(draftId).then(setData).catch(() => {});
  }, [draftId]);
  if (!data || (!data.add.length && !data.missing.length)) return null;
  return (
    <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
      {data.add.length > 0 && <>将新增参与者：{data.add.join("、")}</>}
      {data.add.length > 0 && data.missing.length > 0 && " · "}
      {data.missing.length > 0 && (
        <>human 指定角色未安排，需补进 workflow：{data.missing.join("、")}</>
      )}
    </div>
  );
}
