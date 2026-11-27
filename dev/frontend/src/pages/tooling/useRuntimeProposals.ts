import { useCallback, useState } from "react";
import { api, type RuntimeProposal } from "../../api/client";

export function useRuntimeProposals() {
  const [items, setItems] = useState<RuntimeProposal[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<RuntimeProposal | null>(null);
  const [markdown, setMarkdown] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setListLoading(true);
    setListError(null);
    try {
      const body = await api.listRuntimeProposals();
      setItems(body.items);
      return body.items;
    } catch (e) {
      setListError(String(e));
      return [] as RuntimeProposal[];
    } finally {
      setListLoading(false);
    }
  }, []);

  const selectById = useCallback(async (id: string) => {
    setDetailLoading(true);
    setDetailError(null);
    setMarkdown("");
    try {
      const proposal = await api.getRuntimeProposal(id);
      setSelected(proposal);
      try {
        const md = await api.getRuntimeProposalMarkdown(id);
        setMarkdown(md.content);
      } catch (e) {
        setMarkdown("");
        setDetailError(String(e));
      }
      return proposal;
    } catch (e) {
      setSelected(null);
      setDetailError(String(e));
      return null;
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const clearSelection = useCallback(() => {
    setSelected(null);
    setMarkdown("");
    setDetailError(null);
  }, []);

  const transition = useCallback(async (status: string, note?: string) => {
    if (!selected) return false;
    setDetailLoading(true);
    setDetailError(null);
    try {
      const next = await api.transitionRuntimeProposal(selected.id, status, note);
      setSelected(next);
      await load();
      try {
        const md = await api.getRuntimeProposalMarkdown(next.id);
        setMarkdown(md.content);
      } catch (e) {
        setMarkdown("");
        setDetailError(String(e));
      }
      return true;
    } catch (e) {
      setDetailError(String(e));
      return false;
    } finally {
      setDetailLoading(false);
    }
  }, [selected, load]);

  const exportMarkdown = useCallback(async () => {
    if (!selected) return null;
    try {
      const result = await api.exportRuntimeProposal(selected.id);
      setSelected((prev) => prev ? { ...prev, export_path: result.path } : prev);
      return result.path;
    } catch (e) {
      setDetailError(String(e));
      return null;
    }
  }, [selected]);

  return {
    items, listLoading, listError,
    selected, markdown, detailLoading, detailError,
    load, selectById, clearSelection, transition, exportMarkdown,
  };
}
