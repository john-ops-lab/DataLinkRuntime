import { settingsCategoryFromPath, type SettingsCategory } from "./settings-route";

export type PrimarySection = "adapters" | "templates";

export type AppRoute =
  | { section: "adapters"; settingsCategory: null }
  | { section: "adapters"; settingsCategory: SettingsCategory }
  | { section: "templates"; scenarioSlug: null }
  | { section: "templates"; scenarioSlug: string };

const SCENARIO_SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

/** Parse only the small set of first-party routes supported by the Console. */
export function appRouteFromPath(pathname: string): AppRoute {
  if (pathname === "/templates" || pathname === "/templates/") {
    return { section: "templates", scenarioSlug: null };
  }

  const detailMatch = /^\/templates\/([^/]+)\/?$/.exec(pathname);
  if (detailMatch !== null) {
    try {
      const scenarioSlug = decodeURIComponent(detailMatch[1]);
      if (scenarioSlug.length <= 128 && SCENARIO_SLUG.test(scenarioSlug)) {
        return { section: "templates", scenarioSlug };
      }
    } catch {
      // Invalid percent encoding is treated like any other unknown route.
    }
  }

  const settingsCategory = settingsCategoryFromPath(pathname);
  if (settingsCategory !== null) {
    return { section: "adapters", settingsCategory };
  }

  return { section: "adapters", settingsCategory: null };
}

export function templatePath(scenarioSlug?: string | null): string {
  return scenarioSlug ? `/templates/${encodeURIComponent(scenarioSlug)}` : "/templates";
}

type BrowserLocationBlocker = (nextLocation: string) => boolean;

interface AcceptedBrowserEntry {
  location: string;
  state: unknown;
  index: number;
  epoch: string;
}

const HISTORY_INDEX_KEY = "__datalink_runtime_history_index_v1__";
const HISTORY_EPOCH_KEY = "__datalink_runtime_history_epoch_v1__";
const HISTORY_PAYLOAD_KEY = "__datalink_runtime_history_payload_v1__";
const NAVIGATION_API_EPOCH = "navigation-api";
const DOCUMENT_EPOCH_SEED = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
const browserLocationSubscribers = new Set<() => void>();
let browserLocationBlocker: BrowserLocationBlocker | null = null;
let acceptedBrowserEntry: AcceptedBrowserEntry | null = null;
let allowedNativeTarget: Pick<AcceptedBrowserEntry, "index" | "epoch"> | null = null;
let compensatingEntry: AcceptedBrowserEntry | null = null;
let epochSequence = 0;

function historyIndex(state: unknown): number | null {
  if (typeof state !== "object" || state === null || Array.isArray(state)) {
    return null;
  }
  const value = (state as Record<string, unknown>)[HISTORY_INDEX_KEY];
  return typeof value === "number" && Number.isSafeInteger(value) ? value : null;
}

