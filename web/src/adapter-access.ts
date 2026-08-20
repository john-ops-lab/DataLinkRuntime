import type {
  AccountPrincipal,
  Adapter,
  AdapterAccessLevel,
} from "./types";

/**
 * Resolve the UI capability from server metadata, with a conservative
 * compatibility fallback for older API fixtures. The backend remains the
 * authority for every operation.
 */
export function adapterAccessLevel(
  adapter: Adapter,
  principal?: AccountPrincipal,
): AdapterAccessLevel {
  if (
    adapter.access_level === "admin" ||
    adapter.access_level === "owner" ||
    adapter.access_level === "edit" ||
    adapter.access_level === "read"
  ) {
    return adapter.access_level;
  }
  if (principal === undefined || principal.role === "admin") {
    return "admin";
  }
  return adapter.owner_user_id === principal.id ? "owner" : "read";
}

export function canEditAdapter(level: AdapterAccessLevel): boolean {
  return level !== "read";
}

export function canManageAdapter(level: AdapterAccessLevel): boolean {
  return level === "admin" || level === "owner";
}
