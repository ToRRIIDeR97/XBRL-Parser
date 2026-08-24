# Test Fixtures

- `catalog_notes_sample.html`: stable SEC-catalog parser fixture.
- `submissions_sample.json`: columnar `data.sec.gov/submissions` fixture.
- `fsnds-mini.zip`: all eight SEC FSNDS tables with one representative row.
- `xbrl/simple/`: small conventional XBRL instance for the future Arelle adapter tests.

Run `python scripts/build_fixture_zip.py` after changing bulk headers or fixture rows.
Do not replace the synthetic filing with a real issuer filing; keep real-filing corpus
metadata/checksums separate to avoid redistributing unnecessary source documents.
