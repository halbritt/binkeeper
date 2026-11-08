# Vision repair verification — 2026-11-08

BINK-49 repairs the retired interactive model and delays in the photo-label
path. Base: `50470ac9925c03e0fa2e9435b9eaf75c381295e6`, branch `master`,
checkout `/home/halbritt/git/binkeeper`. The model comparison and preparation
measurements are in [the benchmark record](../benchmarks/2026-11-08-interactive-repair.md);
the decision and rollback scope are in [ADR 0007](../adr/0007-interactive-vision-repair.md).

## Automated checks

`BINKEEPER_TEST_DATABASE_URL=postgresql:///binkeeper_test_agent1315 make check`
passed: 570 tests, the separate 20-test migration selection, Ruff lint and
formatting, Pyright, wheel build, and wheel-content verification. The test
database was disposable and synthetic. The environment installed the existing
`dev` and `b1` extras; the current formatter also required expanding one
pre-existing assertion list in `test_bin_b1.py`.

Another 145 targeted tests passed with production's Python 3.12.3,
FastAPI 0.139.2, Starlette 1.3.1, and psycopg 3.3.4. They cover vision,
photo routes, quick labels, registration, label printing, and benchmarking.
The full development suite used Python 3.14.4. Test output had framework
deprecation warnings, not failures.

New regressions cover OpenRouter's controls on both attempts, bounded cloud
concurrency, upload-order merging, partial and total model failures, sending
the response before QR harvesting, manual recovery with stored-photo linkage,
and distinct reporting when storage also fails.

## Browser review

UIPass used the repository's synthetic read-only fixture and `.uipass.yaml`.
Final run `4eb962fd-6996-4889-bb53-bcc26cb80c61` passed all 12 selected cases
with zero blocking findings, warnings, or incomplete checks. The cases cover
the proposed label, vision failure, combined storage/vision failure, and
quick-label form at 320×720, 390×844, and 1280×800.

The final screenshots were inspected for all four states on phone and
desktop, plus vision failure at 320 pixels. Controls fit their viewports;
failure pages provide a clear manual create/print action and an independent
storage result. The first review caught proposal-specific copy on the manual
form; it was corrected and the same cases rerun. The remaining narrow cases
received mechanical checks only. Catalog, manage, photo-drop form, and
registration layouts were excluded from this focused review.

Local evidence bundles are `/tmp/binkeeper-1315-uipass` (before copy correction)
and `/tmp/binkeeper-1315-uipass-final` (after), with `review.md` beside the
final verdict and screenshots. These are temporary host artifacts, not owner
data. Browser fixtures do not perform mutations; database-backed tests cover
the create/print contracts.

## Deployment

The verified wheel was installed into `/opt/binkeeper/venv` with dependencies
unchanged. The explicit interactive model pin in the protected service
environment was changed to `qwen/qwen3.8-flash`, and the same
`binkeeper.service` was restarted. Previous package and environment backups
remain owner-local. No schema, writer-authority, evidence, or printer changes
were needed.

The installed configured client produced a valid synthetic-image proposal in
2.038 seconds. Its code and the photo-result template byte-matched the
verified checkout. Through `https://proximal.tail0ecc2e.ts.net:8766`, health,
readiness, the photo form, quick-label form, and catalog all returned HTTP 200.
An empty upload rendered the expected recovery message; a cross-origin POST
was rejected with HTTP 403. Readiness included the authenticated backup check.
These probes did not append production inventory or send physical print jobs.

## Decision evidence

Pincite release `6deb1af41d41075b28577bcf2126c9db90d0e7c0` passed its retrieval
gate. The final packet is `pkt-30f79790f9cbbd55`, SHA-256
`30f79790f9cbbd55a2b51be75d3c0df9d3406a9d1fc5fe1044ef4023631964df`, corpus
`corpus-2026-07-12-84d6cdfc0e9f`, doctrine `doctrine-2694f0450e78ff1e`,
retriever `retriever-8b654e47fcb1be28`. Its ceiling is execution within the
owner's repair authorization. The decision relies on the reproduced HTTP
failure, current source, bounded benchmark, tests, and actual endpoint probes.

Unmet generic obligations are nonmaterial to this bounded repair: no ingest
population, database identity key, ranking algorithm, asynchronous UI payload,
or monitoring system is being redesigned. The benchmark explicitly selects
the first six eligible bins rather than claiming a relevance ranking. Live
provider calls verify the configured model; no startup-wide model availability
guarantee is claimed. Existing future plans do not determine the repair of a
retired endpoint. Performance claims are limited to the measured sample.
