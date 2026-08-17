import { currentSystemLocale, i18n } from "./i18n";
import type { DefaultPackageSourceInfo, PackageSource, SystemLocale } from "./types";

export type PackageSourceKind = PackageSource["kind"];

const PRESET_LABEL_KEYS: Record<string, string> = {
  "pypi.aliyun": "packageSources.presets.pypi.aliyun",
  "pypi.official": "packageSources.presets.pypi.official",
  "npm.npmmirror": "packageSources.presets.npm.npmmirror",
  "npm.official": "packageSources.presets.npm.official",
  "maven.aliyun": "packageSources.presets.maven.aliyun",
  "maven.central": "packageSources.presets.maven.central",
};

const KIND_LABEL_KEYS: Record<PackageSourceKind, string> = {
  pypi: "packageSources.kinds.pypi",
  npm: "packageSources.kinds.npm",
  maven: "packageSources.kinds.maven",
};

export function packageSourceKindLabel(
  kind: PackageSourceKind,
  locale: SystemLocale = currentSystemLocale(),
): string {
  return i18n.getFixedT(locale, "settings")(KIND_LABEL_KEYS[kind], {
    defaultValue: kind,
  });
}

export function packageSourcePresetLabel(
  source: Pick<PackageSource, "name" | "preset_id"> | Pick<DefaultPackageSourceInfo, "name" | "preset_id">,
  locale: SystemLocale = currentSystemLocale(),
): string {
  const key =
    typeof source.preset_id === "string" ? PRESET_LABEL_KEYS[source.preset_id] : undefined;
  if (key === undefined) {
    return source.name;
  }
  return i18n.getFixedT(locale, "settings")(key, { defaultValue: source.name });
}
