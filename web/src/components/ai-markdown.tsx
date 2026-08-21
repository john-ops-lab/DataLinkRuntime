/**
 * M5.7 Wave A: Markdown / GFM / Code Block / Copy rendering for the AI
 * Assistant conversation, built on assistant-ui's MarkdownTextPrimitive with
 * DLR-owned component overrides.
 *
 * Wave A deliberately ships no syntax-highlighting dependency: the contract
 * is Markdown/GFM structure plus code blocks with a copy action. All labels
 * come from the ai i18n namespace (zh-CN/en parity), never hardcoded text.
 */

import { useState } from "react";
import { Button, Tooltip } from "antd";
import { CopyOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import remarkGfm from "remark-gfm";
import type { ExtraProps } from "react-markdown";
import {
  MarkdownTextPrimitive,
  type CodeHeaderProps,
  type SyntaxHighlighterProps,
} from "@assistant-ui/react-markdown";
import type { TextMessagePartProps } from "@assistant-ui/react";

type PreCodeProps = React.ComponentPropsWithoutRef<"pre"> & ExtraProps;

/** Copies plain text with the async Clipboard API when available and falls
 * back to a temporary-textarea selection copy (older browsers, denied
 * clipboard permission, or non-trusted programmatic clicks). */
async function copyTextToClipboard(text: string): Promise<void> {
  if (
    typeof navigator !== "undefined" &&
    typeof navigator.clipboard?.writeText === "function"
  ) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Fall through to the selection-based copy below.
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

function MarkdownPre({ node, ...rest }: PreCodeProps) {
  void node;
  return <pre {...rest} className="ai-markdown-pre" />;
}

function MarkdownInlineCode({ node, ...rest }: PreCodeProps) {
  void node;
  return <code {...rest} className="ai-markdown-code-inline" />;
}

/** Code block header: language label + copy action (i18n labels only). */
function MarkdownCodeHeader({ language, code }: CodeHeaderProps) {
  const { t } = useTranslation("ai");
  const [copied, setCopied] = useState(false);
  const label =
    typeof language === "string" && language.trim() !== ""
      ? language
      : t("assistant.code.plain");
  return (
    <div className="ai-code-header">
      <span className="ai-code-lang">{label}</span>
      <Tooltip title={copied ? t("assistant.code.copied") : t("assistant.code.copy")} trigger={["hover", "focus"]}>
        <Button
          type="text"
          size="small"
          icon={<CopyOutlined aria-hidden="true" />}
          className="ai-code-copy"
          data-testid="ai-code-copy"
          aria-label={t("assistant.code.copy")}
          onClick={() => {
            void copyTextToClipboard(code).then(() => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            });
          }}
        >
          {copied ? t("assistant.code.copied") : t("assistant.code.copy")}
        </Button>
      </Tooltip>
    </div>
  );
}

/** Plain code block body (pre/code). Wave A keeps the source text intact.
 * The block body is a plain <code> so the forced inline-code class never
 * leaks into the block (the CodeOverride wraps the original code props). */
function MarkdownSyntaxHighlighter({
  components: { Pre },
  code,
}: SyntaxHighlighterProps) {
  return (
    <Pre className="ai-markdown-pre">
      <code className="ai-markdown-code">{code}</code>
    </Pre>
  );
}

const MARKDOWN_COMPONENTS = {
  pre: MarkdownPre,
  code: MarkdownInlineCode,
  CodeHeader: MarkdownCodeHeader,
  SyntaxHighlighter: MarkdownSyntaxHighlighter,
} as const;

/** assistant-ui message-part text renderer: Markdown + GFM, no streaming
 * animation (DLR Wave A remains single-shot non-streaming). */
export function AssistantMarkdownText(props: TextMessagePartProps) {
  void props;
  return (
    <MarkdownTextPrimitive
      className="ai-markdown"
      remarkPlugins={[remarkGfm]}
      smooth={false}
      components={MARKDOWN_COMPONENTS}
    />
  );
}
