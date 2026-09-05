import type { ReactNode } from "react";
import {
  ApiOutlined,
  CodeOutlined,
  FileTextOutlined,
  NodeIndexOutlined,
  SendOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";

import alibabacloud from "../assets/template-logos/alibabacloud.svg";
import tencentcloud from "../assets/template-logos/tencentcloud.svg";
import servicenow from "../assets/template-logos/servicenow.svg";
import postgresql from "../assets/template-logos/postgresql.svg";
import mysql from "../assets/template-logos/mysql.svg";
import excel from "../assets/template-logos/microsoftexcel.svg";
import s3 from "../assets/template-logos/amazons3.svg";

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
  glyph?: ReactNode;
  image?: string;
  marker: string;
  tone: string;
}

const LOGOS: Record<TemplateLogoKey, LogoDefinition> = {
  "alicloud-compute": { image: alibabacloud, marker: "Alibaba Cloud", tone: "brand" },
  "alicloud-network": { image: alibabacloud, marker: "Alibaba Cloud", tone: "brand" },
  "alicloud-data": { image: alibabacloud, marker: "Alibaba Cloud", tone: "brand" },
  "tencentcloud-compute": { image: tencentcloud, marker: "Tencent Cloud", tone: "brand" },
  "tencentcloud-network": { image: tencentcloud, marker: "Tencent Cloud", tone: "brand" },
  "tencentcloud-data": { image: tencentcloud, marker: "Tencent Cloud", tone: "brand" },
  "servicenow-cmdb": { image: servicenow, marker: "ServiceNow", tone: "brand" },
  "rest-request": { glyph: <ApiOutlined />, marker: "REST", tone: "blue" },
  "rest-pagination": { glyph: <NodeIndexOutlined />, marker: "PAGE", tone: "blue" },
  "webhook-normalize": { glyph: <ThunderboltOutlined />, marker: "HOOK", tone: "violet" },
  "file-csv": { glyph: <FileTextOutlined />, marker: "CSV", tone: "green" },
  "file-excel": { image: excel, marker: "Excel", tone: "brand" },
  "data-json": { glyph: <CodeOutlined />, marker: "JSON", tone: "amber" },
  "database-postgresql": { image: postgresql, marker: "PostgreSQL", tone: "brand" },
  "database-mysql": { image: mysql, marker: "MySQL", tone: "brand" },
  "storage-s3": { image: s3, marker: "Amazon S3", tone: "brand" },
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
      {definition.image ? (
        <img className="template-logo-brand" src={definition.image} alt="" />
      ) : <>
        <span className="template-logo-glyph">{definition.glyph}</span>
        <span className="template-logo-marker">{definition.marker}</span>
      </>}
    </span>
  );
}
