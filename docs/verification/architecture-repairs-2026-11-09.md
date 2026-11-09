# Architecture repair verification — 2026-11-09

The repairs follow the [system review](../architecture/BINK-ARCH-20261108/README.md).
BinKeeper remains the sole data authority. Tests use synthetic state and fake
model/printer transports. Deployment is recorded separately from source checks.

## BINK-54: refuse unsafe image export

Base: `81f93b7`, repository `halbritt/binkeeper`, branch `master`.
Both cloud clients now raise `ImagePreparationError` before transport when image
preparation fails. The local client uses the same preparation contract. Original
bytes are never a fallback. Successful JPEG preparation clears carried metadata;
the new test also reproduced JPEG-comment leakage on the previous successful path.

Verification: `BINKEEPER_TEST_DATABASE_URL=postgresql:///binkeeper_agent1331_repairs
make check` passed: **632 tests**, **20 migration tests**, lint, type checking and
wheel verification. The original regression failed for both cloud clients before
the repair. Tests verify malformed/unsupported/truncated images and injected import,
decode, orientation, conversion, resize and encoder failures make zero HTTP calls.
Valid synthetic images retain orientation, fit the 1280-pixel bound and contain no
original EXIF or JPEG comment. HTTP upload and saved-photo tests preserve originals,
photo references, site, code and supplied fields, with manual review still available.

Existing transport tests now use generated JPEGs so they still reach their intended
transport/response assertions. No live provider request, owner-data write, physical
print, schema change or production deployment was used for these checks. Fronted
installed-package verification remains a deployment check. Rollback must not restore
the raw-byte fallback: disable cloud assistance or select the accepted local route
while forward-fixing.

The repair applied `implementation-explicit-failure-policy` and
`universal-repository-contract-precedence` from Pincite packet
`pkt-61c47d23247950dd` (release `6deb1af41d41075b28577bcf2126c9db90d0e7c0`,
corpus `corpus-2026-07-12-84d6cdfc0e9f`, doctrine `doctrine-2694f0450e78ff1e`).
The retrieval gate passed. Material export/recovery questions are answered by the
accepted ADR, both transport probes and workflow regressions. Remaining generic
obligations about incident response, mutable graphs, Python special methods,
non-ASCII text, unchanged resource ownership and structural-change procedures are
nonmaterial to this bytes-to-image refusal repair. Production incident frequency
and installed behavior are unmeasured; no claim about either follows from the suite.
