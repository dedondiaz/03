import express from 'express';
import { chromium } from 'playwright';
import fs from 'fs/promises';
import path from 'path';
import crypto from 'crypto';

const app = express();
app.use(express.json({ limit: '2mb' }));
const ROOT = process.env.ARTIFACTS_ROOT || '/artifacts';

const mutating = new Set(['fill','select','press']);
function allowed(url, policy){
  const u = new URL(url);
  if (!(policy.allowed_domains || []).includes(u.hostname)) return false;
  const prefixes = policy.allowed_path_prefixes || [];
  return prefixes.length === 0 || prefixes.some((p) => u.pathname.startsWith(p));
}

app.post('/run', async (req,res)=>{
  const { tenant_id, browser_run_id, policy, steps, storage_state, record_trace } = req.body;
  if (!policy || !Array.isArray(steps)) return res.status(400).json({status:'FAILED', errors:['invalid_payload']});
  if (steps.length > (policy.max_steps || 25)) return res.json({status:'ABORTED_POLICY', errors:['step_cap_exceeded'], artifacts:[]});
  const dir = path.join(ROOT, tenant_id, browser_run_id);
  await fs.mkdir(dir, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ storageState: storage_state || undefined });
  if (record_trace) await context.tracing.start({ screenshots: true, snapshots: true });
  const page = await context.newPage();
  const artifacts = [];
  const extracted = {};
  const errors = [];
  let finalUrl = '';
  const startedAt = Date.now();
  try {
    for (let i=0;i<steps.length;i++){
      const s = steps[i];
      if (!policy.allow_mutations && mutating.has(s.type)) throw new Error('mutations_blocked');
      if (s.type === 'goto') { await page.goto(s.url, { waitUntil: 'domcontentloaded' }); const rel = `start-${i}.png`; const abs = path.join(dir, rel); await page.screenshot({ path: abs, fullPage: true }); const buf = await fs.readFile(abs); artifacts.push({ kind:'screenshot', step_index:i, file_path:path.join(tenant_id,browser_run_id,rel), sha256:crypto.createHash('sha256').update(buf).digest('hex'), byte_size:buf.length, mime_type:'image/png' }); }
      if (s.type === 'click') await page.click(s.selector);
      if (s.type === 'fill') await page.fill(s.selector, s.value || '');
      if (s.type === 'select') await page.selectOption(s.selector, s.value);
      if (s.type === 'press') await page.press(s.selector, s.value);
      if (s.type === 'wait_for') await page.waitForSelector(s.selector, { timeout: s.timeout_ms || 5000 });
      if (s.type === 'extract_text') extracted[s.name || `text_${i}`] = (await page.textContent(s.selector) || '').slice(0, 1000);
      if (s.type === 'extract_html') extracted[s.name || `html_${i}`] = (await page.innerHTML(s.selector) || '').slice(0, 1000);
      if (s.type === 'screenshot') {
        const rel = `${i}-shot.png`; const abs = path.join(dir, rel);
        await page.screenshot({ path: abs, fullPage: true });
        const buf = await fs.readFile(abs);
        artifacts.push({ kind:'screenshot', step_index:i, file_path: path.join(tenant_id,browser_run_id,rel), sha256: crypto.createHash('sha256').update(buf).digest('hex'), byte_size:buf.length, mime_type:'image/png' });
      }
      finalUrl = page.url();
      if (!allowed(finalUrl, policy)) throw new Error('allowlist_violation');
    }
    const endRel='final.png'; const endAbs=path.join(dir,endRel);
    await page.screenshot({ path:endAbs, fullPage:true });
    const b = await fs.readFile(endAbs);
    artifacts.push({ kind:'screenshot', step_index:steps.length, file_path:path.join(tenant_id,browser_run_id,endRel), sha256:crypto.createHash('sha256').update(b).digest('hex'), byte_size:b.length, mime_type:'image/png' });
    if (record_trace){
      const traceRel='trace.zip'; const traceAbs=path.join(dir, traceRel);
      await context.tracing.stop({ path: traceAbs });
      const tb = await fs.readFile(traceAbs);
      artifacts.push({ kind:'trace', step_index:null, file_path:path.join(tenant_id,browser_run_id,traceRel), sha256:crypto.createHash('sha256').update(tb).digest('hex'), byte_size:tb.length, mime_type:'application/zip' });
    }
    await context.close(); await browser.close();
    return res.json({ status:'COMPLETED', final_url: finalUrl, extracted, artifacts, errors, runtime_seconds: Math.max(0, Math.floor((Date.now()-startedAt)/1000)) });
  } catch (e) {
    errors.push(String(e.message || e));
    await context.close(); await browser.close();
    return res.json({ status: String(e.message || '').includes('allowlist') ? 'ABORTED_POLICY' : 'FAILED', final_url: finalUrl, extracted, artifacts, errors, runtime_seconds: Math.max(0, Math.floor((Date.now()-startedAt)/1000)) });
  }
});

app.listen(3000, ()=> console.log('automation-runner listening on 3000'));
