# Project Risk Assessor (PRA)

Components from a risk-assessment service that reads a project and contract
database, scores it against deterministic rules, and lets an LLM explain the
result without ever letting the LLM decide it.

This repository is a **curated subset**. The data-access layer is written
against a specific internal schema and is not included, so this is a set of
components with their tests rather than a runnable application. Everything here
runs and is covered — **342 tests pass**.

## Why it might be worth reading

Most of what is interesting sits in the boundaries, not the features.

### The LLM never scores anything

Rules produce the score and the findings; the model only writes prose about
them. A test pins this: the same input yields the same `risk_level` and the same
findings whether or not a chat is open.

That is not enough on its own, because a model can invent a clause inside an
explanation it was only asked to phrase. So citations are verified against the
payload actually sent — contract references strictly, amounts softly, since a
legitimate cross-document sum would otherwise false-positive. Failures annotate
rather than delete: excising a citation mid-sentence leaves prose that still
reads as authoritative.

`app/contracts/chat_verify.py`, `app/contracts/chat_pipeline.py`

### SSRF-hardened attachment fetching

Attachment URLs come out of a database, and database content is untrusted
input. Each guard maps to a specific bypass: fail-closed host allowlist, exact
host match rather than suffix match, https only, resolved address must be
global, any private address among several refuses the whole fetch, no redirect
following, size ceiling on both the declared length and the bytes actually
read, extension allowlist, content-addressed local filenames, and errors that
never echo the URL back.

There is one deliberate escape hatch, for machines where a proxy intercepts
DNS — because then the locally resolved address is never the one dialled, and
the check blocks real traffic while no longer governing the connection. It is
off by default and stamps every affected result, so a weakened run cannot be
mistaken for a guarded one.

`app/contracts/attachment_fetch.py`, `tests/test_attachment_fetch.py`

### Redaction that runs at one chokepoint

Unified social credit codes, national IDs, bank accounts, emails, phone
numbers, and the values of labelled fields. Three rules learned the hard way:
redact rather than delete, because a silently emptied finding is worse than a
marked one; redact *before* clipping, because truncating mid-account leaves a
fragment no pattern matches but a reader can still reconstruct; and apply it at
a single point rather than per rule, because the rule that forgets is the rule
that leaks.

Tables are redacted on the cell grid, not on flattened text — flattening first
destroys the column structure the rules need.

`app/contracts/text_redaction.py`

### Money parsing that refuses to guess

Contract text mixes 万/亿 and Chinese capital numerals while databases store
plain yuan; comparing without normalising is a 10,000x error.

Scanning also requires a currency marker. Without one, a real document yielded
47 "amounts" including a standards number, the signing date, a quantity, and
every clause number. With it, 47 became 5, all genuine.

`app/contracts/text_money.py`, `app/contracts/text_rules.py`

### Document extraction that reports what it cannot do

A file with no text layer stops with an explicit signal instead of returning
empty text, which would read as "checked and clean". A scanned PDF and an empty
document are reported *differently*, because OCR helps the first and not the
second, and an error should point at the right next action.

`.doc` extraction checks the printable-character ratio, since a legacy binary
can yield plausible-length garbage.

`app/contracts/text_extraction.py`

### Clause splitting built on measurement, not on one sample

The splitter originally keyed on `第X条`, taken from a single document. Checking
the largest real files found that marker appears **zero times**; the actual
delimiter is `一、二、`. The original would have dropped every document into the
paragraph fallback while appearing to work.

`app/contracts/clause_split.py`

### Acknowledgements that expire when the facts change

An acknowledgement is scoped to the score it was made at. Accepting a finding at
40 does not silence the same finding at 80 — it reopens and is marked stale.
Nothing is ever deleted.

`app/contracts/annotations.py`

### Local storage that survives concurrency

One shared WAL negotiation, `busy_timeout` on every connection, foreign keys on.
WAL alone was not enough: the real defect was the missing per-connection
timeout, and the concurrency test was verified to have teeth by reproducing the
old configuration and watching it fail.

`app/sqlite_support.py`

### A frontend with no build step

Four classic scripts sharing one global scope, with the cross-file surface named
explicitly as `window.RiskAgent` rather than left implicit in the global object.
Design tokens with a three-state theme — system, light, dark — because a two-way
toggle silently opts a user out of their OS setting with no way back.

The model's answers are parsed into real elements. Escaping markdown into
`innerHTML` showed the syntax literally *and* collapsed every newline, so a
table arrived as one run-on line. Parsing is separate from rendering so the
parser is testable without a DOM, and rendering only ever calls
`createElement`/`textContent` — model output is data and must never become
markup.

`app/static/js/app.js`, `app/static/css/app.css`

## Running the tests

```bash
python -m unittest discover -s tests
```

No configuration needed. Nothing here reaches the network: the fetcher tests
inject a fake resolver and never make a real request.

## Not included

The database gateway, the risk engine's persistence wiring, the job runner, the
HTTP application, and the project documentation. They are written against a
specific internal schema and are not the interesting part.

## Notes on the code

Comments explain *why*, and say when something is a judgement rather than a
measurement. Several record a mistake that was made and corrected, because the
correction is usually the useful part.

Where a rule could not be validated against trustworthy data, that is stated
rather than papered over. A hit count is never treated as evidence that a rule
is correct.
