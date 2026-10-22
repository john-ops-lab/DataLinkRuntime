"""One memory-only inline parse; fixed stdin/stdout protocol, no Control state."""

from __future__ import annotations

import json
import math
import os
import sys


def main() -> None:
    # Limits are installed before importing document parsers. Linux is the
    # supported server platform; macOS keeps the same wall-clock supervision.
    if os.name == "posix":
        import resource

        seconds = max(1, math.ceil(float(sys.argv[3])) + 1)
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        if sys.platform == "linux":
            memory = int(sys.argv[4])
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

    from dlr.control.ai import attachments

    try:
        category, char_budget = sys.argv[1], int(sys.argv[2])
        parsers = {
            attachments.PDF_CATEGORY: attachments._parse_pdf,
            attachments.DOCX_CATEGORY: attachments._parse_docx,
            attachments.XLS_CATEGORY: attachments._parse_xls,
            attachments.XLSX_CATEGORY: attachments._parse_xlsx,
            attachments.TEXT_CATEGORY: attachments._parse_text,
            attachments.CODE_CATEGORY: attachments._parse_text,
        }
        if category not in parsers or not 0 <= char_budget <= attachments.MAX_PARSED_CHARS_PER_FILE:
            raise attachments.AttachmentError("ai_attachment_invalid")
        source = sys.stdin.buffer.read(attachments.MAX_FILE_BYTES + 1)
        if len(source) > attachments.MAX_FILE_BYTES:
            raise attachments.AttachmentError("ai_attachment_too_large")
        text, truncated = parsers[category](source, char_budget)
        output: dict[str, object] = {"text": text, "truncated": truncated}
    except attachments.AttachmentError as error:
        output = {"error": error.code}
    except Exception:  # noqa: BLE001 - no parser exception or source leaves this worker
        output = {"error": "ai_attachment_parse_failed"}
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False, allow_nan=False).encode("utf-8"))


if __name__ == "__main__":
    main()
