/**
 * dsh-forage host half — slim, user-driven.
 *
 * The user picks the videos; this plugin processes exactly those: import,
 * transcribe, capture figures, summarize, index. No autonomous search, no
 * planning, no parallel workers.
 */
import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { defineTool } from "@deepseek-ai/dsh-tools";
import z from "@deepseek-ai/schemastery";
import { TOOLS } from "./schemas.js";

export const name = "forage";
export const inject = ["tools"];

export const Config = z.object({
  pythonPath: z.string().default("python"),
  scriptPath: z.string().default(""),
  whisperModel: z.string().default("small"),
  defaultFrames: z.boolean().default(false),
  timeoutMs: z.number().default(60000),
});

function defaultScript() {
  return path.join(os.homedir(), ".dsh", "harvest", "harvest.py");
}

function engineEnv() {
  return { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1", PYTHONUNBUFFERED: "1" };
}

function runJson(config, args, timeoutMs) {
  const settings = config ?? {};
  const script = settings.scriptPath || defaultScript();
  const python = settings.pythonPath || "python";
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(python, [script, "--json", ...args], {
        env: engineEnv(), windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
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
      try { child.kill(); } catch { /* gone */ }
      reject(new Error("forage: engine timed out after " + String(timeoutMs) + "ms"));
    }, Math.max(1000, Number(timeoutMs) || 60000));
    child.on("error", (error) => { clearTimeout(timer); reject(new Error("forage: cannot start python: " + String(error))); });
    child.on("close", () => {
      clearTimeout(timer);
      const line = out.trim().split(/\r?\n/).filter(Boolean).pop();
      if (!line) { reject(new Error("forage: engine produced no output" + (err ? " — " + err.slice(-300) : ""))); return; }
      let value;
      try { value = JSON.parse(line); } catch { reject(new Error("forage: engine returned non-JSON: " + line.slice(0, 200))); return; }
      if (value && value.ok === false && value.error) { reject(new Error("forage: " + String(value.error.message || value.error.code || "engine error"))); return; }
      resolve(value);
    });
  });
}

function text(body) { return [{ type: "text", text: body }]; }

const ARGS = {
  forage_status: () => ["status"],
  forage_find: (a) => {
    const out = ["find", "--query", String(a.query)];
    if (a.limit !== undefined) out.push("--limit", String(Math.trunc(Number(a.limit))));
    return out;
  },
  forage_add: (a) => {
    const out = ["import", "--url", String(a.url)];
    if (a.frames === true) out.push("--frames");
    if (a.model) out.push("--model", String(a.model));
    return out;
  },
  forage_ingest: (a) => {
    const out = ["ingest"];
    if (Array.isArray(a.ids) && a.ids.length > 0) out.push("--ids", ...a.ids.map(String));
    else out.push("--top", String(Math.trunc(Number(a.top) || 1)));
    if (a.frames === true) out.push("--frames");
    if (a.force === true) out.push("--force");
    if (a.model) out.push("--model", String(a.model));
    return out;
  },
  forage_digest: (a) => {
    const out = ["digest", "--id", String(a.id)];
    if (a.maxChars !== undefined) out.push("--max-chars", String(Math.trunc(Number(a.maxChars))));
    return out;
  },
  forage_note: (a) => {
    const out = ["note", "--id", String(a.id), "--summary", String(a.summary)];
    if (a.tags !== undefined) out.push("--tags", String(a.tags));
    return out;
  },
  forage_kb: (a) => {
    const out = ["kb", "--query", String(a.query)];
    if (a.limit !== undefined) out.push("--limit", String(Math.trunc(Number(a.limit))));
    return out;
  },
  forage_import_notes: (a) => ["import-notes", "--dir", String(a.dir)],
};

const RENDERERS = {
  forage_status: (v) => text("库：" + v.total + " 条\n" + Object.entries(v.byStatus || {}).map(([k, n]) => "  " + k + ": " + n).join("\n") + "\nDB: " + v.db),
  forage_find: (v) => text("查询：" + v.query + "\n" + (v.results || []).map((r, i) => (i + 1) + ". [" + r.concept_score + "] " + r.title + "\n   " + r.url).join("\n") + ((v.local && v.local.length) ? "\n本地命中：\n" + v.local.map((x) => "  - " + x.title).join("\n") : "")),
  forage_add: (v) => text("已处理：" + v.id + (v.note ? "\n" + v.note : "")),
  forage_ingest: (v) => text((v.results || []).map((r) => (r.ok ? "OK " : "FAIL ") + r.id + (r.error ? " — " + r.error : "")).join("\n") || "(nothing)"),
  forage_digest: (v) => text(v.markdown || ""),
  forage_note: (v) => text("已写入摘要：" + v.id),
  forage_kb: (v) => text("查询：" + v.query + "\n" + ((v.results || []).length ? v.results.map((r, i) => (i + 1) + ". " + r.title + "\n   " + r.url).join("\n") : "(无匹配)")),
  forage_import_notes: (v) => text("导入笔记：" + v.imported + " 条"),
};

function buildTools(readConfig) {
  const tools = [];
  for (const spec of TOOLS) {
    const execute = async (args) => runJson(readConfig(), ARGS[spec.name](args), spec.timeoutMs || 60000);
    tools.push(defineTool({
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters,
      output: { schema: spec.output.schema, render: RENDERERS[spec.name] || ((value) => text(JSON.stringify(value, null, 2))) },
      timeoutMs: spec.timeoutMs || 60000,
      isConcurrencySafe: () => spec.name !== "forage_add" && spec.name !== "forage_ingest",
      async execute(args) { return execute(args); },
    }));
  }
  return tools;
}

export function apply(ctx, config) {
  const readConfig = () => config ?? {};
  try {
    for (const tool of buildTools(readConfig)) {
      try { ctx.tools.register(tool); } catch (error) { console.error("[forage] register failed " + tool.name + ": " + String(error)); }
    }
  } catch (error) {
    console.error("[forage] plugin failed to initialise: " + String(error));
  }
  try {
    ctx.inject(["systemPrompt"], (promptCtx) => {
      promptCtx.systemPrompt.section({
        name: "tool:forage",
        order: 2450,
        text: () => "forage_* 处理用户主动提供的视频：forage_add 加链接并转写，forage_ingest 处理已有条目，" +
          "forage_digest 取材料写摘要，forage_note 存回，forage_kb 概念检索，forage_import_notes 导入抖音收藏笔记。" +
          "顺序执行，不要并行；用户选片，模型不做自动搜索。",
      });
    });
  } catch (error) {
    console.error("[forage] prompt section unavailable: " + String(error));
  }
}
