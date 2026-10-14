# Ephemeral AI attachment resources

Resources are separate from Managed Input, the business database and conversation history. Every API requires Adapter edit permission. Ownership also includes the authenticated Principal, Adapter and page UUID scope: an opaque locator never authorizes cross-account, cross-Adapter or cross-page reads.

| API | Contract |
|---|---|
| `POST /api/adapters/{id}/ai/attachments` | `{scope_id, attachment}`; existing MIME/signature/ZIP/6MiB checks; returns opaque ref, original hash, representation revision, directory and coverage |
| `GET /api/adapters/{id}/ai/attachments/{ref}?scope_id=…` | Public manifest without extracted content, binary bodies, internal URLs or paths |
| `DELETE /api/adapters/{id}/ai/attachments/{ref}?scope_id=…` | Idempotent close, actual conversion cancellation and cleanup after the last request pin |
| `DELETE /api/adapters/{id}/ai/attachments/scopes/{scope_id}` | Close the scope and cancel active conversion; a late upload cannot reopen the same generation |
| `GET /api/adapters/{id}/ai/attachment-capabilities` | Preparation and selected-model capabilities for Adapter editors; the legacy global route remains admin-only |

Disconnecting or cancelling preparation signals the converter and waits for its acknowledgement; the service must actually terminate the process. Errors use stable codes and never echo base64 or arbitrary upstream text. A failed file, invalid locator or cancellation leaves a new legal scope usable.

Defaults are a 30-minute TTL, 8 files and 48MiB original bytes per scope, 512MiB total storage and 64MiB derivatives per file. Originals, derivatives, directory metadata and read caches all consume capacity. There are at most 2 active conversions, 4 active-plus-waiting operations, 32 cached reads and 128 scopes. Assist pins only its frozen refs/hash/revision, rechecks source hashes and permissions on reads, and applies count/original-byte limits to mixed inline and prepared files. Closing blocks further reads immediately; TTL cleanup preserves active pins.

The browser prepares files serially within one page scope, retaining at most 8 active or queued jobs. Removing a queued file immediately removes its job; closing the scope clears waiting jobs. Cancelling active work still requires server acknowledgement that the process has terminated. Work from another page or an incomplete cancellation can return a retryable busy state. Retry/Regenerate uses the refs/hash/revision frozen at send time, never later composer files. pagehide closes the current scope; a BFCache restore creates a new page scope and clears the old temporary conversation, while an ordinary pageshow preserves the conversation.

Settings are `DLR_AI_ATTACHMENT_ROOT`, `DLR_AI_ATTACHMENT_TTL_SECONDS`, `DLR_AI_ATTACHMENT_STORE_MAX_BYTES`, `DLR_AI_ATTACHMENT_DERIVED_MAX_BYTES` and `DLR_AI_ATTACHMENT_PREPARE_TIMEOUT_SECONDS`, with bounded values in configuration. Directories use mode700 and files600; user filenames never become disk paths. A single Control owner lock prevents concurrent orphan cleanup. Restart removes only this feature's marked orphan directories; lost references expire and require reattachment.

Phase one exposes preparation and conversion with `prepared_attachments_enabled=false`; refs Assist returns `ai_attachment_reader_unavailable`, and legacy inline requests remain usable. Phase two enables the scoped reader, then phase three selects the preparation UI from capabilities. Rollback cancels/releases these resources and preserves business volumes and authentication keys. See [CPU conversion components](issue202-document-conversion.md) for fixed dependencies and actual component validation boundaries.
