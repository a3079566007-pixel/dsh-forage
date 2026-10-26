/**
 * Slim tool set: the user finds videos, the plugin processes what is given.
 * Removed on purpose: plan / search / learn / parallel workers.
 */
const str = (description, required = false) => ({ type: "string", description, ...(required ? { required: true } : {}) });
const num = (description) => ({ type: "number", description });
const bool = (description) => ({ type: "boolean", description });

export const TOOLS = [
  {
    name: "forage_status",
    description:
      "Report the harvest library state: how many videos are stored, by status, which ids are currently processing, and where the database lives.",
    parameters: {},
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean" },
          total: { type: "integer" },
          byStatus: { type: "json" },
          running: { type: "array", items: { type: "string" } },
          db: { type: "string" },
        },
      },
    },
    timeoutMs: 30000,
  },
  {
    name: "forage_find",
    description:
      "Concept-level search for videos to learn from: keyword recall on Bilibili, then rerank candidates by embedding similarity to the query (bge-m3), so a paraphrase like 'how do I keep the model from making things up' surfaces evaluation/grounding videos even when the words differ. Also returns local library hits. Use it proactively when the user asks to learn a topic and you need candidate videos.",
    parameters: {
      query: str("The concept or question to find videos for.", true),
      limit: num("How many candidates to return (default 8)."),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean" },
          query: { type: "string" },
          results: { type: "json" },
          local: { type: "json" },
        },
      },
    },
    timeoutMs: 120000,
  },
  {
    name: "forage_add",
    description:
      "Add ONE video the user picked and start processing it: import the link (Bilibili now; Douyin links come through the douyin-favorites Markdown export) and run transcription, with optional keyframe capture for tutorials. Returns the video id and processing outcome. Use this when the user pastes a link or says 'process this video'.",
    parameters: {
      url: str("The video URL the user provided.", true),
      frames: bool("Capture keyframes and run OCR/captioning (tutorials)."),
      model: str("Whisper model for local ASR (default from profile)."),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean" },
          id: { type: "string" },
          note: { type: "string" },
        },
      },
    },
    timeoutMs: 1800000,
  },
  {
    name: "forage_ingest",
    description:
      "Process videos already stored but not yet transcribed (for example rows imported from douyin-favorites Markdown notes). Runs sequentially, one at a time.",
    parameters: {
      ids: { type: "array", description: "Video ids to process. Omit to pick the newest unprocessed rows.", items: { type: "string" } },
      top: num("How many rows to process when ids are omitted (default 1)."),
      frames: bool("Capture keyframes and run OCR/captioning."),
      model: str("Whisper model for local ASR."),
      force: bool("Re-process even if a transcript exists."),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean" },
          results: { type: "json" },
        },
      },
    },
    timeoutMs: 1800000,
  },
  {
    name: "forage_digest",
    description:
      "Return the collected material pack for one video: metadata, transcript, figure captions/OCR and any saved summary. Read it, then write the structured summary with forage_note.",
    parameters: {
      id: str("Video id.", true),
      maxChars: num("Transcript character budget (default 6000)."),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: { ok: { type: "boolean" }, id: { type: "string" }, markdown: { type: "string" } },
      },
    },
    timeoutMs: 60000,
  },
  {
    name: "forage_note",
    description:
      "Store a model-written structured summary for one video so it becomes searchable through forage_kb.",
    parameters: {
      id: str("Video id.", true),
      summary: str("Structured summary (conclusion, points, figures, pitfalls, anchors).", true),
      tags: str("Optional comma-separated tags."),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: { ok: { type: "boolean" }, id: { type: "string" } },
      },
    },
    timeoutMs: 60000,
  },
  {
    name: "forage_kb",
    description:
      "Concept-level semantic search over the local library: hybrid FTS + bge-m3 vectors fused with RRF, returning the best timestamped snippet. Falls back to lexical matching when no embedding is configured.",
    parameters: {
      query: str("What to look for, in natural language.", true),
      limit: num("Maximum results (default 8)."),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean" },
          query: { type: "string" },
          results: { type: "json" },
        },
      },
    },
    timeoutMs: 60000,
  },
  {
    name: "forage_import_notes",
    description:
      "Import Markdown notes exported by the douyin-favorites-to-knowledge skill (frontmatter + transcript) into the local library as douyin rows, ready for indexing and search.",
    parameters: {
      dir: str("Directory containing the exported .md notes.", true),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: { ok: { type: "boolean" }, imported: { type: "integer" } },
      },
    },
    timeoutMs: 120000,
  },
];
