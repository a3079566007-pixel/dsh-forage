/**
 * Tool schemas for dsh-forage.
 *
 * Pure data: no imports. Kept separate so a stub test can validate every
 * schema with the host's own defineTool without loading the plugin.
 */

const str = (description, required = false) => ({ type: "string", description, ...(required ? { required: true } : {}) });
const num = (description) => ({ type: "number", description });
const bool = (description) => ({ type: "boolean", description });

/** One candidate row returned by a fast search. */
const row = {
  type: "object",
  additionalProperties: false,
  properties: {
    id: { type: "string" },
    title: { type: "string" },
    author: { type: "string" },
    duration: { type: "integer" },
    url: { type: "string" },
    play: { type: "integer" },
    like: { type: "integer" },
    score: { type: "number" },
    status: { type: "string" },
  },
};

export const TOOLS = [
  {
    name: "forage_status",
    description:
      "Report the Forage library state: how many videos are stored, by status (new/selected/ingesting/ingested/summarized/skipped/failed), which ids are currently ingesting, and where the SQLite database lives. Use it after starting a long ingest, or to check whether the engine and its database are reachable.",
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
    name: "forage_plan",
    description:
      "Turn a collection brief into a search plan: several concrete queries derived from the brief plus the user's configured interests, and the active filters (duration range, excluded words). Does not touch the network. Use it before forage_search when the user gives a direction rather than exact keywords.",
    parameters: {
      brief: str("The direction the user asked for, for example 大模型 RAG 工程实践.", true),
      queries: { type: "array", description: "Optional explicit queries that override the generated ones.", items: { type: "string" } },
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean" },
          brief: { type: "string" },
          queries: { type: "array", items: { type: "string" } },
          filters: { type: "json" },
          results_per_query: { type: "number" },
          deep_ingest_top: { type: "number" },
        },
      },
    },
    timeoutMs: 30000,
  },
  {
    name: "forage_search",
    description:
      "Fast metadata-only search of Bilibili (WBI signed; no download), filtered by the configured duration window and scored by play/like/recency/interest fit. Each call handles up to 4 queries and returns at most limit stored candidates. Repeated calls are deduplicated by video id. Use forage_search first, then forage_ingest on the few worth transcribing.",
    parameters: {
      queries: { type: "array", description: "One to four search queries.", items: { type: "string" }, required: true },
      limit: num("How many ranked candidates to return (default 20)."),
      all: bool("Ignore the profile duration filter."),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean" },
          added: { type: "integer" },
          rows: { type: "array", items: row },
        },
      },
    },
    timeoutMs: 120000,
  },
  {
    name: "forage_ingest",
    description:
      "Start deep collection for selected video ids in the background: download audio, transcribe with faster-whisper, optionally download the video for keyframe OCR, then record everything into the local library. Returns immediately with a job id; poll forage_status for progress and use forage_digest when the status becomes ingested. Ingesting many or long videos is expensive; prefer 1-3 at a time.",
    parameters: {
      ids: { type: "array", description: "Video ids such as bili:BV1yXto63Ede. Omit to auto-pick the highest-scored selected/new rows.", items: { type: "string" } },
      top: num("When ids are omitted, how many top-scored rows to ingest (default 3)."),
      frames: bool("Also download the video and OCR keyframes (slower, larger). Default follows the plugin config."),
      model: str("Whisper model name, for example tiny/small/medium. Default follows the plugin config."),
      force: bool("Clear existing transcript/OCR and re-collect."),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean" },
          jobId: { type: "string" },
          pid: { type: "integer" },
          ids: { type: "array", items: { type: "string" } },
          note: { type: "string" },
        },
      },
    },
    timeoutMs: 30000,
  },
  {
    name: "forage_digest",
    description:
      "Return the collected material pack for one video: metadata, the transcript (ASR or subtitles), the OCR text of keyframes, and any saved summary. Read it, then write a structured summary (conclusion, key points, key figures, pitfalls, source anchors) and store it with forage_note. Does not call a model itself.",
    parameters: {
      id: str("Video id, for example bili:BV1yXto63Ede.", true),
      maxChars: num("Transcript character budget (default 6000)."),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean" },
          id: { type: "string" },
          markdown: { type: "string" },
        },
      },
    },
    timeoutMs: 60000,
  },
  {
    name: "forage_note",
    description:
      "Store a model-written summary for one collected video so it becomes searchable through forage_kb. Call it after reading forage_digest; pass the summary text and optional comma-separated tags.",
    parameters: {
      id: str("Video id.", true),
      summary: str("Structured summary text.", true),
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
      "Concept-level semantic search over the local harvest library: hybrid retrieval combining full-text (FTS5) and bge-m3 vector similarity (chunks from transcripts, summaries and figure captions), fused with RRF. Falls back to lexical matching when no embedding index is configured. Returns id, title, url and the best-matching timestamped snippet, so a paraphrase like 'how do I stop the model from making things up' still finds the right video.",
    parameters: {
      query: str("What to look for.", true),
      limit: num("Maximum results (default 8)."),
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean" },
          query: { type: "string" },
          results: {
            type: "array",
            items: {
              type: "object",
              additionalProperties: false,
              properties: { id: { type: "string" }, title: { type: "string" }, url: { type: "string" } },
            },
          },
        },
      },
    },
    timeoutMs: 60000,
  },
];
