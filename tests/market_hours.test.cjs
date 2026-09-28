const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');

const source = fs.readFileSync('app/static/market_hours.js', 'utf8');
const cases = [
  ['Monday before opening', '2026-09-28T08:59:59+05:30', false],
  ['Monday opening inclusive', '2026-09-28T09:00:00+05:30', true],
  ['Monday last second', '2026-09-28T15:39:59+05:30', true],
  ['Monday last millisecond', '2026-09-28T15:39:59.999+05:30', true],
  ['Monday closing exclusive', '2026-09-28T15:40:00+05:30', false],
  ['Saturday', '2026-10-03T10:00:00+05:30', false],
  ['Sunday', '2026-10-04T10:00:00+05:30', false],
  ['Tuesday', '2026-09-29T09:00:00+05:30', true],
  ['Wednesday', '2026-09-30T09:00:00+05:30', true],
  ['Thursday', '2026-10-01T09:00:00+05:30', true],
  ['Friday', '2026-10-02T09:00:00+05:30', true],
  ['IST Saturday while UTC is Friday', '2026-10-02T20:00:00Z', false],
  ['IST Monday while US is Sunday', '2026-09-28T03:30:00Z', true],
  ['US standard time opening', '2026-01-05T03:30:00Z', true],
  ['US standard time cutoff', '2026-01-05T10:10:00Z', false],
  ['US daylight time opening', '2026-07-06T03:30:00Z', true],
  ['US daylight time cutoff', '2026-07-06T10:10:00Z', false],
  ['IST midnight', '2026-09-28T00:00:00+05:30', false]
];
const context = vm.createContext({window:{}});
vm.runInContext(source, context);
for (const [label, timestamp, expected] of cases) {
  test(label, () => assert.equal(context.window.marketAutoRefreshAllowed(new Date(timestamp)), expected));
}

for (const [zone, expectedOffset] of [['UTC', 0], ['America/New_York', 240],
  ['America/Los_Angeles', 420], ['Asia/Kolkata', -330]]) {
  test('same IST boundaries with system timezone '+zone, () => {
    const probe = `
      global.window = {};
      ${source}
      const cases = ${JSON.stringify(cases)};
      process.stdout.write(JSON.stringify({
        offset: new Date('2026-09-28T03:30:00Z').getTimezoneOffset(),
        results: cases.map(([, timestamp]) => window.marketAutoRefreshAllowed(new Date(timestamp)))
      }));`;
    const result = spawnSync(process.execPath, ['-e', probe], {
      encoding:'utf8', env:{...process.env, TZ:zone}
    });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    const actual = JSON.parse(result.stdout);
    assert.equal(actual.offset, expectedOffset, 'the probe actually changed the host timezone');
    assert.deepEqual(actual.results, cases.map(([, , expected]) => expected));
  });
}
