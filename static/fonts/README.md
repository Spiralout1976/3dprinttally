# Fonts

The stylesheet expects these WOFF2 files in this directory:

- `Inter-Variable.woff2`
- `BarlowCondensed-SemiBold.woff2`
- `BarlowCondensed-Bold.woff2`

Both families are licensed under the SIL Open Font License 1.1, which permits
redistribution. If you ship them, **include their `OFL.txt` alongside** — the
license requires the copyright and license notice to travel with the font files.

- Inter — https://github.com/rsms/inter
- Barlow Condensed — https://github.com/jpt/barlow

If the files are absent the app still works; `style.css` falls through to the
system font stack.
