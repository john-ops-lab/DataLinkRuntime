# Portable adapter and template packages

Issue #142 adds ZIP import/export for adapters and templates, and saving a saved adapter as a reusable gallery template. Python, JavaScript, Java, Task and Webhook are supported.

Use the adapter menu to export or save as a template (owner/admin only). A preview captures the latest saved code, parameters, input selection and trigger settings together. Unsaved editor or trigger/input edits are explicitly excluded. JSON and managed files are opt-in. Marking a parameter as pending removes its value; no executable placeholder is inserted. Review code, ordinary parameters and inputs for customer secrets before exporting or sharing.

Adapter import previews the package and creates a new, independently owned adapter with its own first version. Duplicate names require renaming. Task import explicitly selects a compatible target Worker and uses the normal first-save gate; a fixed offline Worker allows saving but does not imply run readiness. Schedules are disabled without replay cursors. Webhooks are stopped with a new endpoint identity. Reconfigure target credentials, addresses, inputs and Worker before running.

Saving as a template defaults parameter values to empty, preserves pending parameter names, and excludes production inputs. Choose any existing business category or Other. User templates live alongside built-ins in the same gallery; there is no personal-template area. Single-language adapters retain only their actual language. Only the creator and administrators can edit/delete user templates; built-ins stay read-only. Version checks prevent lost edits. Existing adapter copies remain independent of subsequent template edits/deletion and source-adapter deletion.

Import/export is available for both built-in and user templates. Unknown categories map to Other. Imported templates always become user content; external provenance/license statements confer no system or verification status. Reviewed example files remain examples, never production Managed Input bindings, and work even when managed files are disabled. They can be inspected in the package preview.

Format v1 contains readable `manifest.json`, separate source under `code/`, optional adapter input bytes under `input/`, and optional examples under `examples/`. The object type and version inside the manifest are authoritative. Only dependency declarations and instructions are included; installed environments, JARs, caches, source IDs/defaults and dependency binaries stay in the target environment (including #141).

Limits: 16 MiB ZIP, 32 MiB expanded, 32 entries, 9 input files and 9 example files, 48 MiB reviewed JSON. Body reading has a 30-second budget and parsing a 5-second budget. Only single-disk, non-ZIP64 Stored/Deflate archives are supported. Directory preflight rejects excessive records before allocation. Traversal, links, encryption, duplicate paths/JSON keys/languages, undeclared content, unknown fields and incompatible formats are rejected. Nothing is extracted to package-specified server paths or executed.

Managed input import reuses target feature, file, quota, free-space and retention checks, assigning new object identities and target-default retention. An outer transaction contains existing services' SAVEPOINT commits, so failures roll back adapters, versions, bindings and capacity and remove the newly written bytes. Preview cancellation leaves no persistent object.

`0036_portable` adds `user_templates` and adapter configuration reminders after `0035_builtin_packages`. Apply migrations before starting the new Control. User content is separate from shipped catalog files. Downgrading this migration removes the new template/reminder data; back it up before a production rollback.

The complete schema is in OpenAPI and `backend/src/dlr/control/schemas/portable.py`. See the [Chinese implementation contract](portable-packages.md) for endpoint and transaction details.
