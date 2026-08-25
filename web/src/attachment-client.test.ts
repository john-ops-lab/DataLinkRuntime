/**
 * M5.7 Wave B3: unit regressions for the official AttachmentAdapter factory
 * and the send guards (PR #85 review: rejected rows must never produce a
 * wire body; accept must follow the current capability table).
 */

import { describe, expect, it } from "vitest";

import type { PendingAttachment } from "@assistant-ui/react";

import {
  createDlrAttachmentAdapter,
  DEFAULT_ATTACHMENT_LIMITS,
  firstRejectedRowMessage,
  rejectedRowMessage,
  type AttachmentRowStatus,
  type DlrAttachmentAdapterOptions,
} from "./attachment-client";

const translate = (key: string): string => key;

function adapterOptions(overrides: Partial<DlrAttachmentAdapterOptions> = {}): DlrAttachmentAdapterOptions {
  return {
    limits: () => DEFAULT_ATTACHMENT_LIMITS,
    composerAttachments: () => [],
    supportedContentTypes: () => ["text/plain", "application/json"],
    translate,
    wireCache: () => new WeakMap(),
    ...overrides,
  };
}

function makeFile(name: string, type: string, content: string | Uint8Array = "x"): File {
  return new File([content], name, { type });
}

describe("createDlrAttachmentAdapter", () => {
  it("keeps the fallback total aligned with eight maximum-size attachments", () => {
    expect(DEFAULT_ATTACHMENT_LIMITS.max_total_bytes).toBe(48 * 1024 * 1024);
  });

  it("exposes accept as a getter that follows the current capability table", () => {
    const adapter = createDlrAttachmentAdapter(
      adapterOptions({ supportedContentTypes: () => ["text/plain", "application/json"] }),
    );
    expect(adapter.accept).toBe("text/plain,application/json");
    // A later narrowing of the fetched table is picked up immediately.
    const narrowed = createDlrAttachmentAdapter(
      adapterOptions({ supportedContentTypes: () => ["text/plain"] }),
    );
    expect(narrowed.accept).toBe("text/plain");
  });

  it("add() returns a visible error row for a rejected file, and send() refuses to resolve it", async () => {
    const adapter = createDlrAttachmentAdapter(adapterOptions());
    const oversized = makeFile(
      "big.txt",
      "text/plain",
      new Uint8Array(DEFAULT_ATTACHMENT_LIMITS.max_file_bytes + 1),
    );
    // Our adapter returns a plain Promise (never an AsyncGenerator).
    const row = (await adapter.add({ file: oversized })) as PendingAttachment;
    expect(row.status.type).toBe("incomplete");
    // The runtime's own send loop resolves every pending row, so the adapter
    // must refuse error rows instead of base64-encoding them.
    await expect(adapter.send(row)).rejects.toThrow("assistant.attachments.error.tooLarge");
  });

  it("send() refuses an incomplete row even when it carries a file body", async () => {
    const adapter = createDlrAttachmentAdapter(adapterOptions());
    const row: PendingAttachment = {
      id: "row-1",
      type: "document",
      name: "notes.txt",
      contentType: "text/plain",
      file: makeFile("notes.txt", "text/plain", "body"),
      status: { type: "incomplete", reason: "error", message: "rejected-message" },
    };
    await expect(adapter.send(row)).rejects.toThrow("rejected-message");
    // Fallback message when the row carries none.
    const bare: PendingAttachment = { ...row, status: { type: "incomplete", reason: "error" } };
    await expect(adapter.send(bare)).rejects.toThrow("assistant.attachments.error.rejected");
  });

  it("send() resolves a valid pending row into a complete attachment", async () => {
    const adapter = createDlrAttachmentAdapter(adapterOptions());
    const row = (await adapter.add({ file: makeFile("notes.txt", "text/plain", "hello") })) as PendingAttachment;
    const complete = await adapter.send(row);
    expect(complete.status).toEqual({ type: "complete" });
    expect(complete.name).toBe("notes.txt");
    expect(complete.file?.name).toBe("notes.txt");
    expect(complete.content).toHaveLength(1);
  });
});

describe("rejectedRowMessage / firstRejectedRowMessage", () => {
  it("guards incomplete rows only and prefers the row's own message", () => {
    expect(rejectedRowMessage({ type: "requires-action" }, "fallback")).toBeNull();
    expect(
      rejectedRowMessage({ type: "running", reason: "uploading", progress: 0 } as AttachmentRowStatus, "fallback"),
    ).toBeNull();
    expect(rejectedRowMessage({ type: "complete" }, "fallback")).toBeNull();
    expect(rejectedRowMessage({ type: "incomplete", message: "own" }, "fallback")).toBe("own");
    expect(rejectedRowMessage({ type: "incomplete" }, "fallback")).toBe("fallback");
  });

  it("returns the first rejected row message of a composer snapshot", () => {
    expect(
      firstRejectedRowMessage(
        [
          { status: { type: "requires-action" } },
          { status: { type: "incomplete", message: "bad" } },
        ],
        "fallback",
      ),
    ).toBe("bad");
    expect(firstRejectedRowMessage([{ status: { type: "complete" } }], "fallback")).toBeNull();
    expect(firstRejectedRowMessage([], "fallback")).toBeNull();
  });
});
