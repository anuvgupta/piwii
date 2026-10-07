// piwii chunked upload, shared by the Wii page and /legacy.
//
// A file goes up as ~50 MB chunks (the web server says how big), each its own
// request, so it fits under Cloudflare's 100 MB request limit. The web server
// writes each chunk straight into place, so there's no join step. If the
// connection drops or the page reloads, uploading the same file again resumes
// from what the web server already has. Files are uploaded one at a time.

(() => {
  // At home, send uploads straight to the web server over the home network instead of
  // out through Cloudflare and back. The LAN address (wii.lan.<domain>) only
  // resolves to something reachable at home; the login cookie is shared across
  // the domain, so it works on both. Probed once per page (also requires the
  // login to work there). The web server puts the hostnames in window.piwiiConfig.
  const cfg = window.piwiiConfig || {};
  const LAN_ORIGIN = cfg.lanHost ? `https://${cfg.lanHost}` : '';
  let base = null;
  async function pickBase() {
    if (base !== null) return base;
    base = '';
    if (LAN_ORIGIN && location.origin !== LAN_ORIGIN && location.hostname.endsWith(cfg.domain)) {
      try {
        const r = await fetch(LAN_ORIGIN + '/api/pi', { credentials: 'include', signal: AbortSignal.timeout(1500) });
        if (r.ok) base = LAN_ORIGIN;
      } catch {}
    }
    return base;
  }
  // Through Cloudflare only when on the public hostname and the LAN probe failed.
  window.piwiiUploadRoute = () => base === null ? '' : base ? 'home network' : location.hostname === cfg.publicHost ? 'internet' : 'home network';

  const keyFor = f => `piwii-upload:${f.name}:${f.size}:${f.lastModified}`;
  const store = {
    get: k => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
    del: k => { try { localStorage.removeItem(k); } catch {} },
  };
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  async function api(method, url, body) {
    const r = await fetch(base + url, { method, credentials: 'include', headers: body ? { 'Content-Type': 'application/json' } : {}, body: body && JSON.stringify(body) });
    const text = await r.text();
    let data; try { data = JSON.parse(text); } catch { data = { detail: text }; }
    if (!r.ok) throw Object.assign(new Error(data.detail || `HTTP ${r.status}`), { status: r.status });
    return data;
  }

  // PUT one chunk with XHR so we get byte-level progress within it.
  function putChunk(id, offset, blob, onBytes) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', `${base}/api/uploads/${id}?offset=${offset}`);
      xhr.withCredentials = true;
      xhr.upload.onprogress = e => onBytes(e.loaded);
      xhr.onload = () => {
        let data; try { data = JSON.parse(xhr.responseText); } catch { data = {}; }
        xhr.status < 300 ? resolve(data) : reject(Object.assign(new Error(data.detail || `HTTP ${xhr.status}`), { status: xhr.status }));
      };
      xhr.onerror = () => reject(Object.assign(new Error('network error'), { status: 0 }));
      xhr.send(blob);
    });
  }

  async function openUpload(file) {
    const saved = store.get(keyFor(file));
    if (saved) {
      try {
        const s = await api('GET', `/api/uploads/${saved}`);
        if (s.size === file.size) return s;  // resume
      } catch {}
      store.del(keyFor(file));
    }
    const s = await api('POST', '/api/uploads', { filename: file.name, size: file.size });
    store.set(keyFor(file), s.id);
    return s;
  }

  async function uploadOne(file, onProgress) {
    await pickBase();
    let up = await openUpload(file);
    let received = up.received;
    const report = extra => onProgress(Math.min(1, (received + extra) / file.size), received + extra, file.size);
    report(0);
    while (received < file.size) {
      const blob = file.slice(received, received + up.chunk_size);
      for (let attempt = 1; ; attempt++) {
        try {
          up = await putChunk(up.id, received, blob, n => report(n));
          received = up.received;
          break;
        } catch (e) {
          if (e.status === 409) { received = (await api('GET', `/api/uploads/${up.id}`)).received; break; }  // resync offset
          if (e.status === 404 || (e.status >= 400 && e.status < 500) || attempt >= 6) throw e;
          await sleep(Math.min(30, 2 * attempt) * 1000);  // network blip or 5xx: back off, resync, retry
          try {
            const now = (await api('GET', `/api/uploads/${up.id}`)).received;
            if (now !== received) { received = now; break; }  // web server's position moved: resend from there
          } catch {}
        }
      }
      report(0);
    }
    // Finishing can hit a web server restart too; retry it like a chunk. A 404
    // here after a lost response means the web server did take it.
    for (let attempt = 1; ; attempt++) {
      try {
        const job = await api('POST', `/api/uploads/${up.id}/complete`);
        store.del(keyFor(file));
        return job;
      } catch (e) {
        if (e.status === 404 && attempt > 1) { store.del(keyFor(file)); return null; }
        if ((e.status >= 400 && e.status < 500) || attempt >= 6) throw e;
        await sleep(Math.min(30, 2 * attempt) * 1000);
      }
    }
  }

  let chain = Promise.resolve();
  // Queue a file. hooks: onProgress(fraction, sent, total), onDone(job), onError(message).
  window.piwiiUpload = (file, hooks = {}) => {
    chain = chain.then(() => uploadOne(file, hooks.onProgress || (() => {}))
      .then(job => hooks.onDone && hooks.onDone(job))
      .catch(e => hooks.onError && hooks.onError(`${file.name}: ${e.message}`)));
    return chain;
  };
})();
