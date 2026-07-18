"""Shared local-first web substrate for BinKeeper operator surfaces.

The package is intentionally presentation-focused: templates, static helper
paths, status vocabulary, and small request guards. It must not import
interview, extraction, segmentation, or consolidation business logic.

New-surface checklist (RFC 0081, binding):

1. HTML routes use the chrome helper / Jinja path only — no HTML assembled
   in Python (``tests/test_ui_framework_standard.py`` enforces the scan
   family against the shrink-only exemption list).
2. Extend the shared shell via :func:`binkeeper.web.chrome.build_surface_chrome`
   (it pins autoescape and the surface-first/shared-second loader order) and
   mount static assets via :func:`binkeeper.web.chrome.mount_shared_static`.
3. Use ``surface_path`` (and the chrome-supplied versioned static URLs) for
   every URL; never hard-code a mount prefix.
4. Register the surface in ``_surface_tabs.html``.
5. No design-token re-declaration (``--color-*`` / ``--type-*`` /
   ``--space-*`` live in ``static/binkeeper.css`` only); per-surface ``<style>``
   is for surface-unique layout scoped under a surface class — a rule needed
   by two or more surfaces moves to ``binkeeper.css``; no bare generic-element
   selectors; no filename shadowing of shared templates.
6. Route/template tests plus no-external-asset coverage; privacy/authority
   tests cover both allowed and denied paths.
7. Gates (origin/tier/auth/redaction) run in handlers/dependencies before
   context construction; templates receive plain-dict or typed view models,
   never raw domain/ORM objects. Auth-token rendering (e.g. capture's
   bearer/CSRF form embedding) is gate-adjacent, not styling.
"""

from __future__ import annotations
