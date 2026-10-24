/**
 * dsh-forage host half.
 *
 * Thin shell over the Python harvest engine: the plugin validates tool input,
 * spawns the engine with --json, and maps the result back. No business logic
 * lives here, and no client half is shipped yet.
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { defineTool } from "@deepseek-ai/dsh-tools";
import z from "@deepseek-ai/schemastery";
import { TOOLS } from "./schemas.js";

/** Cordis plugin name. */
export const name = "forage";

/** The tool registry is the only hard dependency. */
export const inject = ["tools"];

/** Every field is read on each call, so a settings edit applies next call. */
export const Config = z.object({
  pythonPath: z.string().default("python"),
  scriptPath: z.string().default(""),
  whisperModel: z.string().default("small"),
  defaultFrames: z.boolean().default(false),
  timeoutMs: z.number().default(60000),
  ingestTimeoutMs: z.number().default(1800000),
});

/** Default engine location under the user's DSH home. */
function defaultScript() {
  return path.join(os.homedir(), ".dsh", "harvest", "harvest.py");
}

/** Environment for the engine: forced UTF-8, inherited everything else. */
function engineEnv() {
  return {
    ...process.env,
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
    PYTHONUNBUFFERED: "1",
  };
}

/** Run one engine command that prints a single JSON line, and return it. */
function runJson(config, args, timeoutMs) {
  const settings = config ?? {};
  const script = settings.scriptPath || defaultScript();
  const python = settings.pythonPath || "python";
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(python, [script, "--json", ...args], {
        env: engineEnv(),
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      reject(new Error("forage: cannot spawn " + python + ": " + String(error)));
      return;
    }
    let out = "";
    let err = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { out += chunk; });
    child.stderr.on("data", (chunk) => { err += chunk; });
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* already gone */ }
      reject(new Error("forage: engine timed out after " + String(timeoutMs) + "ms"));
    }, Math.max(1000, Number(timeoutMs) || 60000));
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(new Error("forage: cannot start python: " + String(error)));
    });
    child.on("close", () => {
      clearTimeout(timer);
      const line = out.trim().split(/\r?\n/).filter(Boolean).pop();
      if (!line) {
        reject(new Error("forage: engine produced no output" + (err ? " — " + err.slice(-300) : "")));
        return;
      }
      let value;
      try {
        value = JSON.parse(line);
      } catch {
        reject(new Error("forage: engine returned non-JSON: " + line.slice(0, 200)));
        return;
      }
      if (value && value.ok === false && value.error) {
        reject(new Error("forage: " + String(value.error.message || value.error.code || "engine error")));
        return;
      }
      resolve(value);
    });
  });
}

/** Start a background ingest and return immediately. */
function startIngest(config, args) {
  const settings = config ?? {};
  const script = settings.scriptPath || defaultScript();
  const python = settings.pythonPath || "python";
  const child = spawn(python, [script, "ingest", ...args], {
    env: engineEnv(),
    detached: true,
    windowsHide: true,
    stdio: "ignore",
  });
  child.unref();
  return { jobId: "forage-" + Date.now().toString(36), pid: Number(child.pid) || 0 };
}

/** Build engine argv for each tool. */
const ARGS = {
  forage_status: () => ["status"],
  forage_plan: (args) => {
    const out = ["plan", "--brief", String(args.brief)];
    if (Array.isArray(args.queries) && args.queries.length > 0) out.push("--queries", ...args.queries.map(String));
    return out;
  },
  forage_search: (args) => {
    const out = ["search", "--query", ...args.queries.map(String)];
    if (args.limit !== undefined) out.push("--limit", String(Math.trunc(Number(args.limit))));
    if (args.all === true) out.push("--all");
    return out;
  },
  forage_digest: (args) => {
    const out = ["digest", "--id", String(args.id)];
    if (args.maxChars !== undefined) out.push("--max-chars", String(Math.trunc(Number(args.maxChars))));
    return out;
  },
  forage_note: (args) => {
    const out = ["note", "--id", String(args.id), "--summary", String(args.summary)];
    if (args.tags !== undefined) out.push("--tags", String(args.tags));
    return out;
  },
  forage_kb: (args) => {
    const out = ["kb", "--query", String(args.query)];
    if (args.limit !== undefined) out.push("--limit", String(Math.trunc(Number(args.limit))));
    return out;
  },
};

/** Build the registry-ready tool list. */
function buildTools(readConfig) {
  const tools = [];
  for (const spec of TOOLS) {
    const execute = async (args) => {
      const config = readConfig();
      if (spec.name === "forage_ingest") {
        const argv = ["--top", String(Math.trunc(Number(args.top) || 3))];
        if (Array.isArray(args.ids) && args.ids.length > 0) argv.push("--ids", ...args.ids.map(String));
        if (args.frames === true || (args.frames === undefined && config.defaultFrames === true)) argv.push("--frames");
        if (args.force === true) argv.push("--force");
        const model = args.model || config.whisperModel || "small";
        argv.push("--model", String(model));
        const started = startIngest(config, argv);
        return {
          ok: true,
          jobId: started.jobId,
          pid: started.pid,
          ids: Array.isArray(args.ids) ? args.ids.map(String) : [],
          note: "后台采集已启动；用 forage_status 查进度，完成后用 forage_digest 取材料。",
        };
      }
      return runJson(config, ARGS[spec.name](args), spec.timeoutMs || config.timeoutMs || 60000);
    };
    tools.push(defineTool({
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters,
      output: { schema: spec.output.schema },
      timeoutMs: spec.timeoutMs || 60000,
      isConcurrencySafe: () => spec.name !== "forage_ingest",
      async execute(args) {
        return execute(args);
      },
    }));
  }
  return tools;
}

/** Plugin entry point. A failure here must not prevent the host from booting. */
export function apply(ctx, config) {
  const readConfig = () => config ?? {};
  try {
    for (const tool of buildTools(readConfig)) {
      try {
        ctx.tools.register(tool);
      } catch (error) {
        console.error("[forage] failed to register " + tool.name + ": " + String(error));
      }
    }
  } catch (error) {
    console.error("[forage] plugin failed to initialise: " + String(error));
  }
  try {
    ctx.inject(["systemPrompt"], (promptCtx) => {
      promptCtx.systemPrompt.section({
        name: "tool:forage",
        order: 2450,
        text: () =>
          "forage_* 采集并检索视频知识：forage_search 快扫 B 站元数据，forage_ingest 后台转写/抽帧，" +
          "forage_digest 取材料由你写摘要，forage_note 存回，forage_kb 模糊检索本地库。" +
          "采集长视频很贵，先快扫后精选，一次深采 1-3 条；ingest 是后台任务，用 forage_status 查进度。",
      });
    });
  } catch (error) {
    console.error("[forage] system prompt section unavailable: " + String(error));
  }
}
