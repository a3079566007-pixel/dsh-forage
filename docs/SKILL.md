---
name: video-harvest
description: 采源 —— 处理用户主动提供的视频：加链接 → 转写 → 抽帧/OCR/图注 → 摘要 → 概念检索。不做自动搜索与批量选片。当用户说"把这个视频整理一下""导入这批链接""查一下我库里的XX"时使用。
---

# 采源（用户选片，插件加工）

设计原则：**用户找视频，插件只做加工**。没有自动搜索、没有并行 worker、没有自动选片。

## 一、标准流程

~~~powershell
$H="C:\Users\Administrator\.dsh\harvest\harvest.py"

# 1) 加一条用户给的链接（B站；抖音走 import-notes）
python $H import --url "https://www.bilibili.com/video/BVxxxx" [--frames] [--model small]

# 2) 处理已入库但还没转写的条目（例如从抖音收藏笔记导入的）
python $H ingest --ids bili:BVxxxx [--frames]

# 3) 取材料包，交给模型写摘要
python $H digest --id bili:BVxxxx --max-chars 6000

# 4) 写回摘要
python $H note --id bili:BVxxxx --summary-file sum.md --tags "RAG,评估"

# 5) 概念检索
python $H kb --query "怎么防止模型胡说"
~~~

## 二、抖音收藏

用 douyin-favorites-to-knowledge 导出 Markdown 后导入：

~~~powershell
python $H import-notes --dir "C:\Users\Administrator\.dsh\tools\dfk-kb"
python $H index
~~~

（该 skill 负责登录、抓收藏、Referer 下载、SenseVoice 转写；我们只导入笔记。）

## 三、检索

- 有 `~/.dsh/harvest/embed.json` 时：FTS + bge-m3 向量 + RRF（概念命中），返回带时间戳片段；
- 没有时：退回字面检索（FTS + 中文二元组）；
- 新增内容后跑一次 `index` 补向量。

## 四、模型配置

| 文件 | 作用 |
|---|---|
| profile.json | 采集偏好（仅 CLI search 用，插件不依赖） |
| embed.json | 向量模型（bge-m3） |
| vlm.json | 看图模型（图注） |
| rubrics.yaml | 验收判据 |

## 五、边界

- 不做自动搜索/选片；用户给什么处理什么；
- 顺序执行，一次一条，避免抢内存；
- 长视频转写可能几分钟，属正常；
- 抖音收藏依赖外部 skill 的登录态，登录失效时先修登录。
