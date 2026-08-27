import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const atlas = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
const [, body] = atlas.match(/function updateHash\(type, id\) \{([\s\S]*?)\n      \}\n\n      function applyHash/) || [];

test('detail routes stay on the preview document origin', () => {
  assert.ok(body, 'index.html defines updateHash');

  const location = {
    origin: 'https://htmlpreview.github.io',
    pathname: '/',
    search: '?https://github.com/curie-eng/curie/blob/main/docs/architecture-atlas/index.html',
  };
  let replacedUrl;
  const history = {
    replaceState(_state, _title, url) {
      replacedUrl = url;
      assert.equal(new URL(url).origin, location.origin);
    },
  };

  new Function('history', 'location', 'type', 'id', body)(history, location, 'node', 'runner');

  assert.equal(
    replacedUrl,
    'https://htmlpreview.github.io/?https://github.com/curie-eng/curie/blob/main/docs/architecture-atlas/index.html#node/runner',
  );
});
