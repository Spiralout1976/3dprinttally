# Contributing

Thanks for looking. A few things that will save us both time.

## Before you write code

**Read [AGENTS.md](AGENTS.md) and [docs/COSTING-MODEL.md](docs/COSTING-MODEL.md)
first.** Several things in the costing code look like bugs and are deliberate —
the two precision modes, the scrap branch, `cost_per_unit` not using
`cost_per_part`. Each is documented with the reason. A PR that "fixes" one of
them without addressing the reasoning will be declined.

**Open an issue before a large PR.** The out-of-scope list in `AGENTS.md` is real,
and I would rather say no in a paragraph than after you have written a thousand
lines.

## Hard rules

- **Manual entry is never removed.** Every field stays free text with
  autocomplete. No constrained dropdowns that lock a user out of typing a value.
- **Saved job costs never recompute.** No exceptions, no flags, no "just for
  preview".
- **No new runtime network calls.** This app talks to nothing. The only outbound
  request in the project is the explicit, manually-run dependency check.
- **No default credentials and no wider default network exposure.**
- **Migrations, not schema edits,** for anything touching `filament_types` or
  `filament_transactions` — those hold real purchase history in live installs.

## Practical

- Target Python 3.12. Dependencies stay pinned.
- Run the suite before opening a PR:
  ```sh
  pip install -r requirements-test.txt
  python -m unittest discover -s tests -v
  ```
- New behavior needs a test. Costing changes need a test with worked numbers in
  it, because "it looks right" is how pricing bugs ship.
- Match the surrounding style rather than reformatting. No repo-wide
  reformatting PRs.
- Comments should say *why*, not *what*. The existing code does this and it is
  the reason the project is maintainable.

## Licensing

Contributions are accepted under AGPL-3.0-or-later, the project's license. By
opening a PR you agree your contribution is licensed that way.

## Bug reports

Include what you did, what you expected, what happened, and the version or
commit. For a costing bug, include the actual inputs — quantities, plate counts,
print times, rates — because almost every costing report comes down to
`qty_on_plate` versus `qty_per_product`.
