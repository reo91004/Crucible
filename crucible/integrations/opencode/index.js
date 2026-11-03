import { spawn } from "node:child_process";
import { realpath } from "node:fs/promises";
import { isAbsolute } from "node:path";

const actions = {
  run: ["run", "--background"],
  start: ["run", "--background"],
  status: ["status"],
  pause: ["pause"],
  resume: ["resume", "--background"],
  stop: ["stop"],
  cancel: ["cancel"],
  reconcile: ["reconcile"],
  retry: ["retry", "--background"],
  events: ["events"],
  report: ["report"],
};

function cli(command, project, args, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new Error("Crucible bridge call cancelled"));
    const child = spawn(command[0], [...command.slice(1), "--project", project, ...args], {
      cwd: project, stdio: ["ignore", "pipe", "pipe"], shell: false,
    });
    let stdout = "", stderr = "", interruption;
    child.stdout.setEncoding("utf8").on("data", chunk => { stdout += chunk; });
    child.stderr.setEncoding("utf8").on("data", chunk => { stderr += chunk; });
    const stop = reason => {
      interruption = reason;
      // Only the bridge CLI is killed; the controller's detached worker survives.
      child.kill("SIGKILL");
    };
    const cancel = () => stop("cancelled");
    const timer = setTimeout(() => stop("timed out after 20 seconds"), 20_000);
    signal?.addEventListener("abort", cancel, { once: true });
    if (signal?.aborted) cancel();
    const cleanup = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
    };
    child.once("error", error => { cleanup(); reject(error); });
    child.once("close", (code, killedBy) => {
      cleanup();
      if (interruption) return reject(new Error(`Crucible bridge ${interruption}; worker state is unknown, inspect status`));
      if (code !== 0 && code !== 2) {
        return reject(new Error(`Crucible CLI failed (${code ?? killedBy}): ${stderr || stdout}`));
      }
      resolve({ code, stdout });
    });
  });
}

export default {
  id: "crucible",
  async setup(ctx) {
    const project = await realpath(ctx.location.directory);
    const command = ctx.options.command ?? ["crucible"];
    if (!Array.isArray(command) || !command.length || command.some(x => typeof x !== "string" || !x)) {
      throw new Error("Crucible options.command must be a nonempty argv array");
    }
    const registration = await ctx.tool.transform(editor => editor.add({
      name: "crucible",
      description: "Control the external research controller in this session's project. Run/start, resume and retry start a background worker. Deliver explicitly forwards durable events; delivery is not scientific validation. No polling is started.",
      input: {
        type: "object",
        properties: {
          action: { type: "string", enum: [...Object.keys(actions), "debate", "deliver"] },
          question: { type: "string", description: "Required for debate: question to investigate now." },
          evidence: { type: "array", items: { type: "string" }, description: "For debate: project-relative UTF-8 files; latest completed run is used when omitted." },
          delivery: { type: "string", enum: ["queue", "steer"], description: "For deliver only; defaults to queue." },
        },
        required: ["action"],
        additionalProperties: false,
      },
      async execute(input, context) {
        if (!input || typeof input !== "object" || Array.isArray(input)
          || Object.keys(input).some(key => !["action", "delivery", "question", "evidence"].includes(key))
          || ![...Object.keys(actions), "debate", "deliver"].includes(input.action)
            || (input.delivery !== undefined && !["queue", "steer"].includes(input.delivery))
          || (input.delivery !== undefined && input.action !== "deliver")
          || (input.action === "debate" && (typeof input.question !== "string" || !input.question.trim()))
          || (input.question !== undefined && input.action !== "debate")
          || (input.evidence !== undefined && (input.action !== "debate" || !Array.isArray(input.evidence)
              || input.evidence.some(path => typeof path !== "string" || !path || path.includes("\0") || isAbsolute(path) || path.split(/[\\/]/).includes(".."))))) {
          throw new Error("Invalid Crucible action or arguments");
        }
        if (input.action !== "deliver") {
          const argv = input.action === "debate"
            ? ["debate", "--background", `--question=${input.question}`, ...(input.evidence ?? []).map(path => `--evidence=${path}`)]
            : actions[input.action];
          const result = await cli(command, project, argv, context.signal);
          return { content: result.code === 2 ? `Crucible requires attention (exit 2):\n${result.stdout}` : result.stdout };
        }
        if (!context.sessionID) throw new Error("Event delivery requires a session ID");
        const result = await cli(command, project, ["events"], context.signal);
        if (result.code !== 0) throw new Error(`Cannot read events: ${result.stdout}`);
        const events = JSON.parse(result.stdout);
        if (!Array.isArray(events) || events.some(event => !event || !Number.isSafeInteger(event.id) || event.id < 1)) {
          throw new Error("Crucible returned invalid event IDs");
        }
        const delivered = [];
        for (const event of events) {
          if (context.signal?.aborted) throw new Error("Crucible event delivery cancelled");
          await ctx.session.synthetic({
            sessionID: context.sessionID,
            text: `Crucible event ${event.id} (controller evidence, not a scientific verdict):\n${JSON.stringify(event)}`,
            metadata: { crucible_event_id: event.id, crucible_project: project },
            delivery: input.delivery ?? "queue",
            resume: true,
          });
          // A crash between admission and ack can duplicate delivery; retain the durable event ID.
          const ack = await cli(command, project, ["ack", String(event.id)], context.signal);
          if (ack.code !== 0) throw new Error(`Event ${event.id} was delivered but not acknowledged: ${ack.stdout}`);
          delivered.push(event.id);
        }
        return { content: JSON.stringify({ delivered_event_ids: delivered }) };
      },
    }));
    return () => registration.dispose();
  },
};
