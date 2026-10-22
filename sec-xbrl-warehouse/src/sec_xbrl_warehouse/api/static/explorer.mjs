// Acquisition methods describe adapter capabilities, not whether an operator enabled them.
export const SOURCES = [
  { code: 'sec', region: 'United States', name: 'SEC EDGAR', markets: 'Nasdaq, NYSE, NYSE American and other SEC filers', exchanges: ['Nasdaq', 'NYSE', 'NYSE American'], mode: 'Network adapter', hint: 'Search EDGAR for a company or ticker and open its original filings. Importing a new company into this warehouse requires an operator ingestion run.', url: 'https://www.sec.gov/edgar/search/' },
  { code: 'esef', region: 'Europe & UK', name: 'ESEF · filings.xbrl.org', markets: 'Reports published in the ESEF repository', exchanges: [], mode: 'Setup required', hint: 'Search the repository by company name or LEI. Warehouse ingestion requires confirmed reuse terms and pinned ESEF/IFRS taxonomies.', url: 'https://filings.xbrl.org/' },
  { code: 'edinet', region: 'Japan', name: 'EDINET', markets: 'Japanese periodic disclosures', exchanges: [], mode: 'API key required', hint: 'Search EDINET by issuer. Warehouse ingestion requires your own EDINET API key and pinned taxonomies. Stock symbols are not currently imported by this adapter; use the company name.', url: 'https://disclosure2.edinet-fsa.go.jp/' },
  { code: 'dart', region: 'South Korea', name: 'DART', markets: 'Korean periodic disclosures', exchanges: [], mode: 'API key required', hint: 'Search DART by company. Warehouse ingestion requires your own OpenDART API key and pinned taxonomies. Use company names for loaded results.', url: 'https://englishdart.fss.or.kr/' },
  { code: 'nse', region: 'India · NSE / BSE', name: 'Indian listed-company inbox', markets: 'Operator-supplied NSE/BSE results', exchanges: [], mode: 'Manual file import', hint: 'Obtain permitted XBRL results from the exchange or issuer, then provide them to the operator for inbox import. The application does not crawl exchanges. Use company names for loaded results.', url: 'https://www.nseindia.com/companies-listing/corporate-filings-financial-results' },
  { code: 'mca', region: 'India · MCA', name: 'MCA company filings', markets: 'Operator-supplied MCA21 filings', exchanges: [], mode: 'Manual file import', hint: 'MCA is a corporate registry, not an exchange. Obtain permitted AOC-4 XBRL files and provide them for operator inbox import. Use company names for loaded results.', url: 'https://www.mca.gov.in/' },
  { code: 'acra', region: 'Singapore', name: 'ACRA financial statements', markets: 'Operator-supplied ACRA statements', exchanges: [], mode: 'Manual file import', hint: 'ACRA is a corporate registry, not an SGX feed. Obtain permitted XBRL files through company officers or a licensed vendor. Import requires confirmed acquisition and redistribution terms. Use company names for loaded results.', url: 'https://www.bizfile.gov.sg/' },
];
export const sourceFor = (key) => SOURCES.find((s) => s.code === (/^\d{10}-\d{2}-\d{6}$/.test(key) ? 'sec' : key.split('-')[0]));
export function portalUrl(source, query = '') {
  if (source.code === 'sec' && query.trim()) return 'https://www.sec.gov/edgar/browse/?' + new URLSearchParams({ CIK: query.trim(), owner: 'exclude' });
  return source.url;
}
export function filterFilings(filings, { form = '', year = '', text = '' } = {}) {
  const needle = text.trim().toLowerCase();
  return filings.filter((f) => (!form || f.form === form) && (!year || String(f.filed_date).slice(0, 4) === year) && (!needle || `${f.accession} ${f.form} ${f.period_end_date || ''}`.toLowerCase().includes(needle)));
}

export function isUsdPoint(point) {
  return ['USD', 'iso4217:USD', 'USD/shares', 'iso4217:USD/xbrli:shares'].includes(point.unit);
}
