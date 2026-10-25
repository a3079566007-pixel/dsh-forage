# Changelog

## v0.1.0 — 2026-10-25

首个公开版本。

### Added
- DSH 插件 `dsh-forage`（采源）：7 个 `forage_*` 工具。
- Python 引擎：B 站 WBI 签名搜索、yt-dlp 下载、faster-whisper 转写、ffmpeg 场景抽帧 + rapidocr、SQLite（trigram FTS + 中文二元组兜底）。
- 配置项：Python 路径、引擎路径、Whisper 模型、默认抽帧、超时。
- `forage_ingest` 后台采集，状态写入 SQLite，`forage_status` 可查。
- 文档：README、`docs/SKILL.md`（给模型的使用说明）、PUBLISH。

### Notes
- 抖音仅支持链接导入（yt-dlp）；关键词搜索请配合 `whale_dive_*`。
- OCR 对竖屏低码率视频效果有限，关键帧原图保留供视觉模型精读。
- ASR 专有名词可能误识（如 LoRA → RORA），摘要时需按上下文校正。
