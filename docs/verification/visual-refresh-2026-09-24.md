# BinKeeper visual refresh, 2026-09-24

The owner rejected the appearance of the [first UIPass fix](uipass-2026-09-24.md): the overflow and disclosure defects were fixed, but the pages still looked like a dense admin interface. That earlier `SHIP_WITH_NOTES` verdict applies to the measured defects, not to acceptance of the visual design. This pass changes the layout of the catalog, new-label form, and bin management page while retaining their existing actions.

The catalog now puts search ahead of the results, uses compact two-column cards on desktop, and puts the bin name, contents, and current site ahead of secondary metadata. Empty photo states use one line of text instead of a large blank frame. A synthetic photo checks the real-thumbnail state. On phones, all four navigation links stay visible in one row and cards stack. The new-label form presents photo selection and its primary action first; optional notes, site, and code are in a native disclosure. The management page groups details and photos side by side on desktop and adds links to its sections.

A read-only check of the live catalog after the first deployment exposed a scale problem that the initial two-bin fixture missed: 13 pending label reviews filled the page before any bin card appeared. The review queue is now collapsed by default behind a visible count. The fixture models 13 synthetic proposals and tests both the collapsed and expanded states. Opening the section still reveals every proposal and its review link.

## Visual evidence

All images use synthetic bins and a generated photo from [`scripts/uipass_fixture.py`](../../scripts/uipass_fixture.py). No owner inventory data was captured.

| Page | Screenshot |
|---|---|
| Catalog, 1280 px | [Desktop catalog](catalog-visual-desktop.png) |
| Catalog, 320 px | [Narrow catalog](catalog-visual-narrow.png) |
| Label queue expanded, 1280 px | [Expanded review queue](catalog-reviews-open.png) |
| New label, 320 px | [Narrow photo form](photo-visual-narrow.png) |
| Manage bin, 1280 px | [Desktop management](manage-visual-desktop.png) |

The [final UIPass verdict](uipass-visual-final.json) records 27 completed cases across 320, 390, and 1280 CSS px, with no findings or warnings. Cases include a populated catalog, no-match results, expanded bin details and review queue, the new-label form with its options expanded, registration, a pending management review, and a location-section jump. UIPass checked selected accessibility rules, horizontal overflow, page errors, and console errors. The synthetic browser did not submit forms or exercise owner inventory, printing, or the vision provider.

The catalog and photo web tests passed: 79 passed, 17 skipped. Ruff lint and format checks, Pyright, and wheel resource verification passed. The pending photo form copy assertion was adjusted only for its new sentence casing.

## Deployment

BinKeeper `edc80da` shipped the visual refresh. The live-content check then exposed the long review queue, so `090cef3` changed it to a disclosure and was pushed to `master` and installed into `/opt/binkeeper/venv`. Before each install, a rollback wheel matching the installed revision was built and checked. The service is active and `/readyz` is healthy over tailnet HTTPS. Installed templates and the served catalog match `090cef3`.

A read-only browser check against the live catalog returned HTTP 200 at 1280 and 320 CSS px. All four navigation links were visible, the review queue was collapsed, and document width equaled viewport width at both sizes. The first bin card started at 702 px on desktop and 919 px on the narrow phone, after the search and compact review summary. The live browser screenshots remained under `/tmp` and were not added to version control because they contain owner inventory.
