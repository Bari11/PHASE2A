# three.bundle.js — build notes

`three.bundle.js` is Three.js r152 + `GLTFLoader` bundled into a single
self-contained script with esbuild. No `import`/`export` survives in
the output — everything is resolved at build time, not in the browser.

## Why this exists instead of a CDN `<script>` tag

Two separate things block the normal approaches in this project's
QtWebEngine environment (PyQtWebEngine 5.15.7, Chromium 87):

1. `examples/js/loaders/GLTFLoader.js` (the old non-module build) was
   **deleted from three.js entirely at r124** (Dec 2020) — it doesn't
   exist for any newer version, on any CDN. Not a version-pinning
   problem; the file is just gone.
2. `examples/jsm/loaders/GLTFLoader.js` (the modern ES module build)
   has its own internal `import ... from 'three'` — a bare specifier
   that can only resolve via `<script type="importmap">`, a feature
   that shipped in Chromium 89. This project's Chromium is 87.

Bundling ahead of time sidesteps both: esbuild resolves every import
(including the one inside GLTFLoader itself) while building, so the
file that actually reaches the browser has no imports left to resolve.
As a side benefit, the 3D environment no longer needs a live internet
connection at runtime at all.

## Rebuilding it

Only needed if you want to bump the Three.js version or add another
loader/addon.

```
npm install
npm run build
```

That regenerates `three.bundle.js` in this folder. Then just make sure
`ui/unplug/viewer.html` still references it the same way
(`<script src="../../assets/vendor/three.bundle.js"></script>`) — no
other changes needed downstream, since the bundle still exposes the
same `window.THREE` / `THREE.GLTFLoader` globals the rest of the code
expects.

## Adding another addon later (e.g. DRACOLoader, OrbitControls)

Add it to `entry.js`:

```js
import { DRACOLoader } from 'three/examples/jsm/loaders/DRACOLoader.js';
// ...
window.THREE = Object.assign({}, THREE_NS, { GLTFLoader, DRACOLoader });
```

Then `npm run build` again.
