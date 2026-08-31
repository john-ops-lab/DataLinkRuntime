export const SETTINGS_CATEGORIES = [
  "general",
  "credentials",
  "package-sources",
  "ai-model",
  "knowledge-sources",
  "managed-input",
] as const;

export type SettingsCategory = (typeof SETTINGS_CATEGORIES)[number];

export function isSettingsCategory(value: string | undefined): value is SettingsCategory {
  return value !== undefined && (SETTINGS_CATEGORIES as readonly string[]).includes(value);
}

export function settingsPath(category: SettingsCategory = "general"): string {
  return `/settings/${category}`;
}

export function settingsCategoryFromPath(pathname: string): SettingsCategory | null {
  const match = /^\/settings(?:\/([^/]+))?\/?$/.exec(pathname);
  if (match === null) {
    return null;
  }
  return isSettingsCategory(match[1]) ? match[1] : "general";
}
