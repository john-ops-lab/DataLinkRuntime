# CPU conversion services for temporary AI attachments

These services generate temporary representations only. They do not execute Adapters, modify business InputRevision data, call Providers, or give Control a Docker socket. Original bytes and SHA-256 remain unchanged.

## Pinned components

- Docling CPU image `docling-serve-cpu:v1.37.0@sha256:6eda9c524992147c1b0b06a1a9be4894bd68b1f43c86d92c32f996c4c60d18a3`, containing Docling 2.138.0, core 2.101.1 and parse 7.22.3. Each request runs the SDK in a separate supervised process, not Serve's Local thread pool.
- `gotenberg:8.37.0@sha256:f29984bd1e226bf1b93ba90af06000afa8b315853e99d27b9aaa41b93f15c769`, supplying LibreOffice 26.8.0.3 and Noto CJK. The adapter directly supervises headless LibreOffice; it does not use Gotenberg's HTTP conversion wrapper.
- openpyxl 3.1.5, et-xmlfile 2.0.0, xlrd 2.0.2 and pypdf 6.16.1 for exact source metadata, PDF catalogues and isolated single-page PDF export.

The image preloads layout, TableFormer and OCR models (about 871MiB). Runtime downloads are disabled. Chinese OCR explicitly uses CPU RapidOCR Torch with `lang=["ch"]` and two threads; the default ONNX preset was not a working Chinese configuration in this snapshot. Preserve upstream code, font and model licenses when distributing images: MIT/BSD, LibreOffice MPL/LGPL, Noto OFL, and separate Apache-2.0/MIT/CDLA-Permissive-2.0 model licenses.

## Deployment and isolation

```bash
docker compose -f docker-compose.yml -f docker-compose.ai-documents.yml up -d --build
```

The overlay sets `DLR_AI_DOCUMENT_CONVERTER_URL=http://ai-documents:5001` on Control. Both services use only an internal network, publish no host ports, and mount no business volumes or host directories. Each admits one operation at a time. Docling uses 2 CPU/3GiB/256 PID; Office uses 1 CPU/768MiB/128 PID. Swap is disabled, root filesystems are read-only, temporary storage is bounded tmpfs, and all capabilities are dropped.

Each actual child denies IPv4/IPv6 socket creation through seccomp. Unix sockets needed by Office remain available. Office uses a fresh profile with highest macro security and macros disabled. OOXML containing macros or external relationships skips rendering and reports limited coverage; source metadata remains readable. XLS cannot fetch external resources either.

Private endpoints are prepare, read-page, read-sheet and `DELETE /v1/jobs/{job_id}`. A deadline or cancellation kills and waits for the whole process group and removes temporary files before acknowledging termination. Compose `init: true` reaps adopted descendants; the supervisor verifies that the group no longer exists within 2.25 seconds, otherwise returning `ai_attachment_cancel_failed` without a successful acknowledgement. The Docling supervisor forwards the same job ID, remaining deadline and cancellation to Office. Transport failure also requires a cancellation acknowledgement. Both internal clients reject redirects and expose only fixed safe error codes.

Limits: 6MiB source, 64MiB derivatives, 96MiB private wire, 200 pages, 16 million pixels and 10MiB per media asset, 60 seconds per operation and 200 cells per range. Chinese OCR exceeded a 2GiB limit in an actual test and succeeded at 3GiB; 2GiB is not a verified deployment budget.

## Representations and verification

Preparation gives a catalogue and at most the first page. Later reads preserve original 1-based ordinals, including blank pages. Native single-page PDF export is also isolated. Sheet mapping comes from real PDF outlines; missing mappings remain unknown. Hidden sheets and content outside print areas are not claimed as rendered.

XLSX returns original formulas and original cached values. Missing caches remain null/missing; LibreOffice recalculation belongs only to derived views. xlrd cannot expose BIFF formula source, which is marked unavailable. PDF table structures are auxiliary and remain partial until checked against real page pixels. Public manifests contain identifiers, hashes, dimensions and coverage, never base64, internal URLs or host paths.

The 2026-10-14 same-sample evaluation covered a Chinese digital PDF with a real colour chart, blank second page and third-page text; an image-only scan; a chart/formula/format/merge/print-area/hidden-sheet XLSX; and genuine BIFF XLS. Docling alone omitted workbook chart pixels and failed XLS. Supervised LibreOffice matched Gotenberg HTTP output in chart-page pixels, per-page text and outline destinations (4 XLSX pages, 3 XLS pages).

Ten actual client-to-service checks passed for OCR, page identity, PNG/single-page PDF assets, sheet mapping and original caches. Actual API cancellation acknowledged Docling/Office process cleanup in approximately 51/45ms, and subsequent valid uploads recovered. Observed service memory peaks were about 2.23GiB/125MiB with no OOM. Serve Local instead kept processing for 19.42 seconds after a 0.51-second client timeout.

These checks do not replace real Chrome, the selected Vision model, Candidate/Diff/Apply/Save/Run or final user acceptance. Source files, hashes, independent oracles and detailed runtime receipts stay in private evaluation storage.

## Rollback

Stop new attachment operations, cancel active jobs and release this feature's temporary assets. Remove the overlay and converter URL before reverting the app/services. Never remove business database or Managed Input volumes, tokens, Master Key or historical logs.
