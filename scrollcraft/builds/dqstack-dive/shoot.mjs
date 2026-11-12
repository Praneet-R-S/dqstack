// Photograph the landing page at chosen scroll positions with headless Chrome (DevTools
// protocol over Node's built-in WebSocket; no packages needed).
//
//   node shoot.mjs <url> <outdir> <width> <height> [mobile] [rm]
//
// Positions are [section id, position in that section's own vh units].
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const [url, out, w = "1440", h = "900", ...flags] = process.argv.slice(2);
const mobile = flags.includes("mobile"), rm = flags.includes("rm");
mkdirSync(out, { recursive: true });

const SHOTS = [
  ["dive", 0], ["dive", 70], ["dive", 120], ["dive", 175], ["dive", 240], ["dive", 300],
  ["dive", 340], ["dive", 380], ["dive", 420], ["dive", 445], ["dive", 475], ["dive", 510],
  ["dive", 545], ["dive", 580], ["dive", 625], ["dive", 690], ["dive", 730], ["dive", 760],
  ["dive", 800], ["dive", 830], ["dive", 860], ["dive", 900], ["dive", 950],
  ["how", 20], ["how", 120], ["how", 220],
  ["peak", 0], ["peak", 60], ["peak", 130], ["peak", 190], ["peak", 250], ["peak", 290],
  ["peak", 320], ["peak", 350], ["peak", 400],
  ["team", 30], ["team", 120], ["ready", 0], ["questions", 0],
];

const chromePath = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const port = 9300 + Math.floor(Math.random() * 500);
const profile = join(tmpdir(), "dq-shoot-" + port);
const chrome = spawn(chromePath, [
  "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  `--window-size=${w},${h}`, "--hide-scrollbars", "--disable-gpu-sandbox", "--no-first-run",
  "about:blank"], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws, id = 0;
const pending = new Map();
function send(method, params = {}) {
  return new Promise((res, rej) => {
    const i = ++id; pending.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method, params }));
  });
}
async function main() {
  let target;
  for (let k = 0; k < 50 && !target; k++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      target = list.find((t) => t.type === "page");
    } catch { await sleep(200); }
  }
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  ws.addEventListener("message", (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
  });
  await send("Page.enable"); await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: +w, height: +h, deviceScaleFactor: 1, mobile });
  if (mobile) await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
  if (rm) await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  const errors = [];
  await send("Log.enable");
  ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || "exception"); if (m.method === "Log.entryAdded" && m.params.entry.level === "error") errors.push(m.params.entry.text); });
  await send("Page.navigate", { url });
  await sleep(flags.includes("perf") ? 6000 : 3500);
  const evals = async (expr) => (await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result.value;
  if (flags.includes("perf")) {
    const r = await evals(`new Promise(res=>{const total=document.getElementById('team').offsetTop;const t=[];let last=performance.now(),y=0;function f(now){t.push(now-last);last=now;y+=24;scrollTo(0,y);if(y<total)requestAnimationFrame(f);else{t.shift();t.sort((a,b)=>a-b);res({frames:t.length,median:+t[t.length>>1].toFixed(1),p95:+t[Math.floor(t.length*.95)].toFixed(1),p99:+t[Math.floor(t.length*.99)].toFixed(1),over33:t.filter(x=>x>33).length});}}requestAnimationFrame(f);})`);
    console.log(JSON.stringify(r)); chrome.kill(); process.exit(0);
  }
  let n = 0;
  for (const [sec, p] of SHOTS) {
    const y = await evals(`(function(){var s=document.getElementById(${JSON.stringify(sec)}); if(!s) return -1; var len=+s.dataset.length||100; var top=s.getBoundingClientRect().top+scrollY; var unit=s.offsetHeight/len; if(!s.dataset.length) unit=innerHeight/100; var y=Math.round(top+${p}*unit); scrollTo(0,y); return y;})()`);
    if (y < 0) continue;
    await sleep(1400);
    const shot = await send("Page.captureScreenshot", { format: "jpeg", quality: 78 });
    const name = `${String(++n).padStart(2, "0")}-${sec}-${p}.jpg`;
    writeFileSync(join(out, name), Buffer.from(shot.data, "base64"));
  }
  const metrics = await evals(`({docW: document.documentElement.scrollWidth, winW: innerWidth, h: document.documentElement.scrollHeight})`);
  console.log(JSON.stringify({ shots: n, errors, metrics }));
  chrome.kill();
  process.exit(0);
}
main().catch((e) => { console.error(e); chrome.kill(); process.exit(1); });
