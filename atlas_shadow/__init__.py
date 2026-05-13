"""atlas-shadow — offline Atlas-vs-grep shadow benchmark runner.

This package is the offline benchmark harness for Atlas's retrieval surfaces.
It is NOT an Atlas component — it has no Atlas Python dependencies and
reaches Atlas exclusively by shelling out to `workspace run atlas-query` in a
checkout of `tandemstream/core`.

See README.md and CLAUDE.md for layout and rules. The Phase 2 packet that
introduced this package lives in `tandemstream/core` at
`products/tandem/packages/python/atlas/docs/work/2026-05-13-atlas-shadow-phase2-v1/`.
"""

__version__ = "0.1.0"
