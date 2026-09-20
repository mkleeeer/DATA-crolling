// Execute the shipped page script and click its queue handler with fake
// DOM/network boundaries. No Google sheet or real download is modified.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../templates/pdf.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
async function scenario(runResponse) {
  const nodes = new Map();
  const node = () => ({value: '', textContent: '', innerHTML: '', style: {},
    addEventListener() {}, append() {}, appendChild() {}, classList: {toggle() {}}});
  const document = {getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  }, querySelectorAll() { return []; }, createElement: node};
  const calls = [];
  const ctx = vm.createContext({document, console, URL, setInterval() {}, fetch: async (url, options) => {
    calls.push({url, options});
    let data = {};
    let ok = true;
    if (url === '/api/pdf-queue/add') data = {success: true, added: 1};
    if (url === '/api/workers/pdf/run-once') { data = runResponse; ok = !!data.success; }
    return {ok, status: ok ? 200 : 500, json: async () => data};
  }});
  vm.runInContext(script, ctx);
  document.getElementById('linkPageUrl').value = 'https://example.com/reports';
  document.getElementById('linkFolder').value = 'Reports';
  vm.runInContext("picked.add('https://example.com/report.pdf'); foundLinks = [{url:'https://example.com/report.pdf', text:'Report'}]", ctx);
  await document.getElementById('linkQueueBtn').onclick();
  const added = calls.find(c => c.url === '/api/pdf-queue/add');
  assert.ok(added, 'click must actually send the queue request');
  assert.equal(JSON.parse(added.options.body).submissions[0].source_page, 'https://example.com/reports');
  assert.equal(document.getElementById('linkQueueBtn').disabled, false);
  return document.getElementById('linkQueueResult');
}
(async () => {
  assert.match((await scenario({success: true, handled: 1})).innerHTML, /1개를 처리/);
  const failed = await scenario({success: false, error: 'Google authorization required'});
  assert.match(failed.textContent, /Google authorization required/);
  assert.match(failed.textContent, /다시 추가하지 말고/);
  assert.match((await scenario({success: false, busy: true})).innerHTML, /이미 자동 처리/);
  console.log('PDF UI: success, failure, and busy click scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
