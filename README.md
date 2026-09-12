# 3DPrintTally

**Self-hosted catalog, job costing and label printing for a small 3D printing shop.**

You designed a part. Someone wants forty of them. What do you charge?

3DPrintTally answers that question with real numbers instead of a guess: filament
at your actual blended cost per gram, machine time, electricity, machine wear,
the labour of pulling parts off a plate, the scrap you expect to throw away, and
the one-time CAD work that should not be billed again on the reorder. It keeps a
catalog of products and the parts they are built from, prints address-label
sheets for bins and shelves, and exports your product identities to QuickBooks
Online.

It is a single Flask app with a SQLite database. It runs in one container, holds
your data in a directory you can copy, and talks to no external service.

---

## Is this the right tool?

There are good tools in this space and they mostly solve a different problem.

| | 3DPrintTally | Filament inventory tools | Hosted shop platforms |
|---|---|---|---|
| Filament stock and cost/gram | Yes | Yes, usually better | Yes |
| Products built from multiple parts (BOM) | Yes | No | Sometimes |
| Job quoting with scrap, labour, design time | Yes | No | Yes |
| Physical label sheet printing | Yes | No | No |
| QuickBooks product export | Yes | No | Varies |
| Self-hosted, no account, no subscription | Yes | Varies | No |

If you only want to know how much filament you have,
[Spoolman](https://github.com/Donkie/Spoolman) is more mature and you should use
it. 3DPrintTally exists for the case where filament is one input to a price, and
the price is the thing you actually need.

**Honest scope limits.** This is a single-operator tool. There is no multi-user
support, no roles, no order pipeline, no customer portal, no finished-goods
inventory count, and no printer integration — print time is typed in from your
slicer. Assemblies nest exactly one level deep. These are deliberate; see
`AGENTS.md`.

---

## Quick start

Requires Docker with Compose v2.

```sh
git clone https://github.com/YOURNAME/3dprinttally.git
cd 3dprinttally
cp .env.example .env
docker compose up -d --build
```

Open <http://127.0.0.1:8091>. The database is created on first run.

Then, in this order:

1. **Settings** — set your labour rate, electricity rate, printer wattage,
   margins and scrap rate. Everything downstream is priced from these.
2. **Filament** — add the spools you actually own, with what you paid.
3. **Products** — create a SKU, then add the printed parts that make it up.
4. **Job Calculator** — pick products, enter the print time from your slicer,
   and get a price.

The `/guide` page inside the app walks through the same thing in more detail.

### Before you expose it

**The app ships with authentication off and bound to loopback.** That is safe on
the machine it runs on and nowhere else. Before changing `BIND_ADDRESS`, do one
of these:

- Set `BASIC_AUTH_USERNAME` and `BASIC_AUTH_PASSWORD_HASH` in `.env` (generate
  the hash with `python3 tools/password_hash.py`), or
- Put it behind a reverse proxy or VPN that authenticates for you.

There is no login page and no user accounts. Anyone who can reach the port can
read and change everything. See [SECURITY.md](SECURITY.md).

---

## What it actually does

**Catalog.** Products are identified by SKU (`TYPE-NNN`, e.g. `SGN-003`). A
product is made of one or more printed parts, each with its own grams, print
time and filament. A product can also be an assembly of other products. SKUs are
colour-agnostic on purpose: `SGN-001` is the same part in white or blue, and the
colour is chosen per job.

**Costing.** Covered in detail in [docs/COSTING-MODEL.md](docs/COSTING-MODEL.md).
The short version: filament cost is blended across every brand you buy a given
material and colour in, scrap uplift is applied at the plate level, and a saved
job is frozen — it shows what you quoted, permanently, and never silently
re-prices when your spool costs change.

**Labels.** Generates print-ready PDFs for 30-up 2.625" × 1" address label
sheets — the common format sold as Avery® 5160 and 8160, and by every generic
equivalent. Includes a calibration sheet for dialling in printer offset.

**Filament.** Per-brand stock ledgers, pooled cost per gram across brands sharing
a material and colour, and purchase history.

**QuickBooks Online.** Exports product identity rows (name, SKU, category) for
bulk import. It deliberately does not invent accounts, item types, quantities or
balances. See [QUICKBOOKS-EXPORT.md](QUICKBOOKS-EXPORT.md).

**Backups.** A sidecar container writes a verified ZIP of the database and
photos every 24 hours, keeps the newest 30, and exposes its last-success time in
Settings. Restore is verifiable before you commit to it. Copying `backups/`
somewhere off the host is your job.

---

## Documentation

- [DEPLOYMENT.md](DEPLOYMENT.md) — install, upgrade, backup, restore, rollback
- [docs/COSTING-MODEL.md](docs/COSTING-MODEL.md) — how prices are calculated, and why
- [QUICKBOOKS-EXPORT.md](QUICKBOOKS-EXPORT.md) — export layouts and import steps
- [AGENTS.md](AGENTS.md) — architecture map and design decisions
- [SECURITY.md](SECURITY.md) — threat model and reporting
- [CONTRIBUTING.md](CONTRIBUTING.md) — before you open a PR

## Tests

```sh
pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

Tests run against disposable data directories and never touch a live database.

---

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

In plain terms: use it, modify it, self-host it freely. If you run a modified
version as a service that other people use over a network, you have to publish
your changes.

## Trademark note

Avery® is a registered trademark of Avery Dennison Corporation. This project is
not affiliated with, endorsed by, or connected to Avery Dennison. Their product
numbers are referenced only to describe the physical label sheet dimensions this
software is compatible with.