function historyEpoch(state: unknown): string | null {
  if (typeof state !== "object" || state === null || Array.isArray(state)) {
    return null;
  }
  const value = (state as Record<string, unknown>)[HISTORY_EPOCH_KEY];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function nextHistoryEpoch(): string {
  epochSequence += 1;
  return `${DOCUMENT_EPOCH_SEED}-${epochSequence}`;
}

function stateWithHistoryIndex(
  state: unknown,
  index: number,
  epoch: string,
): Record<string, unknown> {
  if (typeof state === "object" && state !== null && !Array.isArray(state)) {
    return {
      ...(state as Record<string, unknown>),
      [HISTORY_INDEX_KEY]: index,
      [HISTORY_EPOCH_KEY]: epoch,
    };
  }
  return {
    [HISTORY_INDEX_KEY]: index,
    [HISTORY_EPOCH_KEY]: epoch,
    [HISTORY_PAYLOAD_KEY]: state ?? null,
  };
}

function navigationApiIndex(): number | null {
  const navigation = (window as unknown as {
    navigation?: { currentEntry?: { index?: unknown } };
  }).navigation;
  const index = navigation?.currentEntry?.index;
  return typeof index === "number" && Number.isSafeInteger(index) ? index : null;
}

function initialHistoryPosition(): Pick<AcceptedBrowserEntry, "index" | "epoch"> {
  const navigationIndex = navigationApiIndex();
  if (navigationIndex !== null) {
    return { index: navigationIndex, epoch: NAVIGATION_API_EPOCH };
  }
  // A fresh epoch makes this entry a safe origin without pretending that
  // history.length reveals its position when a Forward branch exists.
  return { index: 0, epoch: nextHistoryEpoch() };
}

function currentBrowserEntry(
  fallbackPosition = initialHistoryPosition(),
): AcceptedBrowserEntry {
  const navigationIndex = navigationApiIndex();
  const existingIndex = historyIndex(window.history.state);
  const existingEpoch = historyEpoch(window.history.state);
  const position = navigationIndex !== null
    ? { index: navigationIndex, epoch: NAVIGATION_API_EPOCH }
    : existingIndex !== null && existingEpoch !== null
      ? { index: existingIndex, epoch: existingEpoch }
      : fallbackPosition;

  if (
    existingIndex !== position.index
    || existingEpoch !== position.epoch
  ) {
    window.history.replaceState(
      stateWithHistoryIndex(window.history.state, position.index, position.epoch),
      "",
      browserLocationSnapshot(),
    );
  }
  return {
    location: browserLocationSnapshot(),
    state: window.history.state,
    ...position,
  };
}

function publishBrowserLocation(): void {
  const targetWasUnindexed = navigationApiIndex() === null
    && (
      historyIndex(window.history.state) === null
      || historyEpoch(window.history.state) === null
    );
  // Direction cannot be inferred for a legacy entry when the Navigation API is
  // unavailable. Start a separate epoch even when this traversal was approved
  // by backBrowserLocation(), so no later blocker can use a guessed delta.
  const fallbackPosition = targetWasUnindexed
    ? { index: 0, epoch: nextHistoryEpoch() }
    : allowedNativeTarget ?? { index: 0, epoch: nextHistoryEpoch() };
  const nextEntry = currentBrowserEntry(fallbackPosition);

  if (
    compensatingEntry !== null
    && nextEntry.index === compensatingEntry.index
    && nextEntry.epoch === compensatingEntry.epoch
    && nextEntry.location === compensatingEntry.location
  ) {
    acceptedBrowserEntry = nextEntry;
    compensatingEntry = null;
    allowedNativeTarget = null;
    return;
  }
  compensatingEntry = null;

  const isNativeNavigation = acceptedBrowserEntry !== null
    && (
      nextEntry.index !== acceptedBrowserEntry.index
      || nextEntry.epoch !== acceptedBrowserEntry.epoch
      || nextEntry.location !== acceptedBrowserEntry.location
  );
  const navigationAllowed = (
    allowedNativeTarget !== null
    && (
      nextEntry.epoch !== allowedNativeTarget.epoch
      || nextEntry.index === allowedNativeTarget.index
    )
  )
    || !isNativeNavigation
    || browserLocationBlocker === null
    || browserLocationBlocker(nextEntry.location);

  allowedNativeTarget = null;
  if (!navigationAllowed && acceptedBrowserEntry !== null) {
    if (
      targetWasUnindexed
      || nextEntry.epoch !== acceptedBrowserEntry.epoch
      || nextEntry.index === acceptedBrowserEntry.index
    ) {
      // The browser has already traversed to an entry whose direction is not
      // knowable. Restore the accepted URL/state in the current slot. Unlike a
      // push, replaceState keeps every later Forward entry intact; unlike a
      // guessed history.go(delta), it cannot strand the URL away from the UI.
      window.history.replaceState(
        acceptedBrowserEntry.state,
        "",
        acceptedBrowserEntry.location,
      );
      acceptedBrowserEntry = {
        ...acceptedBrowserEntry,
        state: window.history.state,
      };
      return;
    }
    // A native Back/Forward has already moved the address bar. Move back to the
    // accepted entry instead of pushing a replacement, which would truncate the
    // browser's Forward branch. The resulting compensation popstate is ignored.
    const delta = acceptedBrowserEntry.index - nextEntry.index;
    compensatingEntry = acceptedBrowserEntry;
    window.history.go(delta);
    return;
  }

  acceptedBrowserEntry = nextEntry;
  browserLocationSubscribers.forEach((subscriber) => subscriber());
}

export function subscribeToBrowserLocation(callback: () => void): () => void {
  if (browserLocationSubscribers.size === 0) {
    acceptedBrowserEntry = currentBrowserEntry();
    window.addEventListener("popstate", publishBrowserLocation);
  }
  browserLocationSubscribers.add(callback);
  return () => {
    browserLocationSubscribers.delete(callback);
    if (browserLocationSubscribers.size === 0) {
      window.removeEventListener("popstate", publishBrowserLocation);
      acceptedBrowserEntry = null;
      allowedNativeTarget = null;
      compensatingEntry = null;
    }
  };
}

export function browserLocationSnapshot(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}

export function notifyBrowserLocation(): void {
  // replaceState/pushState are deliberate in-app transitions. Record them as
  // accepted before publishing so the native-navigation blocker is not asked a
  // second time after the initiating control already ran its guard.
  acceptedBrowserEntry = currentBrowserEntry({
    index: acceptedBrowserEntry?.index ?? 0,
    epoch: acceptedBrowserEntry?.epoch ?? nextHistoryEpoch(),
  });
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function pushBrowserLocation(path: string, state?: unknown): void {
  const current = acceptedBrowserEntry ?? currentBrowserEntry();
  const nextIndex = current.index + 1;
  window.history.pushState(
    stateWithHistoryIndex(state ?? null, nextIndex, current.epoch),
    "",
    path,
  );
  acceptedBrowserEntry = currentBrowserEntry({
    index: nextIndex,
    epoch: current.epoch,
  });
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/** Replace the current in-app entry without changing its tracked position. */
export function replaceBrowserLocation(path: string, state?: unknown): void {
  const current = acceptedBrowserEntry ?? currentBrowserEntry();
  window.history.replaceState(
    stateWithHistoryIndex(state ?? null, current.index, current.epoch),
    "",
    path,
  );
  acceptedBrowserEntry = currentBrowserEntry({
    index: current.index,
    epoch: current.epoch,
  });
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/** Register the single Console-level guard used for native Back/Forward events. */
export function setBrowserLocationBlocker(blocker: BrowserLocationBlocker): () => void {
  browserLocationBlocker = blocker;
  return () => {
    if (browserLocationBlocker === blocker) {
      browserLocationBlocker = null;
    }
  };
}

/** Navigate back after the initiating surface has already confirmed its own guard. */
export function backBrowserLocation(): void {
  const current = acceptedBrowserEntry ?? currentBrowserEntry();
  allowedNativeTarget = {
    index: current.index - 1,
    epoch: current.epoch,
  };
  window.history.back();
}
