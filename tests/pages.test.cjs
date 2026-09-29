const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const pages = Object.fromEntries(['index','analysis'].map(name=>[name,fs.readFileSync(`app/static/${name}.html`,'utf8')]));
const ids = source => [...source.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);

test('trade dashboard owns overview, charts, signal qualification and manual trade containers',()=>{
  const source=pages.index, present=ids(source);
  assert.match(source,/<h1>Trading Dashboard<\/h1>/);
  for(const id of ['status','feed','freshness','socket','refresh','nifty','bank','vix','updated',
    'nifty-chart-frame','banknifty-chart-frame','nifty-signal','banknifty-signal','nifty-trade','banknifty-trade'])
    assert.ok(present.includes(id),id);
  for(const index of ['nifty','banknifty']) {
    for(const control of ['interval','zoom-in','zoom-out','reset','refresh']) assert.ok(present.includes(`${index}-chart-${control}`));
  }
  assert.doesNotMatch(source,/id="(?:nifty|banknifty)-(?:options|structure)|>Market Structure<|>Options Intelligence</);
  assert.match(source,/class="market-overview"/);
  assert.match(source,/class="chart-grid"/);
  assert.match(source,/class="structure-grid signal-grid"/);
});

test('analysis owns both full structure and options panels with expiry controls',()=>{
  const source=pages.analysis, present=ids(source);
  assert.match(source,/<h1>Market Analysis<\/h1>/);
  for(const id of ['structure-status','nifty-structure','banknifty-structure','nifty-options','banknifty-options',
    'nifty-options-expiry','banknifty-options-expiry']) assert.ok(present.includes(id),id);
  assert.doesNotMatch(source,/id="(?:nifty|banknifty)-(?:chart|trade|signal)|class="chart-grid"/);
  for(const label of ['Market Structure','Options Intelligence','Automatic analysis expiry','VWAP','opening range','EMAs'])
    assert.ok(source.includes(label),label);
  assert.equal((source.match(/class="structure-grid"/g)||[]).length,2);
});

test('both pages have same-origin navigation, current-page indication and explicit alarm guidance',()=>{
  for(const [name,source] of Object.entries(pages)) {
    assert.match(source,/<nav class="page-nav" aria-label="Main navigation">/);
    assert.match(source,/<a href="\/"[^>]*>Trade Dashboard<\/a>/);
    assert.match(source,/<a href="\/analysis"[^>]*>Market Analysis/);
    const current=name==='index' ? '/' : '/analysis';
    assert.ok(source.includes(`<a href="${current}" aria-current="page">`));
    assert.equal((source.match(/aria-current="page"/g)||[]).length,1);
    assert.match(source,/STOP alarms/);
    assert.match(source,/sound armed/);
    assert.match(source,/<meta name="viewport" content="width=device-width,initial-scale=1">/);
    assert.equal(new Set(ids(source)).size,ids(source).length,'Unique IDs');
    assert.doesNotMatch(source,/<(?:script|link)[^>]+(?:src|href)="https?:/);
  }
  assert.match(pages.index,/<a href="\/analysis" target="_blank" rel="noopener"[^>]*aria-label="Market Analysis \(opens in a new tab\)"/);
  assert.match(pages.analysis,/does not run trade polling or alarms/);
});

test('mobile grids can shrink and signal tables scroll locally without masking page overflow',()=>{
  const css=fs.readFileSync('app/static/style.css','utf8');
  assert.match(css,/@media\(max-width:720px\)\{\s*\.chart-grid\{grid-template-columns:1fr\}/);
  assert.match(css,/@media\(max-width:650px\)\{[\s\S]*\.market-overview,\.structure-grid\{grid-template-columns:minmax\(0,1fr\)\}/);
  assert.match(css,/\.structure-grid article\{min-width:0\}/);
  assert.match(css,/\.signal-table\{overflow-x:auto/);
  assert.match(css,/button,select,\.connection-controls a\{min-height:44px\}/);
  assert.doesNotMatch(css,/(?:body|html)\s*\{[^}]*overflow(?:-x)?:hidden/);
});
