# Saved-photo inbox and field prefills — 2026-11-08

Work items: BINK-51, BINK-52. Repository: `halbritt/binkeeper`, branch `master`,
base `d4e6121f930a3a3dc9d8cd90c078f011c8300ab5`. No secondary worktree.

## Delivered behavior

Saved photos is an upload inbox. Uploading stores originals without invoking
vision or recording inventory/location observations. Later, the owner selects
up to 12 photos and either attaches them to an existing bin or creates a bin.
Creation retains every selected photo through confirmation and links them in
the registration transaction. Attachment checks scope and bin existence before
appending any links; replay does not duplicate them or change the bin profile.

Both upload-and-propose and saved-photo creation accept Label and Contents
prefills. Only blank fields are inferred. A supplied label is preserved while
contents are identified; supplied contents are preserved while a label is
suggested. Supplying both bypasses model setup and inference. Failures retain
entered text and leave manual review available.

The owner's requested repair also attached the two missing photos from an
identified three-photo upload to its already-created bin. The resulting bin
has all three photos. Its label, contents, site, location evidence and original
blobs were unchanged. No label was printed. Private identifiers and contents
are excluded from this repository.

## Verification

- `BINKEEPER_TEST_DATABASE_URL=postgresql:///binkeeper_test_agent1315 make check`:
  lint, formatting and type checks passed; 604 tests passed; the separate
  migration gate passed 20 tests; wheel build and resource checks passed.
- Synthetic regressions cover all four prefill combinations on both routes,
  model/vault failures, upload without analysis or inventory writes, atomic
  rejection of invalid upload batches, attachment scope and idempotency,
  profile/location preservation, multi-photo creation and rollback on an
  unavailable additional photo. Existing nightly and printer tests passed.
- UIPass run `adf2d953-7330-48e3-ad5f-ecfc631622eb` completed all 63 cases across
  seven surfaces and 320, 390 and 1280 px viewports, with no blocking findings.
  Screenshot inspection caught clipped navigation; wrapping the tabs fixed it.
  The final review is
  `/tmp/binkeeper-1315-photo-inbox-uipass-final/review.md`. Three warnings are
  intentional outage responses; 17 contrast-assessment warnings concern the
  existing catalog review panel at 320 px. This is not a claim of a complete
  accessibility audit.
- A focused browser check verified the four action labels, selected-photo
  counts, and submission of all three selected IDs to the attach route while
  new-bin Site was blank. The request was intercepted against the synthetic
  fixture; it did not mutate production.
- The built wheel was installed into the existing production venv and the
  same `binkeeper.service` restarted. Eleven changed runtime files/templates
  matched the checkout by SHA-256. No schema or environment change was needed.
- Through `https://proximal.tail0ecc2e.ts.net:8766`, health and readiness passed.
  Gallery HTML took 0.086 seconds; review with both fields supplied took 0.083
  seconds; one contents-only inference with the owner's label took 6.633
  seconds. These are individual observations, not latency guarantees.
  Confirmation retained all three original references. Replaying attachment
  took 0.981 seconds and added no evidence. Missing-Origin writes were rejected,
  as was an empty upload. Verification did not submit registration or printing.

Selected live verification is stored privately under
`~/.local/state/binkeeper/photo-inbox-20261108/`. The earlier owner attachment
receipt is under `~/.local/state/binkeeper/saved-photo-recovery-20261108/`.

## Authority and rollback

The owner authorized the workflow and the attachment repair. BinKeeper remains
the sole data/runtime authority; existing originals and history remain intact.
The advisory export scope is unchanged. Rollback restores the prior installed
package from `photo-inbox-20261108/previous-install.tar.gz` and restarts the same
service. No database rollback is needed; valid append-only attachment evidence
remains. The previous model repair and provider pin are unaffected.

## Decision provenance

The implementation follows the repository's canonical capture and registration
contracts. Requiring reuploads or manual re-entry was rejected because it does
not satisfy the requested saved-photo workflow. There is no new upload-session
table, model route or second writer. Missing scoped originals, unknown bins and
invalid image batches stop the affected write before any attachment is added.

Pincite packet `pkt-a587a1ae2c327e94`, SHA-256
`a587a1ae2c327e94ceafb0bf0938daac89f8c71f8afdfcdedadeb335937ee45c`, used
`doctrine-2694f0450e78ff1e`, `retriever-8b654e47fcb1be28`, and
`corpus-2026-07-12-84d6cdfc0e9f` from validated release
`6deb1af41d41075b28577bcf2126c9db90d0e7c0`. Source locators are the repository's
`AGENTS.md`, ADR 0007, `bin_manage.append_saved_bin_photos`,
`bin_photo_web._analyze`, `bin_photo_web._register_and_view`, and their route
and database tests. The private decision receipt records supporting doctrine
locators and citation consumption.

Eleven generic obligations remain nonmaterial to this bounded change:
configuration reference resolution, named mismatch errors and identifier sets
(no configuration registry changes); match-text composition, scoring before
truncation and two selection-contract formulations (the gallery is chronological
pagination, not relevance ranking); serving/evaluation parity, gate inventory,
direct-signal proof and authoritative gate signals (no operational gate changes
or general parity claim). Verification above covers this deployed workflow.
