# September 12, 2026 — security release

- Restrict direct startup and validate Host headers before authentication.
- Reject spreadsheet formula/control-character CSV exports and malformed CSV rows.
- Handle non-ASCII authentication and CSRF input without HTTP 500 responses.
- Bound backup extraction and verify restored images; retire unsafe catalog reset.
- Update the PDF test dependency, pin the base image and cap container resources.
- Include AGPL-3.0-or-later license and an explicit corresponding-source download.

# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-09-12

First public release.

This project ran privately for some time as an internal tool before being
generalized and published. History before this release is not included; 1.0.0 is
the baseline.

### Added
- Product catalog with SKU identity (`TYPE-NNN`), free-text collections, and
  bill-of-materials made of printed parts
- One-level assemblies, costed from components looked up fresh on every render
- Job Calculator with per-part run planning, scrap handling, design fee and
  minimum job charge
- Filament inventory with per-brand stock ledgers and pooled cost per gram across
  brands sharing a material and color
- Label sheet PDF generation for 30-up 2.625" x 1" sheets, plus a calibration
  sheet for printer offset
- QuickBooks Online product identity CSV export
- Catalog CSV version 2 with preview-and-confirm import, preserving filament
  identities, overrides, multi-color draws and assembly relationships
- Scheduled backup sidecar writing verified ZIPs every 24 hours, retaining 30,
  with verifiable restore
- Optional HTTP Basic authentication, CSRF protection, CSP with per-request
  nonces, and read-only non-root containers
- Regression test suite running against disposable data directories

### Changed from the internal version
- All deployment configuration is environment-driven; no hostnames, ports or
  paths are hardcoded
- `BIND_ADDRESS` now defaults to `127.0.0.1` instead of `0.0.0.0`, because the
  app ships without authentication enabled
- `TZ` defaults to `UTC`
- Data directory override renamed to `TALLY_DATA_DIR`

### Known limitations
- No multi-user support, roles, or per-user data separation
- No finished-goods inventory counting
- Assemblies nest one level only
- Print time is entered manually from the slicer; there is no printer integration
