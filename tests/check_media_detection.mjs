// Actual classic content scripts, with only browser/DOM delivery replaced.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { webcrypto } from 'node:crypto';
import vm from 'node:vm';
import { test } from 'node:test';

const plain = value => JSON.parse(JSON.stringify(value));
const signed = 'https://cdn.fixture.invalid/file.mp4?Sig=A%2fb+Z&part=2&part=1';

function element(tag, attributes = {}, children = []) {
  return { tagName: tag.toUpperCase(), localName: tag, children, currentSrc: '',
    offsetWidth: 32, offsetHeight: 24,
    get src() { return this.attributes.src || ''; },
    get href() { return this.attributes.href || ''; },
    attributes,
    getAttribute(name) { return this.attributes[name] ?? null; },
    querySelectorAll(selector) { return children.filter(child => selector.includes(child.localName)); },
  };
}

function environment(browser, nodes = [], page = 'https://page.fixture.invalid/watch', { body = true } = {}) {
  const document = new EventTarget();
  document.title = 'বাংলা display title';
  document.body = body ? {} : null;
  document.documentElement = {};
  document.querySelectorAll = selector => nodes.filter(node => selector.split(',').some(part => {
    const tag = part.trim().split(/[:[.]/u)[0];
    return node.localName === tag;
  }));
  const window = new EventTarget();
  const messages = [], timers = new Map(), intervals = new Map(), observers = [];
  const receivers = [];
  let nextTimer = 0;
  const context = vm.createContext({ document, window, location: new URL(page), URL, console, crypto: webcrypto,
    chrome: { runtime: { id: browser === 'chrome' ? 'chrome-fixture' : 'firefox-fixture',
      onMessage: { addListener(listener) { receivers.push(listener); } },
      sendMessage(value, callback) { messages.push(plain(value)); callback?.({ success: true }); } } },
    MutationObserver: class {
      constructor(callback) { this.callback = callback; observers.push(this); }
      observe(target, options) { assert.ok(target); this.options = options; }
      disconnect() {}
    },
    setTimeout(callback) { timers.set(++nextTimer, callback); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback) { intervals.set(++nextTimer, callback); return nextTimer; },
    clearInterval(id) { intervals.delete(id); },
    fetch() { throw new Error('Detection must not probe a page or fetch a manifest'); },
  });
  window.location = context.location;
  context.globalThis = context;
  const manifest = JSON.parse(readFileSync('extension/' + (browser === 'chrome' ? 'manifest.json' : 'manifest.firefox.json')));
  const scripts = manifest.content_scripts[0].js.filter(path => !path.endsWith('overlay.js'));
  scripts.forEach(path => vm.runInContext(readFileSync('extension/' + path, 'utf8'), context, { filename: path }));
  const flush = () => { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(callback => callback()); };
  const scan = () => { observers[0].callback([{ type: 'attributes', attributeName: 'src' }]); flush(); };
  const navigate = value => { context.location = new URL(value); window.location = context.location;
    window.dispatchEvent(new Event('popstate')); intervals.forEach(callback => callback()); flush(); };
  const receive = (message, sender) => {
    let result;
    for (const listener of receivers) listener(message, sender, value => { result = plain(value); });
    return result;
  };
  return { context, messages, nodes, document, observers, scan, navigate, flush, receive,
    videos: () => messages.filter(message => message.type === 'video_detected').at(-1)?.videos || [] };
}

for (const browser of ['chrome', 'firefox']) {
  test(browser + ': signed direct media keeps literal bytes and page context', () => {
    const video = element('video', { src: signed });
    video.currentSrc = signed;
    const e = environment(browser, [video]);
    assert.equal(e.videos().length, 1, 'Small players remain discoverable');
    const candidate = e.videos()[0];
    assert.equal(candidate.media_url, signed);
    assert.equal(candidate.url, signed);
    assert.equal(candidate.frame_url, e.context.location.href);
    assert.equal(candidate.title, e.document.title, 'Display metadata must preserve Bangla');
    assert.equal(candidate.source, 'element');
  });

  test(browser + ': audio and source alternatives retain observed MIME types', () => {
    const source = element('source', { src: '/song.ogg?token=x%2Fy', type: 'audio/ogg; codecs=opus' });
    const e = environment(browser, [element('audio', {}, [source]), element('video', {}, [
      element('source', { src: '/stream.m3u8?a=1&a=2', type: 'application/vnd.apple.mpegurl' }),
    ])]);
    assert.equal(e.videos().length, 2);
    assert.equal(e.videos()[0].media_url, 'https://page.fixture.invalid/song.ogg?token=x%2Fy');
    assert.equal(e.videos()[0].kind, 'audio');
    assert.equal(e.videos()[0].observed_content_type, 'audio/ogg');
    assert.equal(e.videos()[1].kind, 'hls');
  });

  test(browser + ': duplicate element/source evidence merges without erasing MIME', () => {
    const source = element('source', { src: signed, type: 'video/mp4' });
    const video = element('video', { src: signed }, [source]);
    video.currentSrc = signed;
    const e = environment(browser, [video, element('video', { src: signed })]);
    assert.equal(e.videos().length, 1);
    assert.equal(e.videos()[0].observed_content_type, 'video/mp4');
  });

  test(browser + ': media links and page metadata add bounded explicit hints', () => {
    const e = environment(browser, [element('a', { href: '/sound.mp3?sig=two' }),
      element('a', { href: '/ordinary-page' }), element('meta', { property: 'og:video', content: signed }),
      element('meta', { name: 'unrelated', content: 'https://fixture.invalid/no.mp4' })]);
    assert.equal(e.videos().length, 2);
    assert.equal(e.videos()[0].kind, 'audio');
    assert.equal(e.videos()[1].source, 'metadata');
  });

  test(browser + ': known extractor pages use hostname boundaries', () => {
    assert.equal(environment(browser, [], 'https://youtube.com.attacker.invalid/watch').videos().length, 0);
    const e = environment(browser, [], 'https://m.youtube.com/watch?v=x&list=y');
    assert.equal(e.videos().length, 1);
    assert.equal(e.videos()[0].source, 'page');
  });

  test(browser + ': blob is a browser hint with a page probe fallback', () => {
    const blob = 'blob:https://page.fixture.invalid/fixture-stream';
    const e = environment(browser, [element('video', { src: blob })]);
    assert.equal(e.videos().length, 1);
    assert.equal(e.videos()[0].media_url, blob);
    assert.equal(e.videos()[0].url, e.context.location.href);
    assert.equal(e.videos()[0].browser_local, true);
  });

  test(browser + ': source changes and SPA navigation replace the current report', () => {
    const video = element('video', { src: signed });
    const e = environment(browser, [video]);
    assert.equal(e.observers[0].options.attributes, true);
    video.attributes.src = '/new.mp4';
    e.scan();
    assert.equal(e.videos()[0].media_url, 'https://page.fixture.invalid/new.mp4');
    e.nodes.length = 0;
    e.navigate('https://page.fixture.invalid/next');
    assert.deepEqual(e.videos(), []);
    assert.equal(e.messages.at(-1).frame_url, 'https://page.fixture.invalid/next');
    assert.ok(e.messages.at(-1).sequence > e.messages[0].sequence);
  });

  test(browser + ': reports and DOM work are bounded without automatic probes', () => {
    const e = environment(browser, Array.from({ length: 600 }, (_, index) => element('video', { src: signed + '&item=' + index })));
    assert.equal(e.videos().length, 128);
    const count = e.messages.length;
    e.scan(); e.scan();
    assert.equal(e.messages.length, count, 'Unchanged observations should not message the background again');
  });

  test(browser + ': media elements remain detectable after a large navigation link list', () => {
    const links = Array.from({ length: 600 }, (_, index) => element('a', { href: '/page-' + index }));
    const e = environment(browser, [...links, element('video', { src: signed })]);
    assert.equal(e.videos()[0]?.media_url, signed);
  });

  test(browser + ': content scripts start before a body exists and run in all frames', () => {
    const e = environment(browser, [element('audio', { src: signed })], undefined, { body: false });
    assert.equal(e.videos().length, 1);
    const manifest = JSON.parse(readFileSync('extension/' + (browser === 'chrome' ? 'manifest.json' : 'manifest.firefox.json')));
    assert.equal(manifest.content_scripts[0].all_frames, true);
  });

  test(browser + ': live frame evidence binds the current document and visible referrer policy', () => {
    const e = environment(browser, [element('video', { src: signed }), element('meta', { name: 'referrer', content: 'no-referrer' })]);
    const result = e.receive({ type: 'vidgrab.frame_context' }, { id: e.context.chrome.runtime.id });
    assert.equal(result?.document_token, e.messages[0].document_token);
    assert.equal(result.frame_url, e.context.location.href);
    assert.equal(result.referrer_policy, 'no-referrer');
    assert.equal(e.receive({ type: 'vidgrab.frame_context' }, { id: 'another-extension' }), undefined);
    assert.equal(e.receive({ type: 'vidgrab.frame_context' }, { id: e.context.chrome.runtime.id, tab: { id: 0 } }), undefined);
  });

  test(browser + ': referrers require policy evidence and never expose paths or signed queries', () => {
    const e = environment(browser);
    const referer = e.context.vidGrabMedia.referrer;
    assert.equal(typeof referer, 'function');
    for (const policy of [null, 'no-referrer', 'unknown', 'same-origin']) {
      assert.equal(referer('https://page.fixture.invalid/private?secret=token#anchor', signed, policy), null);
    }
    for (const policy of ['origin', 'strict-origin', 'strict-origin-when-cross-origin', 'unsafe-url']) {
      assert.equal(referer('https://page.fixture.invalid/private?secret=token#anchor', signed, policy), 'https://page.fixture.invalid/');
      assert.equal(referer('https://page.fixture.invalid/private', 'http://cdn.fixture.invalid/media.mp4', policy), null);
    }
    assert.equal(referer('https://page.fixture.invalid/private?secret=token', 'https://page.fixture.invalid/media.mp4', 'same-origin'),
      'https://page.fixture.invalid/');
  });
}
