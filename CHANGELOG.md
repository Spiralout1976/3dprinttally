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
- Filament inventory by material and color, with grams-weighted average cost per
  gram over the purchase ledger; brand is an optional field that preserves vendor
  detail without splitting the pool
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
