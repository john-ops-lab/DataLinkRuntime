/** 版本差异弹窗：Monaco DiffEditor 对比 code/依赖/参数（M3.2 §Diff）。 */

import { useState } from "react";
import { DiffEditor } from "@monaco-editor/react";
import { Button, Modal, Tabs } from "antd";

import ActionWithReason from "./ActionWithReason";

export interface DiffPane {
  key: string;
  label: string;
  /** Monaco language id, e.g. "python" or "plaintext". */
  language: string;
  original: string;
  modified: string;
}

/** M5.5.4：AI Candidate 的 Apply 动作（只出现在 Candidate Diff 中）。 */
export interface DiffApplyAction {
  /** 按钮文案：正常 "应用修改"，stale 时 "仍然应用"。 */
  label: string;
  /** null = 可应用；非空时按钮禁用并给出原因。 */
  reason: string | null;
  /** Apply 已成功后只读展示 "已应用"。 */
  applied: boolean;
  /** stale 时在 Apply 区展示覆盖警告。 */
  stale: boolean;
  onApply: () => void;
}

interface VersionDiffModalProps {
  open: boolean;
  title: string;
  originalTitle: string;
  modifiedTitle: string;
  panes: DiffPane[];
  onClose: () => void;
  /** 传入时渲染 "[应用修改] [关闭]" 底部操作；否则不渲染底部。 */
  applyAction?: DiffApplyAction | null;
}

export default function VersionDiffModal(props: VersionDiffModalProps) {
  const [activeKey, setActiveKey] = useState<string | null>(null);

  const current =
    props.panes.find((pane) => pane.key === activeKey) ?? props.panes[0] ?? null;

  const applyAction = props.applyAction ?? null;

  return (
    <Modal
      title={props.title}
      open={props.open}
      width={960}
      footer={
        applyAction === null ? null : (
          <div className="diff-modal-footer">
            {applyAction.stale && (
              <div className="ai-stale-warning" role="alert" data-testid="diff-candidate-stale">
                <strong>⚠ AI 生成期间工作副本已发生修改。</strong>
                <span>该候选修改基于较早的编辑内容生成，应用会覆盖当前工作副本。</span>
              </div>
            )}
            {applyAction.applied && (
              <p className="ai-candidate-applied" role="status" data-testid="diff-candidate-applied">
                已应用到浏览器工作副本；请继续人工保存、测试与运行。
              </p>
            )}
            <div className="diff-modal-actions">
              <ActionWithReason label="应用修改" reason={applyAction.reason}>
                <Button
                  type="primary"
                  data-testid="diff-apply-candidate"
                  disabled={applyAction.reason !== null}
                  onClick={applyAction.onApply}
                >
                  {applyAction.applied ? "已应用" : applyAction.label}
                </Button>
              </ActionWithReason>
              <Button data-testid="diff-close" onClick={props.onClose}>
                关闭
              </Button>
            </div>
          </div>
        )
      }
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
            // M5.5.6：Modal destroyOnHidden 卸载 DiffEditor 时，@monaco-editor/react
            // 默认先 dispose 内部 model 再 dispose DiffEditorWidget，触发
            // "TextModel got disposed before DiffEditorWidget model got reset"
            // 的 BugIndicatingError（页面 error 事件）。固定 model path 并在
            // 卸载时保留 model（由 Monaco model 缓存复用），消除卸载时序竞争，
            // 也不会累积无 uri 的孤儿 model。
            originalModelPath="dlr-diff-original"
            modifiedModelPath="dlr-diff-modified"
            keepCurrentOriginalModel
            keepCurrentModifiedModel
            options={{ readOnly: true, renderSideBySide: true, minimap: { enabled: false } }}
          />
        </div>
      )}
    </Modal>
  );
}
