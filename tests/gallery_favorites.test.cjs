const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const template = readFileSync(join(__dirname, '..', 'src', 'reply_gallery.html'), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];
const key = 'thread-foto-graph:reply-image-favorites:v1';

test('the published gallery uses the same favorites script as the template', () => {
  const preview = readFileSync(join(__dirname, '..', 'docs', 'reply-images.html'), 'utf8');
  assert.equal(preview.match(/<script>([\s\S]*?)<\/script>/)[1], script);
});

function sharedState(initial = []) {
  const data = new Map(initial.length ? [[key, JSON.stringify(initial)]] : []);
  let tail = Promise.resolve();
  return {
    data,
    locks: {
      request(name, action) {
        assert.equal(name, key);
        const pending = tail.then(action);
        tail = pending.catch(() => {});
        return pending;
      },
    },
    saved: () => JSON.parse(data.get(key) || '[]').sort(),
  };
}

function tab(shared, options = {}) {
  const handlers = {};
  const elements = {};
  const flags = { failWrite: false, failRemove: false };
  const button = (favorite = false) => ({
    dataset: {}, disabled: false, textContent: '',
    attributes: {},
    classList: { contains: name => favorite && name === 'favorite-toggle' },
    setAttribute(name, value) { this.attributes[name] = value; },
    hasAttribute() { return false; },
    closest() { return this; },
  });
  for (const id of [
    'favorite-view', 'copy-favorites', 'reset-favorites', 'status',
    'manual-copy', 'storage-status', 'favorite-count', 'no-favorites',
  ]) elements[id] = button();
  const cards = ['a', 'b'].map(id => {
    const favoriteButton = button(true);
    const card = {
      dataset: { photoId: id, replyUrl: `https://example.com/${id}` },
      hidden: false,
      classList: { toggle() {} },
      querySelector: () => favoriteButton,
    };
    favoriteButton.closest = selector => selector === 'article' ? card : favoriteButton;
    return card;
  });
  const thread = { hidden: false, querySelectorAll: () => cards };
  const topic = { hidden: false, querySelectorAll: () => [thread] };
  const storage = {
    getItem: name => shared.data.get(name) ?? null,
    setItem(name, value) {
      if (flags.failWrite) throw new Error('Storage full');
      shared.data.set(name, value);
    },
    removeItem(name) {
      if (flags.failRemove) throw new Error('Storage blocked');
      shared.data.delete(name);
    },
  };
  const context = vm.createContext({
    document: {
      getElementById: id => elements[id],
      querySelectorAll: selector => ({
        'article[data-reply-url]': cards,
        '.gallery-thread': [thread],
        '.gallery-topic': [topic],
        '.thumbnail img': [],
      })[selector],
      addEventListener: (event, handler) => { handlers[event] = handler; },
    },
    window: {
      localStorage: storage,
      addEventListener: (event, handler) => { handlers[event] = handler; },
    },
    navigator: { locks: options.noLocks ? undefined : shared.locks },
  });
  vm.runInContext(script, context);
  const click = target => {
    assert.equal(target.disabled, false, 'Control unexpectedly disabled');
    return handlers.click({ target });
  };
  return {
    flags, elements, cards,
    favorite: id => click(cards.find(card => card.dataset.photoId === id).querySelector()),
    reset: () => click(elements['reset-favorites']),
    synchronize: () => handlers.storage({ key }),
    selected: () => JSON.parse(vm.runInContext('JSON.stringify([...favorites].sort())', context)),
  };
}

test('concurrent additions preserve both picks and picks absent from both galleries', async () => {
  const shared = sharedState(['absent']);
  const a = tab(shared);
  const b = tab(shared);
  // Neither tab receives storage events before submitting its stale-state edit.
  await Promise.all([a.favorite('a'), b.favorite('b')]);
  assert.deepEqual(shared.saved(), ['a', 'absent', 'b']);
  a.synchronize();
  b.synchronize();
  assert.deepEqual(a.selected(), ['a', 'absent', 'b']);
  assert.deepEqual(b.selected(), ['a', 'absent', 'b']);
});

test('a stale tab cannot resurrect picks removed by another tab reset', async () => {
  const shared = sharedState(['a', 'absent']);
  const a = tab(shared);
  const b = tab(shared);
  await Promise.all([a.reset(), b.favorite('b')]);
  assert.deepEqual(shared.saved(), ['b']);
});

test('a reset ordered after another addition clears that addition too', async () => {
  const shared = sharedState(['a', 'absent']);
  const a = tab(shared);
  const b = tab(shared);
  await Promise.all([b.favorite('b'), a.reset()]);
  assert.deepEqual(shared.saved(), []);
});

