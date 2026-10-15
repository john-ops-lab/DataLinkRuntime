import { useEffect, useMemo, useSyncExternalStore } from "react";
import { PreparedAttachmentSession, type PreparedAttachmentTransport } from "../prepared-attachment-session";

/** Resource generations may renew without resetting the panel's conversation
 * or its composer. Account/Adapter/page changes still replace the owner. */
export function usePreparedAttachmentSession(adapterId: number, ownerKey: string, transport: PreparedAttachmentTransport) {
  const session = useMemo(() => new PreparedAttachmentSession(
    adapterId, crypto.randomUUID(), transport, ownerKey,
  ), [adapterId, ownerKey, transport]);
  const states = useSyncExternalStore(session.subscribe, session.snapshot);
  useEffect(() => session.mount(), [session]);
  return { session, states };
}
