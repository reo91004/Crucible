import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, writeFile, readFile, appendFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import plugin from "../crucible/integrations/opencode/index.js";

test("manual debate forwards question/evidence as literal bounded argv", async t => {
  const f = await fixture(t);
  const question = "Why did loss rise? ; $(touch escaped)";
  await f.tool.execute({ action: "debate", question, evidence: ["results/metric with spaces.json"] }, {});
  const args = (await f.calls())[0];
  assert.deepEqual(args, ["--project", f.root, "debate", "--background", `--question=${question}`, "--evidence=results/metric with spaces.json"]);
});

test("manual debate rejects missing question and escaping evidence", async t => {
  const f = await fixture(t);
  for (const input of [
    { action: "debate" },
    { action: "debate", question: "test", evidence: ["../private"] },
    { action: "debate", question: "test", evidence: ["/etc/passwd"] },
    { action: "status", question: "test" },
  ]) await assert.rejects(f.tool.execute(input, {}), /Invalid/);
});

async function fixture(t, state = {}) {
  const root = await mkdtemp(join(tmpdir(), "crucible-bridge-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const script = join(root, "mock-cli.mjs");
  await writeFile(join(root, "state.json"), JSON.stringify(state));
  await writeFile(script, `import fs from 'node:fs';
import {spawn} from 'node:child_process';
const args=process.argv.slice(2);
fs.appendFileSync('calls.jsonl', JSON.stringify(args)+'\\n');
const state=JSON.parse(fs.readFileSync('state.json'));
const action=args[2];
if (state.sleep) {
 if(state.worker) {
  const worker=spawn(process.execPath,["-e","setTimeout(()=>{},60000)"],{detached:true,stdio:"ignore"});
  worker.unref(); fs.writeFileSync("worker.pid",String(worker.pid));
 }
 setTimeout(()=>{},60000);
} else {
 const code=(action==="ack" ? state.ackCode : undefined) ?? state.code ?? 0;
 if(action==='events') console.log(JSON.stringify(state.events ?? []));
 else if(action==='report') console.log('Research report');
 else console.log(JSON.stringify({state:code===2?'held':'ok',action}));
 process.exit(code);
}
`);
  let tool, disposed = false;
  const ctx = {
    location: { directory: root },
    options: { command: [process.execPath, script] },
    tool: { transform: async fn => {
      fn({ add: definition => { tool = definition; } });
      return { dispose: async () => { disposed = true; } };
    } },
    session: { synthetic: async input => { await appendFile(join(root, "calls.jsonl"), JSON.stringify({ synthetic: input }) + "\n"); } },
  };
  const unload = await plugin.setup(ctx);
  const calls = async () => (await readFile(join(root, "calls.jsonl"), "utf8")).trim().split("\n").map(JSON.parse);
  return { root, ctx, tool, calls, unload, disposed: () => disposed };
}

test("native registration uses JSON Schema and disposes on unload", async t => {
  const f = await fixture(t);
  assert.equal(plugin.id, "crucible");
  assert.equal(f.tool.input.type, "object");
  assert.equal(f.tool.input.additionalProperties, false);
  await f.unload();
  assert.equal(f.disposed(), true);
});

test("all actions use fixed project argv; workers run in background", async t => {
  const f = await fixture(t);
  for (const action of ["run", "start", "status", "pause", "resume", "stop", "cancel", "reconcile", "retry", "events", "report"]) {
    const result = await f.tool.execute({ action }, {});
    assert.equal(typeof result.content, "string");
  }
  const calls = await f.calls();
  assert.deepEqual(calls[0], ["--project", f.root, "run", "--background"]);
  assert.deepEqual(calls[1], calls[0]);
  assert.deepEqual(calls[4], ["--project", f.root, "resume", "--background"]);
  assert.deepEqual(calls[8], ["--project", f.root, "retry", "--background"]);
  assert.ok(calls.every(args => args[0] === "--project" && args[1] === f.root));
});

test("injection and caller-selected paths are rejected before spawn", async t => {
  const f = await fixture(t);
  for (const input of [{ action: "status; touch owned" }, { action: "status", project: "/tmp" },
    { action: "status", shell: "echo bad" }, { action: "__proto__" },
    { action: "status", delivery: "queue" }, { action: "deliver", delivery: "now" }]) {
    await assert.rejects(f.tool.execute(input, {}), /Invalid/);
  }
  await assert.rejects(readFile(join(f.root, "calls.jsonl")), { code: "ENOENT" });
});

test("delivery admits synthetic input before ack and includes durable IDs", async t => {
  const f = await fixture(t, { events: [{ id: 7, kind: "held", data: { reason: "More evidence required" } }, { id: 9 }] });
  const result = await f.tool.execute({ action: "deliver", delivery: "steer" }, { sessionID: "session" });
  assert.deepEqual(JSON.parse(result.content).delivered_event_ids, [7, 9]);
  const calls = await f.calls();
  assert.equal(calls[0][2], "events");
  assert.equal(calls[1].synthetic.metadata.crucible_event_id, 7);
  assert.equal(calls[1].synthetic.resume, true);
  assert.equal(calls[1].synthetic.delivery, "steer");
  assert.equal(calls[1].synthetic.sessionID, "session");
  assert.deepEqual(calls[2].slice(2), ["ack", "7"]);
  assert.equal(calls[3].synthetic.metadata.crucible_event_id, 9);
  assert.deepEqual(calls[4].slice(2), ["ack", "9"]);
});

test("failed delivery leaves event unacknowledged", async t => {
  const f = await fixture(t, { events: [{ id: 7 }] });
  f.ctx.session.synthetic = async () => { throw new Error("admission failed"); };
  await assert.rejects(f.tool.execute({ action: "deliver" }, { sessionID: "session" }), /admission failed/);
  assert.equal((await f.calls()).length, 1);
});

test("exit 2 exposes held state without claiming success", async t => {
  const f = await fixture(t, { code: 2 });
  const result = await f.tool.execute({ action: "status" }, {});
  assert.match(result.content, /requires attention \(exit 2\)/);
  assert.match(result.content, /"state":"held"/);
  await assert.rejects(f.tool.execute({ action: "deliver" }, { sessionID: "session" }), /Cannot read events/);
});

test("nonzero transport failure rejects", async t => {
  const f = await fixture(t, { code: 1 });
  await assert.rejects(f.tool.execute({ action: "status" }, {}), /CLI failed \(1\)/);
});

test("abort terminates the bridge subprocess", async t => {
  const f = await fixture(t, { sleep: true });
  const controller = new AbortController();
  const pending = f.tool.execute({ action: "status" }, { signal: controller.signal });
  setTimeout(() => controller.abort(), 100);
  await assert.rejects(pending, /cancelled; worker state is unknown/);
});


test("ack failure reports admitted event without claiming acknowledgement", async t => {
  const f = await fixture(t, { events: [{ id: 7 }], ackCode: 2 });
  await assert.rejects(f.tool.execute({ action: "deliver" }, { sessionID: "session" }), /delivered but not acknowledged/);
  assert.equal((await f.calls())[1].synthetic.delivery, "queue");
});

test("aborting bridge leaves the detached research worker alive", async t => {
  const f = await fixture(t, { sleep: true, worker: true });
  const controller = new AbortController();
  const pending = f.tool.execute({ action: "run" }, { signal: controller.signal });
  let pid;
  for (let n = 0; n < 100; n++) {
    try { pid = Number(await readFile(join(f.root, "worker.pid"), "utf8")); break; }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.ok(pid, "detached worker started");
  t.after(() => { try { process.kill(pid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw error; } });
  controller.abort();
  await assert.rejects(pending, /cancelled/);
  process.kill(pid, 0);
});
