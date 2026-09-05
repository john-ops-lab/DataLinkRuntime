import type { ReactNode } from "react";
import {
  ApiOutlined,
  ApartmentOutlined,
  CloudOutlined,
  CodeOutlined,
  DatabaseOutlined,
  DeploymentUnitOutlined,
  FileExcelOutlined,
  FileTextOutlined,
  GlobalOutlined,
  HddOutlined,
  LinkOutlined,
  NodeIndexOutlined,
  SafetyCertificateOutlined,
  SendOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";

export const TEMPLATE_LOGO_KEYS = [
  "alicloud-compute",
  "alicloud-network",
  "alicloud-data",
  "tencentcloud-compute",
  "tencentcloud-network",
  "tencentcloud-data",
  "servicenow-cmdb",
  "rest-request",
  "rest-pagination",
  "webhook-normalize",
  "file-csv",
  "file-excel",
  "data-json",
  "database-postgresql",
  "database-mysql",
  "storage-s3",
  "transfer-sftp",
] as const;

export type TemplateLogoKey = (typeof TEMPLATE_LOGO_KEYS)[number];

interface LogoDefinition {
  glyph: ReactNode;
  marker: string;
  tone: string;
}

const LOGOS: Record<TemplateLogoKey, LogoDefinition> = {
  "alicloud-compute": { glyph: <CloudOutlined />, marker: "ALI", tone: "orange" },
  "alicloud-network": { glyph: <GlobalOutlined />, marker: "ALI", tone: "orange" },
  "alicloud-data": { glyph: <DatabaseOutlined />, marker: "ALI", tone: "orange" },
  "tencentcloud-compute": { glyph: <CloudOutlined />, marker: "TCE", tone: "cyan" },
  "tencentcloud-network": { glyph: <ApartmentOutlined />, marker: "TCE", tone: "cyan" },
  "tencentcloud-data": { glyph: <DatabaseOutlined />, marker: "TCE", tone: "cyan" },
  "servicenow-cmdb": { glyph: <DeploymentUnitOutlined />, marker: "CMDB", tone: "teal" },
  "rest-request": { glyph: <ApiOutlined />, marker: "REST", tone: "blue" },
  "rest-pagination": { glyph: <NodeIndexOutlined />, marker: "PAGE", tone: "blue" },
  "webhook-normalize": { glyph: <ThunderboltOutlined />, marker: "HOOK", tone: "violet" },
  "file-csv": { glyph: <FileTextOutlined />, marker: "CSV", tone: "green" },
  "file-excel": { glyph: <FileExcelOutlined />, marker: "XLS", tone: "emerald" },
  "data-json": { glyph: <CodeOutlined />, marker: "JSON", tone: "amber" },
  "database-postgresql": { glyph: <DatabaseOutlined />, marker: "PG", tone: "indigo" },
  "database-mysql": { glyph: <DatabaseOutlined />, marker: "SQL", tone: "sky" },
  "storage-s3": { glyph: <HddOutlined />, marker: "S3", tone: "orange" },
  "transfer-sftp": { glyph: <SendOutlined />, marker: "SFTP", tone: "teal" },
};

const FALLBACK: LogoDefinition = {
  glyph: <CodeOutlined />,
  marker: "DLR",
  tone: "blue",
};

export default function TemplateScenarioLogo({ logoKey }: { logoKey: string }) {
  const definition = (LOGOS as Record<string, LogoDefinition>)[logoKey] ?? FALLBACK;
  return (
    <span
      className={`template-logo-tile template-logo-tone-${definition.tone}`}
      data-logo-key={logoKey}
      aria-hidden="true"
    >
      <span className="template-logo-orbit"><LinkOutlined /></span>
      <span className="template-logo-glyph">{definition.glyph}</span>
      <span className="template-logo-marker">{definition.marker}</span>
      <span className="template-logo-shield"><SafetyCertificateOutlined /></span>
    </span>
  );
}
