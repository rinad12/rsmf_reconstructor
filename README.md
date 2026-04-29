# RSMF Reconciler & Chat Log Utility

A specialized data management utility designed to reconcile asymmetrical Relativity Short Message Format (RSMF) exports. This tool identifies and aligns messages across multiple sources to produce a unified, structured chronological transcript.

## Key Features

- **Automated Reconciliation:** Uses a deterministic matching algorithm to merge disparate chat logs.
- **Divergence Identification:** Automatically flags records present in one source but missing from another, ensuring a complete overview of the conversation history.
- **Data Integrity:** Operates in a strictly read-only mode to ensure the original source files remain unaltered.
- **Standalone Portability:** Fully self-contained Windows executable designed for offline environments and portable use.

## How to Run

### Standalone Executable (Windows)

The provided `.exe` is the recommended method for most users. It is bundled with all necessary dependencies, including the PDF rendering engine and required system libraries.

**Command Line Usage:**

```
rsmf-reconstruct.exe source_a.rsmf source_b.rsmf --output ./report_folder
```

**Drag & Drop:** You can select two `.rsmf` files in Windows Explorer and drag them simultaneously onto the `rsmf-reconstruct.exe` icon to trigger an automatic reconstruction.

**Environment Compatibility:** Tested on standard builds of Windows 10 and 11. It does not require a Python runtime, internet access, or pre-installed DLLs.

### Developer Setup (Python)

Requires Python 3.11+ and the `uv` package manager.

```bash
uv sync
uv run -m rsmf_reconstruct file_a.rsmf file_b.rsmf
```

## Technical Architecture & Data Logic

### Reconciliation Algorithm

The tool employs a **Full Outer Join** logic to align the message streams:

- **Matched Records:** If a unique message ID exists in both exports, the records are reconciled into a single verified event.
- **Discrepancy Detection:** If a record exists in Source A but is missing from Source B (or vice versa), the tool identifies the gap and flags the record's origin.
- **Metadata Preservation:** Identifies records where the `deleted: true` boolean is present in the underlying RSMF manifest and preserves this status for accurate reporting.

### Deduplication & Fingerprinting

To account for varying identifiers across different data exports, the tool utilizes a composite fingerprinting logic:

$$Fingerprint = \{Timestamp (UTC), SenderID, Hash(Body)\}$$

This prevents false duplicates caused by minor clock drift or varying export timestamps between source devices.

### Performance Profile

| Metric | Value |
|---|---|
| Time Complexity | $O(L \log L)$, where $L$ is the total message count |
| Resource Usage | < 200 MB RAM for a 50,000-message dataset |
| Binary Footprint | ~230 MB (includes bundled headless browser engine) |

## Output Formats

- **PDF Summary Report:** A fixed-layout document featuring chronological message bubbles, highlighted discrepancies, and embedded attachment metadata.
- **Reconciled RSMF:** A consolidated `.rsmf` file containing the merged manifest, suitable for ingestion into standard review platforms.
- **Extracted Media:** All media files (images, PDFs, voice notes) are extracted, deduplicated, and mapped to their respective events in the report.