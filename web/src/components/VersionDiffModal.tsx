/** 版本差异弹窗：Monaco DiffEditor 对比 code/依赖/参数（M3.2 §Diff）。 */

import { useState } from "react";
import { DiffEditor } from "@monaco-editor/react";
import { Modal, Tabs } from "antd";

export interface DiffPane {
  key: string;
  label: string;
  /** Monaco language id, e.g. "python" or "plaintext". */
  language: string;
  original: string;
  modified: string;
}

interface VersionDiffModalProps {
  open: boolean;
  title: string;
  originalTitle: string;
  modifiedTitle: string;
  panes: DiffPane[];
  onClose: () => void;
}

export default function VersionDiffModal(props: VersionDiffModalProps) {
  const [activeKey, setActiveKey] = useState<string | null>(null);

  const current =
    props.panes.find((pane) => pane.key === activeKey) ?? props.panes[0] ?? null;

  return (
    <Modal
      title={props.title}
      open={props.open}
      width={960}
      footer={null}
      onCancel={props.onClose}
      destroyOnHidden
    >
      {current !== null && (
        <div className="diff-modal" data-testid="version-diff">
          <div className="diff-modal-titles">
            <span>{props.originalTitle}</span>
            <span>{props.modifiedTitle}</span>
          </div>
          <Tabs
            size="small"
            activeKey={current.key}
            onChange={setActiveKey}
            items={props.panes.map((pane) => ({ key: pane.key, label: pane.label }))}
          />
          <DiffEditor
            height="420px"
            language={current.language}
            original={current.original}
            modified={current.modified}
            options={{ readOnly: true, renderSideBySide: true, minimap: { enabled: false } }}
          />
        </div>
      )}
    </Modal>
  );
}
