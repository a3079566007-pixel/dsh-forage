---
name: video-harvest
description: 采源 Harvest —— 按你的方向"倾向性"批量采集视频（B站为主，抖音链接可导入），把口播文案转写成文字、把画面里的 PPT/代码/板书抽帧 OCR 成图文资料，生成核心摘要并落进本地 SQLite 知识库做模糊检索。当用户要求"刷/采集/整理知识类视频""把这个视频讲了什么整理出来""批量看某主题的教程并总结"时使用。
---

# 采源 Harvest（视频知识采集）

> **优先用插件工具**（插件 dsh-forage，重启后生效）：`forage_status / forage_plan / forage_search / forage_ingest / forage_digest / forage_note / forage_kb`。
> 插件不可用时，退回下面的 CLI 流程（两者共用同一引擎与数据库）。

引擎：`C:\Users\Administrator\.dsh\harvest\harvest.py`
数据：`~/.dsh/harvest/`（`data/harvest.db`、`media/<id>/`、`profile.json`）
定位：**快扫 → 精选 → 深采 → 总结 → 入库检索**。快扫不下载视频；深采才转写/抽帧。

## 一、先看偏好（倾向性来源）

```powershell
type $env:USERPROFILE\.dsh\harvest\profile.json
```

字段：`interests`（方向）、`exclude`（排除词）、`min_duration/max_duration`（秒）、`deep_ingest_top`、`whisper_model`、`ocr`。
用户改方向时改这里，不要改代码。

## 二、标准流程

```powershell
$env:PYTHONIOENCODING='utf-8'
$H="C:\Users\Administrator\.dsh\harvest\harvest.py"

# 1) 需求 → 检索计划（可自己指定 queries，更准）
python $H plan --brief "大模型 RAG 工程实践"

# 2) 快扫：只取元数据，不下载（一次 20 条/查询，秒级）
python $H search --query "RAG 实战 教程" "向量数据库 原理" --limit 20

# 3) 精选（按 score 排序后人工/模型判断，标记要深采的）
python $H pick --ids bili:BV1JLN2z4EZQ bili:BV1mNa76yEN8

# 4) 深采：下载音频转写（默认），加 --frames 才下载视频做抽帧 OCR
python $H ingest --ids bili:BV1JLN2z4EZQ --frames

# 5) 取采集包（元数据 + 转写 + OCR），由模型写摘要
python $H digest --id bili:BV1JLN2z4EZQ --max-chars 6000

# 6) 摘要写回（沿用第 5 步的结果整理成要点）
python $H note --id bili:BV1JLN2z4EZQ --summary "……" --tags "RAG,检索,向量库"

# 7) 以后按内容模糊搜
python $H kb --query "RAG 召回率 怎么调"
```

## 三、总结的写法（第 5→6 步）

读 `digest` 输出的转写与 OCR 后，输出固定结构，别写空话：

```
一句话结论：
核心要点（3~6 条，每条一行）：
关键图文（OCR 里出现的代码/公式/表格/架构图，转写缺失处以此补）：
可操作步骤（若是教程）：
坑与前提（版本、依赖、quota）：
原文锚点：时间戳或链接
```

OCR 是"画面文字"，可能与口播重复；**取信息量更大的一方**，冲突时以画面为准并注明。

## 四、抖音

- 元数据/热榜/搜索：用 `whale_dive_douyin`（搜索需先在插件浏览器里登录一次）。
- 抖音**没有字幕接口**，要文字只能：`harvest.py import --url "<视频直链或分享链接>"` 走 yt-dlp 下载 + 本地 ASR（可能需要 cookies；失败会明确报错）。
- 抖音竖屏教学视频的价值主要在画面（代码/PPT），务必加 `--frames`。

## 五、硬约束与坑

| 事项 | 说明 |
|---|---|
| 时长过滤 | 默认 120–3600 秒；几千分钟的"合集"会被过滤，别硬采 |
| 限速 | B站搜索走 WBI 签名；连续调用会 -352/-412，遇到就停 30–60 秒，别重试轰炸 |
| 模型下载 | faster-whisper 走 `HF_ENDPOINT=https://hf-mirror.com`（直连 HF 被墙），首次会下模型；`small` 约 460 MB |
| 抽帧成本 | `--frames` 要下载 720p 视频，体积大；知识类视频值得，纯口播别开 |
| 磁盘 | `media/<id>/` 存音频/视频/帧，长期用要定期清 `video.*` |
| 幂等 | 重复 `ingest` 不会重转写（已有 transcript 会跳过），要重来先删库里的 transcript |

## 六、已验证的环境事实（2026-10-25 本机实测）

| 项 | 事实 |
|---|---|
| ASR | faster-whisper CPU int8；`tiny` 中文技术词会错（微调→"微条"），**生产用 `small`** |
| 模型源 | 直连 HuggingFace 超时；必须 `HF_ENDPOINT=https://hf-mirror.com` + `HF_HUB_DISABLE_XET=1`（已在代码里 setdefault） |
| PyAV 坑 | faster-whisper 的 `av.open(metadata_errors=...)` 与旧 PyAV 不兼容；代码已改为 **ffmpeg 解码成 16k wav → numpy**，绕开 PyAV |
| OCR | rapidocr_onnxruntime（已装），跨帧重复行已去重；**竖屏 480p 源（抖音/B站竖版）文字识别很粗**，只当线索，精确读图请用视觉模型看 frames/ |
| 抽帧 | ffmpeg 场景检测 scene>0.25 + 8x8 平均哈希去重（相邻帧差<6 丢弃）；帧图保存在 media/<id>/frames/ |
| 下载 | yt-dlp 可下 B站音频/视频，无需登录；重复 ingest 会复用已有 audio/video 文件 |
| 搜索 | B站 WBI 签名直连可用；无 cookie 时 4 req/s 以内稳定 |

## 七、当前状态（2026-10-25）

- ✅ B站搜索（WBI）、入库、评分、时长过滤、SQLite + trigram FTS
- ✅ yt-dlp 下载 → faster-whisper 转写（tiny 验证通过；生产用 small）
- ✅ ffmpeg 场景抽帧 → rapidocr OCR
- 🟡 抖音链接导入：代码已支持（yt-dlp），未实测；搜索/热榜请用 whale_dive_douyin
- 🟡 语义检索：当前是 SQLite trigram FTS + 子串兜底；不是 embedding，但中文短词可用
