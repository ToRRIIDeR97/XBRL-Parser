# Test Fixtures

- `catalog_notes_sample.html`: stable SEC-catalog parser fixture.
- `submissions_sample.json`: columnar `data.sec.gov/submissions` fixture.
- `fsnds-mini.zip`: all eight SEC FSNDS tables with one representative row.
- `xbrl/simple/`: conventional XBRL instance plus schema, label, presentation, and calculation linkbases for Arelle extraction tests.

Run `python scripts/build_fixture_zip.py` after changing bulk headers or fixture rows.
Do not replace the synthetic filing with a real issuer filing; keep real-filing corpus
metadata/checksums separate to avoid redistributing unnecessary source documents.
