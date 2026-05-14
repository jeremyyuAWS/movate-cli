#!/usr/bin/env bash
#
# Render any markdown file to a PDF with Movate-themed styling.
#
# Use case: turning the Deva walkthrough docs (or anything else
# under docs/) into a forwardable PDF — useful for stakeholders who
# want a leave-behind reference instead of a github URL.
#
# Pipeline: markdown -> HTML (via Python's `markdown` lib, no system
# deps beyond Python 3 + uv) -> PDF (via Chrome's headless print
# mode, no extra install required on macOS if Chrome is installed).
#
# Usage:
#   ./build-md-pdf.sh <input.md>                    # output: <input>.pdf
#   ./build-md-pdf.sh <input.md> <output.pdf>       # explicit output path
#   ./build-md-pdf.sh ~/.movate/deva-pillar-walkthrough.md
#
# After the bearer-baked Deva-ready markdown has been generated via
# the sed pipeline at ~/.movate/, run this against it to produce a
# matching PDF for forwarding. The PDF is chmod 600 by default since
# the input usually has live credentials.
#
# Heads-up: requires Google Chrome installed at the default macOS
# location. If your install is elsewhere or you're on Linux, edit
# CHROME below.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <input.md> [output.pdf]" >&2
    exit 2
fi

readonly INPUT="$1"
readonly OUTPUT="${2:-${INPUT%.md}.pdf}"

if [[ ! -f "$INPUT" ]]; then
    echo "error: input file not found: $INPUT" >&2
    exit 2
fi

# Chrome path on macOS. Override via CHROME env var on other OSes:
#   CHROME=/usr/bin/google-chrome ./build-md-pdf.sh ...
readonly CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

if [[ ! -x "$CHROME" ]]; then
    echo "error: Chrome not found at: $CHROME" >&2
    echo "       set CHROME=... env var if Chrome is elsewhere, or" >&2
    echo "       install Chrome from https://www.google.com/chrome/" >&2
    exit 2
fi

# Stage the intermediate HTML in /tmp (cleaned at exit).
readonly HTML="$(mktemp -t mdpdf).html"
trap 'rm -f "$HTML"' EXIT

# Render markdown -> styled HTML. The `markdown` package is tiny;
# uv pulls it into a throwaway env for this run only.
INPUT_PATH="$INPUT" HTML_PATH="$HTML" uv run --quiet --with markdown python3 <<'PYEOF'
import os
import pathlib

import markdown

src = pathlib.Path(os.environ["INPUT_PATH"]).read_text()
out_path = pathlib.Path(os.environ["HTML_PATH"])

body = markdown.markdown(
    src,
    extensions=["fenced_code", "tables", "toc", "sane_lists", "attr_list"],
)

# Movate-themed styling. Tuned for readability at PDF size:
#   * Body text 11pt, line-height 1.5
#   * Code blocks dark background, 9pt Consolas, scrollable horizontally
#     (we use overflow-wrap to wrap long curls instead — PDFs don't scroll)
#   * Headers use the same blue as the architecture deck (#004a8f)
#   * Blockquotes get a left accent for "heads up" callouts
#   * Tables collapsed borders, light header background
html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MDK + Mova iO — Endpoint Walkthrough</title>
<style>
  @page {{
    size: letter;
    margin: 0.6in 0.7in;
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: #1a1a1a;
    font-size: 10.5pt;
    line-height: 1.5;
    max-width: 100%;
  }}
  h1 {{
    color: #004a8f;
    border-bottom: 2px solid #004a8f;
    padding-bottom: 6px;
    page-break-after: avoid;
    font-size: 22pt;
  }}
  h2 {{
    color: #002e5c;
    border-bottom: 1px solid #ddd;
    padding-bottom: 4px;
    margin-top: 1.8em;
    page-break-after: avoid;
    font-size: 16pt;
  }}
  h3 {{
    color: #002e5c;
    margin-top: 1.4em;
    page-break-after: avoid;
    font-size: 13pt;
  }}
  h4 {{
    color: #444;
    margin-top: 1em;
    font-size: 11.5pt;
  }}
  code {{
    background: #f4f4f4;
    padding: 1.5px 4px;
    border-radius: 3px;
    font-family: "Consolas", "Menlo", monospace;
    font-size: 88%;
    color: #c41a16;
  }}
  pre {{
    background: #1e1e1e;
    color: #dcdcdc;
    padding: 10px 14px;
    border-radius: 5px;
    font-size: 9pt;
    line-height: 1.4;
    overflow-wrap: break-word;
    word-wrap: break-word;
    page-break-inside: avoid;
  }}
  pre code {{
    background: none;
    padding: 0;
    color: inherit;
    font-size: inherit;
    white-space: pre-wrap;
  }}
  table {{
    border-collapse: collapse;
    margin: 1em 0;
    width: 100%;
    font-size: 10pt;
  }}
  th, td {{
    border: 1px solid #ccc;
    padding: 6px 10px;
    text-align: left;
    vertical-align: top;
  }}
  th {{
    background: #f0f4fa;
    color: #002e5c;
    font-weight: 600;
  }}
  blockquote {{
    border-left: 4px solid #004a8f;
    background: #f7faff;
    padding: 8px 14px;
    margin: 1em 0;
    color: #333;
    page-break-inside: avoid;
  }}
  hr {{
    border: none;
    border-top: 1px solid #ddd;
    margin: 2em 0;
  }}
  a {{
    color: #004a8f;
    text-decoration: none;
  }}
  a:hover {{
    text-decoration: underline;
  }}
  ul, ol {{
    margin: 0.4em 0;
    padding-left: 1.6em;
  }}
  li {{
    margin: 0.2em 0;
  }}
  /* Don't split short content across pages awkwardly */
  p, pre, blockquote, table {{
    page-break-inside: avoid;
  }}
</style>
</head>
<body>
{body}
</body>
</html>
"""
out_path.write_text(html_doc)
PYEOF

# Render HTML -> PDF via Chrome headless. The --no-pdf-header-footer
# flag drops the default browser header/footer ("file:///tmp/..."
# at top, page number at bottom) for a clean output.
"$CHROME" \
    --headless \
    --disable-gpu \
    --no-pdf-header-footer \
    --no-sandbox \
    --print-to-pdf="$OUTPUT" \
    "file://$HTML" \
    2>/dev/null

# Lock down permissions in case the input contained credentials.
chmod 600 "$OUTPUT"

echo "wrote: $OUTPUT"
echo "open with: open '$OUTPUT'"
