# Template usage

[简体中文](recipe-usage-security.md)

## Find and copy

Template Gallery opens on All. Search across scenarios or filter by theme, vendor, type, protocol, and language. Each template offers its supported languages; select a language to read its code.

Choose a language, enter a name, and copy the template to open the new Adapter editor. The code starts as an unsaved draft. Check or edit it and click Save to create the first version. The copy is independent; later template updates do not change it.

## Configure and run

1. Edit the configuration section at the top of the code. Most fixed tasks need no execution input.
2. Create and bind credentials as described by the code comments. Binding names must match the keys read by the code. Store passwords, Tokens, and private keys in credentials.
3. Select a compatible Worker and prepare required dependencies through Adapter dependency settings.
4. Save the code, then run it through the ordinary Adapter workflow and inspect the result.

Comments beside remote-write settings explain their effect. Templates that support preview/sync explain mode selection in the code configuration section instead of showing mode labels in the gallery.

## When input is needed

File processing, Webhooks, and tasks with per-run parameters provide input examples. Use them to configure files or request data. For other tasks, edit the code configuration section; where supported, execution input can override settings for debugging.

CSV can accept text directly; Excel needs an execution-input file. Disabling Managed Input Store does not block browsing or copying, but file processing still needs an input path supported by the deployment. Result examples help explain what the template produces.

## Further integration

Writing to a CMDB requires a target implementing [dlr-cmdb-upsert/v1 (Simplified Chinese)](cmdb-upsert-v1.md). That document is for integration developers; use the selected template's code comments for ordinary configuration.