test('a stale removal preserves unrelated concurrent additions', async () => {
  const shared = sharedState(['a']);
  const a = tab(shared);
  const b = tab(shared);
  await Promise.all([a.favorite('b'), b.favorite('a')]);
  assert.deepEqual(shared.saved(), ['b']);
});

test('missing coordination never writes an unprotected snapshot', async () => {
  const shared = sharedState(['absent']);
  const a = tab(shared, { noLocks: true });
  assert.match(a.elements['storage-status'].textContent, /Web Locks/);
  await a.favorite('a');
  assert.deepEqual(shared.saved(), ['absent']);
  assert.deepEqual(a.selected(), ['a', 'absent']);
  assert.match(a.elements['storage-status'].textContent, /Could not save/);
});

test('corrupt saved data is not overwritten by a toggle but can be reset', async () => {
  const shared = sharedState();
  shared.data.set(key, '{invalid');
  const a = tab(shared);
  await a.favorite('a');
  assert.equal(shared.data.get(key), '{invalid');
  assert.match(a.elements['storage-status'].textContent, /Could not save/);
  await a.reset();
  assert.equal(shared.data.has(key), false);
  assert.equal(a.elements['storage-status'].textContent, '');
});

test('failed writes and resets remain explicit and can be retried', async () => {
  const shared = sharedState(['absent']);
  const a = tab(shared);
  a.flags.failWrite = true;
  await a.favorite('a');
  assert.deepEqual(shared.saved(), ['absent']);
  assert.deepEqual(a.selected(), ['a', 'absent']);
  assert.match(a.elements['storage-status'].textContent, /Could not save/);
  a.flags.failRemove = true;
  await a.reset();
  assert.match(a.elements.status.textContent, /could not be reset/);
  a.flags.failRemove = false;
  await a.reset();
  assert.deepEqual(shared.saved(), []);
  assert.equal(a.elements.status.textContent, 'Favorites reset.');
});

test('a successful edit retries failed additions without dropping remote picks', async () => {
  const shared = sharedState(['absent']);
  const a = tab(shared);
  a.flags.failWrite = true;
  await a.favorite('a');
  shared.data.set(key, JSON.stringify(['absent', 'remote']));
  a.synchronize();
  assert.deepEqual(a.selected(), ['a', 'absent', 'remote']);
  assert.match(a.elements['storage-status'].textContent, /not saved/);
  a.flags.failWrite = false;
  await a.favorite('b');
  assert.deepEqual(shared.saved(), ['a', 'absent', 'b', 'remote']);
  assert.deepEqual(a.selected(), ['a', 'absent', 'b', 'remote']);
  assert.equal(a.elements['storage-status'].textContent, '');
});

test('a successful edit retries a failed removal instead of restoring it', async () => {
  const shared = sharedState(['a', 'absent']);
  const a = tab(shared);
  a.flags.failWrite = true;
  await a.favorite('a');
  a.flags.failWrite = false;
  await a.favorite('b');
  assert.deepEqual(shared.saved(), ['absent', 'b']);
  assert.deepEqual(a.selected(), ['absent', 'b']);
});

test('a successful edit retries a failed reset before adding the new pick', async () => {
  const shared = sharedState(['a', 'absent']);
  const a = tab(shared);
  a.flags.failRemove = true;
  await a.reset();
  a.flags.failRemove = false;
  await a.favorite('b');
  assert.deepEqual(shared.saved(), ['b']);
  assert.deepEqual(a.selected(), ['b']);
});

test('pending edits stay focusable but ignore more toggle or reset requests', async () => {
  const shared = sharedState(['absent']);
  const a = tab(shared);
  const pending = a.favorite('a');
  const button = a.cards[0].querySelector();
  assert.equal(button.disabled, false);
  assert.equal(button.attributes['aria-disabled'], 'true');
  const ignoredToggle = a.favorite('b');
  const ignoredReset = a.reset();
  await Promise.all([pending, ignoredToggle, ignoredReset]);
  assert.deepEqual(shared.saved(), ['a', 'absent']);
  assert.equal(button.attributes['aria-disabled'], 'false');
});

test('a failed pending save reconciles a remote reset without losing its own intent', async () => {
  const shared = sharedState(['a', 'absent']);
  let grantLock;
  shared.locks.request = (name, action) =>
    new Promise(resolve => { grantLock = resolve; }).then(action);
  const a = tab(shared);
  a.flags.failWrite = true;
  const pending = a.favorite('b');
  shared.data.delete(key);
  a.synchronize();
  grantLock();
  await pending;
  assert.deepEqual(shared.saved(), []);
  assert.deepEqual(a.selected(), ['b']);
  assert.match(a.elements['storage-status'].textContent, /Could not save/);
});
