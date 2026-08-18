import * as THREE_NS from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/examples/jsm/loaders/DRACOLoader.js';

// Everything below runs in the browser after bundling — no import/export
// statements survive in the output, so there is nothing left for the
// browser to resolve at runtime. This is what makes it work on an old
// Chromium (import maps aren't needed because there's no bare specifier
// left anywhere by the time this reaches the browser).
window.THREE = Object.assign({}, THREE_NS, { GLTFLoader, DRACOLoader });
