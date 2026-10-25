# dsh-forage · 采源

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![npm](https://img.shields.io/badge/package-0.1.0-blue)](package.json)


把视频"采"成可检索的知识：快扫候选、精选深采、转写口播、抽帧 OCR、写回摘要、本地模糊检索。

> **Forage**（v. 觅食、采集）——像觅食一样在视频流里挑有用的东西，而不是把整个平台搬回家。
> DSH 插件（宿主层）+ Python 引擎（转写 / OCR / SQLite）。本仓库是插件与引擎的合集。

## 为什么叫这个名字

- 中文 **采源**：采集信源、留存知识；
- 英文 **Forage**：主动觅食，强调"按偏好挑"，不是无差别抓取；
- 命名前检查过 npm 与 DSH 插件目录：`dsh-harvest` 已被 @stifer/dsh-harvest 占用，`dsh-forage` 无冲突。

## 能力

| 工具 | 作用 |
|---|---|
| `forage_plan` | 把方向性需求展开成检索计划（含时长/排除词过滤） |
| `forage_search` | B 站快扫元数据（WBI 签名，不下载），按播放/点赞/新鲜度/兴趣评分 |
| `forage_ingest` | 后台深采：音频转写（faster-whisper）+ 可选抽帧 OCR |
| `forage_digest` | 取材料包（元数据 + 转写 + OCR），交给模型写摘要 |
| `forage_note` | 写回摘要与标签，进入检索库 |
| `forage_kb` | 本地模糊检索（FTS + 中文二元组兜底） |
| `forage_status` | 库状态与进行中的采集 |

## 架构

```
DSH 插件（Node，薄壳）
  ├─ Config：pythonPath / scriptPath / whisperModel / defaultFrames / timeout
  └─ 7 个 defineTool ──spawn──> engine/harvest.py --json <cmd>
                                   ├─ B 站 WBI 搜索（requests）
                                   ├─ yt-dlp 下载 → faster-whisper 转写
                                   ├─ ffmpeg 抽帧（场景检测 + 哈希去重）→ rapidocr
                                   └─ SQLite（trigram FTS + 二元组模糊）
```

## 安装（DSH）

```powershell
# 1) 引擎：放到 ~/.dsh/harvest/
mkdir "$env:USERPROFILE\.dsh\harvest"
copy engine\harvest.py "$env:USERPROFILE\.dsh\harvest\harvest.py"
copy engine\profile.example.json "$env:USERPROFILE\.dsh\harvest\profile.json"

# 2) 插件：本地 link 安装
#    profile 的 package.json 里加依赖与 bundle，然后 pnpm install
```

依赖：Python 3.12、yt-dlp、ffmpeg、faster-whisper、rapidocr_onnxruntime。
国内网络：`HF_ENDPOINT=https://hf-mirror.com` + `HF_HUB_DISABLE_XET=1`（引擎已内置）。

## 配置

| 字段 | 默认 | 说明 |
|---|---|---|
| pythonPath | python | Python 可执行文件 |
| scriptPath | ~/.dsh/harvest/harvest.py | 引擎路径 |
| whisperModel | small | tiny 对中文技术词误差大 |
| defaultFrames | false | 默认是否抽帧 OCR |
| timeoutMs | 60000 | 单次工具超时 |
| ingestTimeoutMs | 1800000 | 预留 |

## 限制

- 抖音只支持链接导入（yt-dlp），搜索用 whale_dive_*；
- OCR 对竖屏低码率视频不准，横屏 1080p 可用；关键帧原图保留，可用视觉模型精读；
- ASR 专有名词会错（LoRA→RORA），摘要需按上下文校正；
- 长任务为 detached 进程，异常状态落 `failed`，可用 `harvest.py --json status` 排查。

## 目录

```
package.json          DSH 插件清单
cordis.patch.yml      loader 插桩
lib/index.js          宿主层（工具注册 + spawn）
lib/schemas.js        工具 schema（纯数据）
engine/harvest.py     Python 引擎（CLI + --json）
engine/profile.example.json
docs/SKILL.md         给模型看的使用说明
```

## License

MIT © 2026 mikejikichen
