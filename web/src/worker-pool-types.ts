import type { AdapterLanguage } from "./types";

export interface FixedRoute {
  schema_version: 1;
  mode: "fixed";
  worker_id: number;
  failover: "none";
}

export interface PoolRule {
  selector: Record<string, string>;
  environment_class: string;
  placement_scope: string;
}

export interface PoolRoute extends PoolRule {
  schema_version: 1;
  mode: "pool";
  pool_id: number;
  pool_revision: number;
  failover: "compatible";
}

export type RuntimeRoute = FixedRoute | PoolRoute;
export type RouteSource = "adapter" | "activation" | "original" | "repair" | "current-config" | "dependency-check" | "legacy";

export interface RoutePreview {
  route: RuntimeRoute | null;
  source: RouteSource;
}

export interface RouteSnapshot extends RoutePreview {
  route: RuntimeRoute;
  required_language: AdapterLanguage;
  environment: WorkerEnvironment | "unknown";
  wait_policy: { placement_wait_timeout_seconds: number } | "w1-fixed";
}

export interface WorkerEnvironment {
  name: string;
  image_digest: string;
  architecture: "amd64" | "arm64";
  toolchains: Partial<Record<AdapterLanguage, string>>;
  builtin_materials: string[];
}

export interface PoolRevision extends PoolRule {
  revision: number;
  created_at: string;
}

export interface WorkerPool {
  id: number;
  name: string;
  enabled: boolean;
  version: number;
  revision: PoolRevision;
}

export interface PoolMember {
  worker_id: number;
  worker_identity_id: string;
  enabled: boolean;
  labels: Record<string, string>;
  placement_scopes: string[];
  credential_ids: number[];
  material_scopes: string[];
}

export interface WorkerPoolDetail extends WorkerPool {
  revisions: PoolRevision[];
  members: PoolMember[];
  audit: {
    id: number;
    action: "create" | "update" | "member" | "delete_revision";
    actor_kind: string;
    actor_user_id: number | null;
    version: number;
    created_at: string;
  }[];
}

export interface RuntimeRoutePatch {
  runtime_worker_id?: number | null;
  route?: RuntimeRoute | null;
  expected_route_version?: number;
}
