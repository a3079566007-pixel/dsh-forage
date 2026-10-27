---
name: video-harvest
description: 采源 —— 主动用概念检索找视频/文本，加工成知识：forage_find 概念召回 → add 加链接 → 转写/图注 → 摘要 → 概念检索；收藏夹视频用 douyin-favorites 导出后 import-notes。顺序执行，不做批量并行。当用户说"学一下XX""帮我找些讲XX的视频并总结""把这些视频整理成知识"时使用。
metadata:
  category: knowledge
  subcategory: 知识管理/视频采集/采源
---

# 采源（主动概念检索 + 单条加工）

## 一、主动循环

~~~powershell
$H="C:\Users\Administrator\.dsh\harvest\harvest.py"

# 1) 概念检索：关键词召回 + bge-m3 重排（换说法也能命中）
python $H find --query "怎样减少大模型胡说" --limit 8

# 2) 选中一条，加进来并转写
python $H import --url "https://www.bilibili.com/video/BVxxxx" --frames

# 3) 取材料写摘要
python $H digest --id bili:BVxxxx --max-chars 6000
python $H note --id bili:BVxxxx --summary-file sum.md --tags "RAG,评估"

# 4) 检索
python $H kb --query "检索结果不相关怎么办"
~~~

- 文本类补充用 web_search（Agent 自带），结果可人工并入摘要；
- 顺序执行，一次一条，不并行。

## 二、收藏夹视频

douyin-favorites-to-knowledge 负责登录/抓收藏/下载/转写并导出 Markdown：

~~~powershell
python $H import-notes --dir "C:\Users\Administrator\.dsh\tools\dfk-kb"
python $H index
~~~

## 三、检索与配置

- `embed.json`：默认本地 ONNX `bge-small-zh-v1.5`（512 维，离线、免费）；也可切回 API bge-m3（1024 维，质量更高，走 SiliconFlow）；没有则退回字面检索；
- `vlm.json`：图注；
- `rubrics.yaml`：验收判据；
- 新增内容后跑 `index` 补向量。

## 四、边界

- 主动检索是**概念召回**，不是无脑批量：先 find 看候选，再挑；
- 不做双 worker 并行（16G 内存会互相挤）；
- 抖音收藏依赖外部 skill 的登录态；
- 知识库/记忆库双写尚未实现（见架构 v2 文档）。
