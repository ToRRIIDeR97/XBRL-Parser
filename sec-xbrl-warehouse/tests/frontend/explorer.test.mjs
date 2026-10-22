import assert from 'node:assert/strict';
import test from 'node:test';
import { SOURCES, sourceFor, portalUrl, filterFilings, isUsdPoint } from '../../src/sec_xbrl_warehouse/api/static/explorer.mjs';

test('filing filters combine form, filed year and accession without mutating data', () => {
  const filings = [
    { accession: '0000320193-25-000079', form: '10-K', filed_date: '2025-10-31', period_end_date: '2025-09-27' },
    { accession: '0000320193-24-000123', form: '10-K', filed_date: '2024-11-01', period_end_date: '2024-09-28' },
    { accession: 'edinet-S100ABCD', form: 'Annual', filed_date: '2025-06-27', period_end_date: null },
  ];
  assert.equal(filterFilings(filings).length, 3);
  assert.deepEqual(filterFilings(filings, { form: '10-K', year: '2025', text: '000079' }), [filings[0]]);
  assert.deepEqual(filterFilings(filings, { text: 'S100abcd' }), [filings[2]]);
  assert.equal(filterFilings(filings, { year: '2026' }).length, 0);
  assert.equal(filings.length, 3);
});

test('all existing source regions have explicit acquisition modes and safe portal links', () => {
  assert.deepEqual(SOURCES.map((s) => s.code), ['sec', 'esef', 'edinet', 'dart', 'nse', 'mca', 'acra']);
  for (const source of SOURCES) {
    assert.equal(new URL(portalUrl(source)).protocol, 'https:');
    assert.ok(source.hint && source.mode);
  }
  const sec = sourceFor('0000320193-25-000079');
  assert.equal(sec.code, 'sec');
  assert.equal(new URL(portalUrl(sec, 'BRK.B & owner=include')).searchParams.get('CIK'), 'BRK.B & owner=include');
  assert.equal(new URL(portalUrl(sec, 'BRK.B & owner=include')).searchParams.get('owner'), 'exclude');
  assert.equal(sourceFor('edinet-S100ABCD').code, 'edinet');
  assert.equal(sourceFor('unknown-file'), undefined);
  assert.equal(SOURCES.find((s) => s.code === 'acra').mode, 'Manual file import');
});

test('USD financials retain qualified per-share units without relabeling other currencies', () => {
  for (const unit of ['USD', 'iso4217:USD', 'USD/shares', 'iso4217:USD/xbrli:shares']) assert.ok(isUsdPoint({ unit }));
  for (const unit of ['JPY', 'SGD', 'EUR', 'shares', null]) assert.equal(isUsdPoint({ unit }), false);
});
