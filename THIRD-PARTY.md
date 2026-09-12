# Third-party components

The application license does not replace dependency licenses. Pinned runtime
packages identify BSD-family licenses (Flask, ReportLab, Click, ItsDangerous,
Jinja2, MarkupSafe, Werkzeug), MIT (Gunicorn, Blinker, charset-normalizer),
Unlicense (filelock), Apache-2.0 OR BSD-2-Clause (packaging), MIT-CMU (Pillow),
and BSD-3-Clause (Windows-only colorama). Keep the notices shipped inside their
installed distributions when distributing an image or vendored dependencies.
The test-only pypdf dependency uses BSD-3-Clause.

No font binaries are bundled. If adding Inter or Barlow Condensed, include the
font's original SIL Open Font License and copyright notice as explained in
static/fonts/README.md. The Python base image includes additional system packages
with their own notices in the image. Do not represent this file as a complete
legal audit of upstream ownership.
