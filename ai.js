/* Shared AI panel for wifi_viewer.html and usb_viewer.html.
 *
 *   var ai = CamAI.mount(document.getElementById('aiMount'), {
 *     getCanvas: function () { return canvasElement; },   // the picture to analyze
 *     ready:     function () { return gotFrame; }          // true once a frame has been shown
 *   });
 *   ai.frame();   // call once for every frame the viewer displays (drives "every X frames")
 *
 * Classic script (no import/export) so it also loads when a page is opened from a file.
 * The models themselves are loaded on demand from cdn.jsdelivr.net (Transformers.js).
 */
(function () {
  'use strict';

  var CSS = [
    '.camai{display:flex;flex-direction:column;gap:12px}',
    '.camai .presets{display:grid;grid-template-columns:1fr 1fr;gap:8px}',
    '.camai .preset{flex-direction:column;align-items:flex-start;gap:2px;padding:10px 12px;white-space:normal;text-align:left}',
    '.camai .preset b{font-size:14px}',
    '.camai .preset small{color:var(--muted);font-size:11.5px;line-height:1.3}',
    '.camai .preset.on{border-color:var(--accent);background:rgba(59,130,246,.14)}',
    '.camai .sched{display:flex;flex-wrap:wrap;gap:8px 10px;align-items:center;padding:10px 12px;border:1px solid var(--line);',
    '  border-radius:8px;background:var(--panel2)}',
    '.camai .sched.on{border-color:var(--accent);background:rgba(59,130,246,.10)}',
    '.camai .sched input[type=number]{width:84px}',
    '.camai .sched .nx{margin-left:auto;font-variant-numeric:tabular-nums}',
    '.camai .msg{font-size:12px;color:var(--muted);min-height:16px}',
    '.camai canvas.res{max-width:100%;height:auto;border-radius:8px;border:1px solid var(--line)}',
    '.camai ul.out{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}',
    '.camai ul.out li{display:flex;gap:10px;align-items:center;background:var(--panel2);border:1px solid var(--line);',
    '  border-radius:8px;padding:8px 10px;font-size:13px}',
    '.camai ul.out img{width:48px;height:48px;object-fit:contain;background:#fff;border-radius:6px;flex:none}',
    '.camai ul.out .sc{margin-left:auto;color:#9be9a8;font-variant-numeric:tabular-nums;font-weight:600}',
    '.camai ul.out .err{color:#ffaba5}'
  ].join('\n');

  var HTML = [
    '<div>',
    '  <h2>What\'s in the image?</h2>',
    '  <p class="muted small" style="margin:2px 0 0">Click one to analyze the current frame, or tick <b>Run automatically</b>. Models run in your browser and download once.</p>',
    '</div>',
    '<div class="presets">',
    '  <button class="preset" data-engine="objects"><b>Objects</b><small>Finds and boxes things &middot; 7 MB</small></button>',
    '  <button class="preset" data-engine="caption"><b>Describe</b><small>One sentence &middot; 250 MB</small></button>',
    '  <button class="preset" data-engine="brick"><b>LEGO part</b><small>Exact part ID &middot; online</small></button>',
    '  <button class="preset" data-engine="lego"><b>LEGO family</b><small>Brick / plate / technic &middot; 90 MB offline</small></button>',
    '</div>',
    '<div class="sched" data-r="sched">',
    '  <label class="check"><input type="checkbox" data-r="auto"> Run automatically</label>',
    '  <span class="muted small">every</span>',
    '  <input type="number" data-r="every" min="1" max="100000" value="5">',
    '  <select data-r="unit"><option value="s">seconds</option><option value="f">frames</option></select>',
    '  <span class="nx muted small" data-r="next"></span>',
    '</div>',
    '<progress data-r="prog" max="100" value="0" hidden></progress>',
    '<div class="msg" data-r="msg"></div>',
    '<div class="small muted" data-r="info"></div>',
    '<canvas class="res" data-r="cv" hidden></canvas>',
    '<ul class="out" data-r="list"></ul>',
    '<details>',
    '  <summary>Advanced</summary>',
    '  <div class="row">',
    '    <label class="field grow"><span>Engine</span><select data-r="engine"></select></label>',
    '    <label class="field" data-r="deviceRow"><span>Run on</span>',
    '      <select data-r="device">',
    '        <option value="wasm">CPU (small q8 models)</option>',
    '        <option value="webgpu">GPU (WebGPU, bigger download)</option>',
    '      </select>',
    '    </label>',
    '  </div>',
    '  <div data-r="labelsRow" style="margin-top:10px" hidden>',
    '    <label class="field"><span>Labels (comma separated, editable)</span>',
    '      <textarea data-r="labels" spellcheck="false"></textarea></label>',
    '  </div>',
    '  <div class="row" data-r="customRow" style="margin-top:10px" hidden>',
    '    <label class="field grow"><span>Hugging Face model id (ONNX / Transformers.js)</span>',
    '      <input type="text" data-r="cModel" placeholder="user/your-lego-model" spellcheck="false"></label>',
    '    <label class="field"><span>Task</span>',
    '      <select data-r="cTask">',
    '        <option value="image-classification">image-classification</option>',
    '        <option value="zero-shot-image-classification">zero-shot-image-classification</option>',
    '        <option value="object-detection">object-detection</option>',
    '        <option value="image-to-text">image-to-text</option>',
    '      </select>',
    '    </label>',
    '  </div>',
    '  <p class="small muted" data-r="note" style="margin:10px 0"></p>',
    '  <div class="row center"><button data-r="analyze">Run selected engine now</button></div>',
    '</details>',
    '<pre class="log" data-r="log" style="max-height:140px;font-size:11.5px">Every step (library, download, model, result) is listed here.</pre>'
  ].join('\n');

  var GENERIC_LABELS = 'person, hand, face, cat, dog, car, bottle, cup, phone, laptop, keyboard, book, chair, table, plant, bag, tool, toy, food, LEGO piece';
  var LEGO_LABELS = [
    '1x1 brick', '1x2 brick', '1x4 brick', '2x2 brick', '2x3 brick', '2x4 brick', 'round brick',
    '1x1 plate', '1x2 plate', '1x4 plate', '2x2 plate', '2x4 plate', 'round plate', 'wedge plate',
    '1x1 tile', '1x2 tile', '2x2 tile', 'slope brick', 'curved slope', 'arch brick',
    'technic beam', 'technic brick with holes', 'technic axle', 'technic pin', 'technic gear',
    'technic connector', 'wheel', 'tire', 'hinge', 'bracket', 'window frame', 'door',
    'minifigure', 'minifigure head', 'minifigure torso', 'minifigure legs', 'motor', 'hub', 'sensor'
  ].join(', ');

  var ENGINES = {
    objects: { label: 'Objects: YOLOS-tiny (~7 MB, local)', kind: 'detect', task: 'object-detection', model: 'Xenova/yolos-tiny',
               note: 'Finds and boxes common objects (80 COCO classes). Smallest and fastest.' },
    caption: { label: 'Describe: ViT-GPT2 caption (~250 MB, local)', kind: 'caption', task: 'image-to-text', model: 'Xenova/vit-gpt2-image-captioning',
               note: 'Writes one sentence about the whole image.' },
    clip:    { label: 'Custom labels: CLIP (~90 MB, local)', kind: 'clip', task: 'zero-shot-image-classification', model: 'Xenova/clip-vit-base-patch32',
               template: 'a photo of a {}.', labels: 'generic',
               note: 'Ranks YOUR labels against the frame. Edit the label list freely.' },
    lego:    { label: 'LEGO family: CLIP with LEGO labels (local)', kind: 'clip', task: 'zero-shot-image-classification', model: 'Xenova/clip-vit-base-patch32',
               template: 'a photo of a LEGO {}.', labels: 'lego',
               note: 'Tells part families apart (brick / plate / tile / technic / minifig). It cannot read exact stud counts or part numbers: use "LEGO part" for that.' },
    brick:   { label: 'LEGO part: Brickognize (online, exact IDs)', kind: 'brickognize',
               note: 'Dedicated LEGO recognition service: returns part number + name. Sends the frame to api.brickognize.com (at most one frame every 3 s). Put one piece on a plain background, close to the lens.' },
    custom:  { label: 'Custom Hugging Face model (your own LEGO model)', kind: 'custom',
               note: 'Any Transformers.js-compatible ONNX model. Paste a fine-tuned LEGO classifier id here when you have one.' }
  };

  var LS_SCHED = 'esp32cam.ai.sched', LS_ENGINE = 'esp32cam.engine', LS_MODEL = 'esp32cam.cmodel';
  var BRICK_GAP_MS = 3000;     // never hit the online service faster than this
  var MAX_AUTO_ERRORS = 3;     // automatic runs stop after this many failures in a row

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function mb(n) { return (n / 1048576).toFixed(1); }

  var cssDone = false;
  function addCss() {
    if (cssDone) return;
    cssDone = true;
    var st = document.createElement('style');
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  // One model stays in memory for the whole page, shared by every mount.
  var tf = null, curKey = '', curPipe = null;

  function mount(root, opts) {
    addCss();
    root.classList.add('camai');
    root.innerHTML = HTML;
    var q = function (n) { return root.querySelector('[data-r="' + n + '"]'); };
    var el = {};
    ['sched', 'auto', 'every', 'unit', 'next', 'prog', 'msg', 'info', 'cv', 'list', 'engine', 'device', 'deviceRow',
     'labelsRow', 'labels', 'customRow', 'cModel', 'cTask', 'note', 'analyze', 'log'].forEach(function (n) { el[n] = q(n); });
    var presets = root.querySelectorAll('.preset');

    var busy = false, frameCount = 0, lastStart = 0, lastSecs = 0, runs = 0, autoErrors = 0, brickOk = false;
    var logLines = [];
    var labelStore = { generic: GENERIC_LABELS, lego: LEGO_LABELS };
    var curLabelKey = null;

    /* ---------- log ---------- */
    function log(m) {
      console.log('[ai]', m);
      logLines.push(new Date().toLocaleTimeString() + '  ' + m);
      if (logLines.length > 80) logLines.shift();
      el.log.textContent = logLines.join('\n');
      el.log.scrollTop = el.log.scrollHeight;
    }

    /* ---------- engines ---------- */
    Object.keys(ENGINES).forEach(function (k) {
      var o = document.createElement('option');
      o.value = k; o.textContent = ENGINES[k].label;
      el.engine.appendChild(o);
    });
    if (!('gpu' in navigator)) el.device.querySelector('option[value="webgpu"]').disabled = true;

    function onEngine() {
      var e = ENGINES[el.engine.value];
      if (curLabelKey) labelStore[curLabelKey] = el.labels.value;
      curLabelKey = e.labels || null;
      el.labelsRow.hidden = !(e.kind === 'clip' || (e.kind === 'custom' && el.cTask.value === 'zero-shot-image-classification'));
      if (e.labels) el.labels.value = labelStore[e.labels];
      el.customRow.hidden = e.kind !== 'custom';
      el.deviceRow.hidden = e.kind === 'brickognize';
      el.note.textContent = e.note;
      presets.forEach(function (b) { b.classList.toggle('on', b.dataset.engine === el.engine.value); });
      lsSet(LS_ENGINE, el.engine.value);
    }
    el.engine.addEventListener('change', function () {
      onEngine();
      if (el.auto.checked && !confirmOnline()) { el.auto.checked = false; saveSched(); }
    });
    el.cTask.addEventListener('change', function () {
      el.labelsRow.hidden = el.cTask.value !== 'zero-shot-image-classification';
      if (!el.labels.value.trim()) el.labels.value = labelStore.generic;
    });
    presets.forEach(function (b) {
      b.addEventListener('click', function () {
        el.engine.value = b.dataset.engine;
        onEngine();
        if (el.auto.checked && !confirmOnline()) { el.auto.checked = false; saveSched(); }
        analyze(false);
      });
    });
    var savedEngine = lsGet(LS_ENGINE);
    if (savedEngine && ENGINES[savedEngine]) el.engine.value = savedEngine;
    var savedModel = lsGet(LS_MODEL);
    if (savedModel) el.cModel.value = savedModel;
    onEngine();

    /* ---------- model loading ---------- */
    async function loadTf() {
      if (tf) return tf;
      var t0 = performance.now();
      el.msg.textContent = 'Loading AI library...';
      log('Loading the AI library from cdn.jsdelivr.net (Transformers.js)...');
      try {
        tf = await import('https://cdn.jsdelivr.net/npm/@huggingface/transformers@3');
      } catch (e) {
        throw new Error('Could not load the AI library (network blocked?): ' + (e && e.message ? e.message : e));
      }
      tf.env.allowLocalModels = false;
      log('AI library loaded in ' + ((performance.now() - t0) / 1000).toFixed(1) + ' s');
      return tf;
    }

    var dl = {};     // file -> { loaded, total, step } for the model being loaded
    function onProgress(p) {
      if (!p) return;
      var f = p.file || p.name || 'model';
      if (p.status === 'initiate') {
        log('Fetching ' + f);
      } else if (p.status === 'progress') {
        var d = dl[f] || (dl[f] = { loaded: 0, total: 0, step: -1 });
        d.loaded = p.loaded || 0; d.total = p.total || 0;
        el.prog.hidden = false;
        el.prog.value = p.progress || 0;
        el.msg.textContent = 'Downloading ' + f.split('/').pop() + '  ' + mb(d.loaded) + ' / ' + mb(d.total) + ' MB (' + Math.round(p.progress || 0) + '%)';
        var step = Math.floor((p.progress || 0) / 25);
        if (step !== d.step) { d.step = step; log('  ' + f.split('/').pop() + ': ' + mb(d.loaded) + ' / ' + mb(d.total) + ' MB'); }
      } else if (p.status === 'done') {
        log('Downloaded ' + f);
      } else if (p.status === 'ready') {
        log('Model ready in memory');
      }
    }

    async function getPipe(task, model, device) {
      var key = task + '|' + model + '|' + device;
      if (curPipe && curKey === key) return curPipe;
      if (curPipe) {                                   // free the previous model before loading the next
        el.msg.textContent = 'Freeing the previous model...';
        log('Freeing the previous model...');
        try { await curPipe.dispose(); } catch (e) {}
        curPipe = null; curKey = '';
        await new Promise(function (r) { setTimeout(r, 150); });
      }
      var T = await loadTf();
      for (var k in dl) delete dl[k];
      var t0 = performance.now();
      el.msg.textContent = 'Loading model ' + model + '...';
      log('Loading model ' + model + ' (' + task + ', ' + (device === 'webgpu' ? 'GPU fp32' : 'CPU q8') + ')...');
      curPipe = await T.pipeline(task, model, {
        device: device,
        dtype: device === 'webgpu' ? 'fp32' : 'q8',
        progress_callback: onProgress
      });
      curKey = key;
      el.prog.hidden = true;
      var total = Object.keys(dl).reduce(function (a, k2) { return a + (dl[k2].total || 0); }, 0);
      var secs = ((performance.now() - t0) / 1000).toFixed(1);
      el.info.textContent = 'Model: ' + model + '  |  ' + task + '  |  ' + (device === 'webgpu' ? 'GPU fp32' : 'CPU q8') +
        '  |  loaded in ' + secs + ' s  |  ' + (total ? mb(total) + ' MB downloaded' : 'from browser cache');
      log('Model loaded in ' + secs + ' s (' + (total ? mb(total) + ' MB downloaded' : 'from browser cache') + ')');
      return curPipe;
    }

    function parseLabels() {
      var l = el.labels.value.split(/[,\n]/).map(function (s) { return s.trim(); }).filter(Boolean);
      if (l.length < 2) throw new Error('Give at least 2 labels.');
      return l;
    }

    function normalize(out) {
      var arr = Array.isArray(out) ? out : [out];
      return arr.map(function (it) {
        if (it && it.generated_text !== undefined) return { label: it.generated_text };
        return { label: it.label, score: it.score, box: it.box };
      });
    }

    async function brickognize(url) {
      var blob = await (await fetch(url)).blob();
      var fd = new FormData();
      fd.append('query_image', blob, 'frame.jpg');
      var r;
      try {
        r = await fetch('https://api.brickognize.com/predict/parts/', { method: 'POST', body: fd });
      } catch (e) {
        throw new Error('Brickognize unreachable or blocked by the browser (' + e.message + '). Try "LEGO family" (offline) instead.');
      }
      if (!r.ok) throw new Error('Brickognize HTTP ' + r.status);
      var j = await r.json();
      var items = (j.items || []).slice(0, 5);
      if (!items.length) return [{ label: 'No part recognised. Try one piece, plain background, closer.' }];
      return items.map(function (it) {
        var ext = it.external_sites && it.external_sites[0] && it.external_sites[0].url;
        return {
          label: it.id + '  ' + (it.name || ''),
          score: it.score,
          img: it.img_url,
          link: ext && /^https:\/\//.test(ext) ? ext : ''
        };
      });
    }

    async function runEngine(key, url) {
      var e = ENGINES[key], dev = el.device.value, p;
      if (e.kind === 'brickognize') { el.msg.textContent = 'Asking Brickognize...'; log('Sending the frame to api.brickognize.com...'); return brickognize(url); }
      if (e.kind === 'detect') {
        p = await getPipe(e.task, e.model, dev);
        el.msg.textContent = 'Analyzing...'; log('Running the model on the frame...');
        return normalize(await p(url, { threshold: 0.5, percentage: true }));
      }
      if (e.kind === 'caption') {
        p = await getPipe(e.task, e.model, dev);
        el.msg.textContent = 'Analyzing...'; log('Running the model on the frame...');
        return normalize(await p(url, { max_new_tokens: 40 }));
      }
      if (e.kind === 'clip') {
        p = await getPipe(e.task, e.model, dev);
        el.msg.textContent = 'Analyzing...'; log('Running the model on the frame...');
        var out = await p(url, parseLabels(), { hypothesis_template: e.template });
        return normalize(out).sort(function (a, b) { return b.score - a.score; }).slice(0, 5);
      }
      var model = el.cModel.value.trim(), task = el.cTask.value;
      if (!model) throw new Error('Enter a model id (Advanced).');
      lsSet(LS_MODEL, model);
      p = await getPipe(task, model, dev);
      el.msg.textContent = 'Analyzing...'; log('Running the model on the frame...');
      var res;
      if (task === 'object-detection') res = await p(url, { threshold: 0.5, percentage: true });
      else if (task === 'zero-shot-image-classification') res = await p(url, parseLabels());
      else if (task === 'image-to-text') res = await p(url, { max_new_tokens: 40 });
      else res = await p(url, { top_k: 5 });
      return normalize(res).slice(0, 8);
    }

    /* ---------- results ---------- */
    function showResults(items, snap) {
      var hasBox = items.some(function (i) { return i.box; });
      el.cv.hidden = !hasBox;
      if (hasBox) {
        el.cv.width = snap.width; el.cv.height = snap.height;
        var c = el.cv.getContext('2d');
        c.drawImage(snap, 0, 0);
        c.lineWidth = 2; c.font = '13px system-ui,Arial'; c.textBaseline = 'top';
        items.forEach(function (it) {
          if (!it.box) return;
          var b = it.box, fr = b.xmax <= 1.01 && b.ymax <= 1.01;
          var sx = fr ? snap.width : 1, sy = fr ? snap.height : 1;
          var x = b.xmin * sx, y = b.ymin * sy, w = (b.xmax - b.xmin) * sx, h = (b.ymax - b.ymin) * sy;
          c.strokeStyle = '#4cd964'; c.strokeRect(x, y, w, h);
          var t = it.label + (it.score != null ? ' ' + Math.round(it.score * 100) + '%' : '');
          var tw = c.measureText(t).width + 6;
          c.fillStyle = '#4cd964'; c.fillRect(x, Math.max(0, y - 16), tw, 16);
          c.fillStyle = '#000'; c.fillText(t, x + 3, Math.max(0, y - 15));
        });
      }
      el.list.textContent = '';
      if (!items.length) items = [{ label: 'Nothing found above the 50% threshold.' }];
      items.forEach(function (it) {
        var li = document.createElement('li');
        if (it.img) { var im = document.createElement('img'); im.src = it.img; im.alt = ''; li.appendChild(im); }
        var span = document.createElement('span');
        if (it.link) {
          var a = document.createElement('a');
          a.href = it.link; a.target = '_blank'; a.rel = 'noopener'; a.textContent = it.label;
          span.appendChild(a);
        } else {
          span.textContent = it.label;
        }
        li.appendChild(span);
        if (it.score != null) {
          var sc = document.createElement('span');
          sc.className = 'sc';
          sc.textContent = Math.round(it.score * 100) + '%';
          li.appendChild(sc);
        }
        el.list.appendChild(li);
      });
    }

    function showError(msg) {
      el.cv.hidden = true;
      el.list.textContent = '';
      var li = document.createElement('li');
      var s = document.createElement('span');
      s.className = 'err';
      s.textContent = msg;
      li.appendChild(s);
      el.list.appendChild(li);
    }

    function setButtons(disabled) {
      el.analyze.disabled = disabled;
      presets.forEach(function (b) { b.disabled = disabled; });
    }

    /* ---------- one analysis ---------- */
    async function analyze(fromSchedule) {
      if (busy) { if (!fromSchedule) log('Still working on the previous request...'); return; }
      if (!opts.ready()) {
        if (!fromSchedule) { showError('No frame yet: connect to the camera first.'); log('No frame yet: connect to the camera first.'); }
        return;
      }
      var src = opts.getCanvas();
      if (!src || !src.width || !src.height) return;
      busy = true;
      frameCount = 0;
      lastStart = performance.now();
      logLines = [];
      setButtons(true);
      var snap = document.createElement('canvas');
      snap.width = src.width; snap.height = src.height;
      snap.getContext('2d').drawImage(src, 0, 0);
      var url = snap.toDataURL('image/jpeg', 0.92);
      var key = el.engine.value;
      runs++;
      log('Run #' + runs + (fromSchedule ? ' (automatic)' : ' (manual)') + '  engine: ' + ENGINES[key].label);
      log('Frame ' + snap.width + 'x' + snap.height + ' captured');
      el.msg.textContent = 'Starting...';
      var t0 = performance.now();
      var tick = setInterval(function () {              // elapsed time while nothing is downloading
        if (!el.prog.hidden) return;
        el.msg.textContent = el.msg.textContent.replace(/\s*\(\d+ s\)$/, '') + ' (' + Math.round((performance.now() - t0) / 1000) + ' s)';
      }, 1000);
      try {
        var items = await runEngine(key, url);
        showResults(items, snap);
        lastSecs = (performance.now() - t0) / 1000;
        log('Done in ' + lastSecs.toFixed(1) + ' s: ' + (items.length
          ? items.slice(0, 3).map(function (i) { return i.label + (i.score != null ? ' ' + Math.round(i.score * 100) + '%' : ''); }).join(' | ')
          : 'nothing found'));
        el.msg.textContent = 'Run #' + runs + ' done in ' + lastSecs.toFixed(1) + ' s  (' + new Date().toLocaleTimeString() + ')';
        autoErrors = 0;
      } catch (e) {
        var m = e && e.message ? e.message : String(e);
        showError('AI error: ' + m);
        log('ERROR: ' + m);
        el.msg.textContent = '';
        if (fromSchedule && ++autoErrors >= MAX_AUTO_ERRORS) {
          el.auto.checked = false;
          saveSched();
          log('Automatic runs stopped after ' + MAX_AUTO_ERRORS + ' errors in a row.');
          showError('Automatic runs stopped after ' + MAX_AUTO_ERRORS + ' errors in a row: ' + m);
        }
      } finally {
        clearInterval(tick);
        el.prog.hidden = true;
        busy = false;
        setButtons(false);
      }
    }
    el.analyze.addEventListener('click', function () { analyze(false); });

    /* ---------- schedule: every X seconds or every X frames ---------- */
    function schedN() {
      var n = parseInt(el.every.value, 10);
      if (!(n >= 1)) n = 1;
      return Math.min(100000, n);
    }

    function saveSched() {
      lsSet(LS_SCHED, JSON.stringify({ on: el.auto.checked, n: schedN(), unit: el.unit.value }));
      el.sched.classList.toggle('on', el.auto.checked);
      if (!el.auto.checked) el.next.textContent = '';
    }

    // Sending frames to an online service on a timer needs a clear yes, once per page.
    function confirmOnline() {
      if (ENGINES[el.engine.value].kind !== 'brickognize' || brickOk) return true;
      brickOk = window.confirm('"LEGO part" sends the camera frame to api.brickognize.com every time it runs.\n\n' +
        'Run it automatically (at most one frame every 3 s)?');
      return brickOk;
    }

    function onSchedChange() {
      if (el.auto.checked) {
        if (!confirmOnline()) { el.auto.checked = false; saveSched(); return; }
        autoErrors = 0;
        lastStart = 0;                      // first automatic run starts right away
        frameCount = schedN();
      }
      saveSched();
    }
    el.auto.addEventListener('change', onSchedChange);
    el.unit.addEventListener('change', function () { frameCount = 0; saveSched(); });
    el.every.addEventListener('change', function () { el.every.value = schedN(); saveSched(); });

    try {
      var s0 = JSON.parse(lsGet(LS_SCHED) || '{}');
      if (s0.n >= 1) el.every.value = Math.min(100000, Math.round(s0.n));
      if (s0.unit === 'f' || s0.unit === 's') el.unit.value = s0.unit;
      // never start sending frames by itself after a page load: the person turns it on
    } catch (e) {}
    saveSched();

    setInterval(function () {
      if (!el.auto.checked) return;
      if (!opts.ready()) { el.next.textContent = 'waiting for the camera'; return; }
      if (busy) { el.next.textContent = 'model running...'; return; }
      var now = performance.now(), n = schedN();
      var gap = ENGINES[el.engine.value].kind === 'brickognize' ? BRICK_GAP_MS : 0;
      var sinceStart = lastStart ? now - lastStart : 1e9;
      var left;
      if (el.unit.value === 's') {
        left = Math.max(n * 1000 - sinceStart, gap - sinceStart);
        if (left > 0) { el.next.textContent = 'next in ' + Math.ceil(left / 1000) + ' s'; return; }
      } else {
        var fl = n - frameCount;
        if (fl > 0) { el.next.textContent = 'next in ' + fl + ' frame' + (fl === 1 ? '' : 's'); return; }
        if (gap - sinceStart > 0) { el.next.textContent = 'next in ' + Math.ceil((gap - sinceStart) / 1000) + ' s'; return; }
      }
      el.next.textContent = 'running...';
      analyze(true);
    }, 250);

    return {
      frame: function () { frameCount++; },
      analyze: function () { return analyze(false); }
    };
  }

  window.CamAI = { mount: mount };
})();
