#!/usr/bin/env python
r"""采源 Harvest - 视频知识采集与检索 (Bilibili / 抖音链接).

核心流程: plan -> search (快) -> pick -> ingest (转写 + 抽帧 OCR) -> digest -> note -> kb
数据目录: %USERPROFILE%\.dsh\harvest  (可用 HARVEST_HOME 覆盖)
"""
from __future__ import annotations

import argparse, hashlib, json, math, os, re, shutil, sqlite3, subprocess, sys, time, urllib.parse
from datetime import datetime
from pathlib import Path

import requests

JSON_MODE = False


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def rowdict(r) -> dict:
    return {k: r[k] for k in r.keys()}

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOME = Path(os.environ.get("HARVEST_HOME") or Path.home() / ".dsh" / "harvest")
DATA = HOME / "data"
MEDIA = HOME / "media"
DB_PATH = DATA / "harvest.db"
PROFILE_PATH = HOME / "profile.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/", "Accept": "application/json"}

MIXIN_KEY_ENC_TAB = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,
                     14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,
                     56,59,6,63,57,62,11,36,20,34,44,52]

DEFAULT_PROFILE = {
    "interests": [], "must_include_any": [], "exclude": ["搬运", "广告", "合集"],
    "min_duration": 120, "max_duration": 3600, "platforms": ["bilibili"],
    "queries_per_brief": 5, "results_per_query": 20, "deep_ingest_top": 3,
    "whisper_model": "small",
    "ocr": {"scene_threshold": 0.25, "max_frames": 24, "min_conf": 0.5},
    "score_weights": {"play": 1.0, "like": 0.6, "recency": 0.5, "interest": 1.5, "duration": 1.0},
}


def profile() -> dict:
    if PROFILE_PATH.exists():
        try:
            return {**DEFAULT_PROFILE, **json.loads(PROFILE_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return dict(DEFAULT_PROFILE)


def db() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS videos (
        id TEXT PRIMARY KEY, platform TEXT, bvid TEXT, url TEXT,
        title TEXT, author TEXT, duration INTEGER, pubdate INTEGER,
        play INTEGER, like_ INTEGER, description TEXT, tags TEXT, query TEXT,
        score REAL DEFAULT 0, status TEXT DEFAULT 'new', added_at INTEGER,
        transcript TEXT, transcript_src TEXT, ocr_text TEXT, frames_dir TEXT,
        summary TEXT, summary_json TEXT, notes TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_status ON videos(status);
    CREATE TABLE IF NOT EXISTS feedback (id TEXT, verdict TEXT, reason TEXT, at INTEGER);
    CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT, seq INTEGER, start_sec INTEGER, end_sec INTEGER,
        kind TEXT, text TEXT, model TEXT, dim INTEGER, emb BLOB
    );
    CREATE INDEX IF NOT EXISTS idx_chunks_video ON chunks(video_id);
    """)
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(id UNINDEXED, title, description, transcript, ocr_text, summary, tokenize='trigram')")
    except sqlite3.OperationalError:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(id UNINDEXED, title, description, transcript, ocr_text, summary)")
    return conn


def fts_sync(conn: sqlite3.Connection, vid: str) -> None:
    row = conn.execute("SELECT title, description, transcript, ocr_text, summary FROM videos WHERE id=?", (vid,)).fetchone()
    conn.execute("DELETE FROM videos_fts WHERE id=?", (vid,))
    if row:
        conn.execute("INSERT INTO videos_fts (id, title, description, transcript, ocr_text, summary) VALUES (?,?,?,?,?,?)",
                     (vid, row[0] or "", row[1] or "", row[2] or "", row[3] or "", row[4] or ""))
    conn.commit()


# ---------------- bilibili WBI ----------------

_wbi_cache: dict = {}


def wbi_keys(session: requests.Session) -> tuple[str, str]:
    now = time.time()
    if _wbi_cache.get("at", 0) + 1800 > now:
        return _wbi_cache["img"], _wbi_cache["sub"]
    r = session.get("https://api.bilibili.com/x/web-interface/nav", headers=HEADERS, timeout=15)
    data = r.json().get("data", {})
    img_url = data["wbi_img"]["img_url"]
    sub_url = data["wbi_img"]["sub_url"]
    img = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub = sub_url.rsplit("/", 1)[-1].split(".")[0]
    _wbi_cache.update(at=now, img=img, sub=sub)
    return img, sub


def wbi_sign(img: str, sub: str, params: dict) -> dict:
    orig = img + sub
    mixin = "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]
    params = {k: v for k, v in params.items() if v is not None}
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return params


def clean_title(t: str) -> str:
    return re.sub(r"<[^>]+>", "", t or "").replace("&amp;", "&").strip()


def parse_duration(text: str) -> int:
    parts = [p for p in (text or "").split(":") if p]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    sec = 0
    for n in nums:
        sec = sec * 60 + n
    return sec


def bili_search(keyword: str, page: int = 1, order: str = "totalrank") -> list[dict]:
    s = requests.Session()
    img, sub = wbi_keys(s)
    params = wbi_sign(img, sub, {"search_type": "video", "keyword": keyword, "page": page, "order": order})
    r = s.get("https://api.bilibili.com/x/web-interface/wbi/search/type", params=params, headers=HEADERS, timeout=20)
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"bilibili search failed: {body.get('code')} {body.get('message')}")
    items = (body.get("data") or {}).get("result") or []
    out = []
    for it in items:
        if it.get("type") and it.get("type") != "video":
            continue
        bvid = it.get("bvid") or ""
        if not bvid:
            continue
        out.append({
            "bvid": bvid, "title": clean_title(it.get("title")),
            "author": it.get("author") or "", "url": f"https://www.bilibili.com/video/{bvid}",
            "duration": parse_duration(it.get("duration")), "pubdate": int(it.get("pubdate") or 0),
            "play": int(it.get("play") or 0), "like": int(it.get("like") or 0),
            "description": (it.get("description") or "").strip(),
            "tags": [t.strip() for t in (it.get("tag") or "").split(",") if t.strip()][:12],
        })
    return out


# ---------------- scoring ----------------

def score_item(item: dict, p: dict) -> float:
    w = p.get("score_weights", {})
    play = math.log1p(max(item.get("play", 0), 0))
    like = math.log1p(max(item.get("like", 0), 0))
    age_days = max((time.time() - item.get("pubdate", 0)) / 86400, 1)
    recency = 1.0 / math.log1p(age_days)
    text = (item.get("title", "") + " " + item.get("description", "")).lower()
    interests = [i.lower() for i in p.get("interests", []) if i]
    hits = sum(1 for i in interests if i in text)
    interest = hits / max(len(interests), 1)
    dur = item.get("duration", 0)
    duration_fit = 1.0 if p.get("min_duration", 0) <= dur <= p.get("max_duration", 10**9) else 0.0
    s = (w.get("play", 1) * play + w.get("like", 0.6) * like + w.get("recency", 0.5) * recency
         + w.get("interest", 1.5) * interest + w.get("duration", 1.0) * duration_fit)
    for bad in p.get("exclude", []):
        if bad and bad.lower() in text:
            s -= 3.0
    for must in p.get("must_include_any", []):
        if must and must.lower() not in text:
            s -= 2.0
            break
    return round(s, 3)


def upsert_search(rows: list[dict], query: str, p: dict) -> tuple[int, int]:
    conn = db()
    added = updated = 0
    now = int(time.time())
    for it in rows:
        vid = f"bili:{it['bvid']}"
        s = score_item(it, p)
        cur = conn.execute("SELECT id FROM videos WHERE id=?", (vid,)).fetchone()
        if cur:
            conn.execute("UPDATE videos SET play=?, like_=?, score=?, query=? WHERE id=?",
                         (it["play"], it["like"], s, query, vid))
            updated += 1
        else:
            conn.execute("""INSERT INTO videos (id, platform, bvid, url, title, author, duration, pubdate,
                            play, like_, description, tags, query, score, status, added_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (vid, "bilibili", it["bvid"], it["url"], it["title"], it["author"], it["duration"],
                          it["pubdate"], it["play"], it["like"], it["description"],
                          json.dumps(it["tags"], ensure_ascii=False), query, s, "new", now))
            added += 1
        fts_sync(conn, vid)
    conn.close()
    return added, updated


# ---------------- ingest ----------------

def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def ytdlp(*args: str) -> subprocess.CompletedProcess:
    return run([sys.executable, "-m", "yt_dlp", "--no-warnings", *args])


def download_audio(url: str, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    for f in work.glob("audio.*"):
        if f.suffix.lower() in (".m4a", ".webm", ".mp3", ".opus", ".wav"):
            return f
    target = work / "audio.%(ext)s"
    cp = ytdlp("-f", "bestaudio/best", "-x", "--audio-format", "m4a", "-o", str(target), url)
    if cp.returncode != 0:
        raise RuntimeError("yt-dlp audio failed: " + (cp.stderr or cp.stdout)[-600:])
    for f in work.glob("audio.*"):
        if f.suffix.lower() in (".m4a", ".webm", ".mp3", ".opus", ".wav"):
            return f
    raise RuntimeError("audio file not found after download")


def download_video(url: str, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    for f in work.glob("video.*"):
        if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".flv"):
            return f
    target = work / "video.%(ext)s"
    cp = ytdlp("-f", "bv*[height<=1080]+ba/b[height<=1080]", "-o", str(target), url)
    if cp.returncode != 0:
        raise RuntimeError("yt-dlp video failed: " + (cp.stderr or cp.stdout)[-600:])
    for f in work.glob("video.*"):
        if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".flv"):
            return f
    raise RuntimeError("video file not found after download")


def decode_to_array(path: Path, sr: int = 16000):
    import wave
    import numpy as np
    wav = path.with_suffix(".16k.wav")
    cp = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
              "-ac", "1", "-ar", str(sr), "-f", "wav", str(wav)])
    if cp.returncode != 0:
        raise RuntimeError("ffmpeg decode failed: " + (cp.stderr or "")[-400:])
    with wave.open(str(wav), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0


def transcribe(audio: Path, model_name: str) -> tuple[str, list[dict]]:
    from faster_whisper import WhisperModel
    os.environ.setdefault("HF_HOME", str(HOME / "models"))
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=4)
    audio_array = decode_to_array(audio)
    segments, info = model.transcribe(audio_array, language="zh", vad_filter=True, beam_size=1)
    lines, cues = [], []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        lines.append(f"[{int(seg.start)//60:02d}:{int(seg.start)%60:02d}] {text}")
        cues.append({"from": round(seg.start, 2), "to": round(seg.end, 2), "text": text})
    return "\n".join(lines), cues


def frame_signature(path: Path):
    import numpy as np
    import cv2
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    small = cv2.resize(img, (8, 8), interpolation=cv2.INTER_AREA)
    return (small > small.mean()).flatten()


def unique_frames(frames: list[Path], min_distance: int = 6) -> list[Path]:
    import numpy as np
    kept, sigs = [], []
    for f in frames:
        sig = frame_signature(f)
        if sig is None:
            continue
        if any(int(np.count_nonzero(sig != prev)) < min_distance for prev in sigs):
            continue
        kept.append(f)
        sigs.append(sig)
    return kept


def extract_frames(video: Path, outdir: Path, threshold: float, max_frames: int, mode: str = "scene") -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    for old in outdir.glob("*.jpg"):
        old.unlink()
    pattern = str(outdir / "f_%03d.jpg")
    if mode == "ui":
        vf = "fps=1/6,scale=1920:-2"
        cp = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
                  "-vf", vf, "-frames:v", str(max(max_frames, 40)), pattern])
        return sorted(outdir.glob("f_*.jpg"))
    vf = f"select='gt(scene,{min(threshold, 0.18)})',scale=1920:-2"
    cp = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
              "-vf", vf, "-vsync", "vfr", "-frames:v", str(max(max_frames, 40)), pattern])
    frames = sorted(outdir.glob("f_*.jpg"))
    if len(frames) < 12:
        for old in frames:
            old.unlink()
        cp = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
                  "-vf", "fps=1/25,scale=1920:-2", "-frames:v", str(max_frames), pattern])
        frames = sorted(outdir.glob("f_*.jpg"))
    return unique_frames(frames)


def classify_frame(path: Path, ocr_lines: int) -> str:
    import cv2
    import numpy as np
    img = cv2.imread(str(path))
    if img is None:
        return "unknown"
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 80, 160)
    edge_ratio = float(edges.mean()) / 255.0
    faces = ()
    try:
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if not cascade.empty():
            faces = cascade.detectMultiScale(gray, 1.1, 4)
    except Exception:
        faces = ()
    if len(faces) and ocr_lines < 4:
        return "face"
    if ocr_lines >= 10:
        return "code/text"
    if ocr_lines >= 3 and edge_ratio < 0.05:
        return "slide"
    if edge_ratio >= 0.03:
        return "diagram"
    return "other"


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def ocr_ui_frames(frames, min_conf: float):
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    per = []
    for f in frames:
        try:
            result, _ = engine(str(f))
        except Exception:
            continue
        texts = []
        for _, txt, conf in (result or []):
            txt = (txt or "").strip()
            if txt and len(txt) >= 2 and conf >= min_conf:
                texts.append(txt)
        if texts:
            per.append({"frame": f.name, "text": texts})
    kept = []
    for item in per:
        s = set(item["text"])
        if any(_jaccard(s, set(k["text"])) >= 0.9 for k in kept):
            continue
        kept.append(item)
    text = "\n".join(dict.fromkeys(x for k in kept for x in k["text"]))
    return text, kept


def load_vlm_config():
    import os
    cfg = {}
    path = HOME / "vlm.json"
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    base = os.environ.get("VLM_BASE_URL") or cfg.get("base_url")
    key = os.environ.get("VLM_API_KEY") or cfg.get("api_key")
    model = os.environ.get("VLM_MODEL") or cfg.get("model")
    if not (base and key and model):
        return None
    return {"base_url": base.rstrip("/"), "api_key": key, "model": model}


def caption_frame(fp: Path, cfg: dict):
    import base64
    import requests
    try:
        b64 = base64.b64encode(fp.read_bytes()).decode()
        payload = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                {"type": "text", "text": "先用一句话说明这张图在讲什么，再逐行列出图中出现的功能、模块、参数或步骤。只输出内容，不要客套。"},
            ]}],
            "max_tokens": 220,
        }
        r = requests.post(cfg["base_url"] + "/chat/completions",
                          headers={"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"},
                          json=payload, timeout=60)
        if r.status_code != 200:
            return None
        return (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()[:500]
    except Exception:
        return None


def write_manifest(work: Path, frames: list[Path], per_frame: list[dict]) -> Path:
    texts = {x["frame"]: x["text"] for x in per_frame}
    items = []
    for i, fp in enumerate(frames):
        lines = texts.get(fp.name, [])
        items.append({
            "file": fp.name,
            "index": i,
            "approx_second": i * 25,
            "type": classify_frame(fp, len(lines)),
            "ocr": lines[:12],
        })
    out = work / "frames" / "manifest.json"
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def ocr_frames(frames: list[Path], min_conf: float) -> tuple[str, list[dict]]:
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    raw = []
    for f in frames:
        try:
            result, _ = engine(str(f))
        except Exception:
            continue
        raw.append((f, [(str(txt).strip(), float(conf)) for _, txt, conf in (result or []) if txt]))

    def norm(s: str) -> str:
        return re.sub(r"[\s，。,.、：:；;！!？?（）()\[\]【】\-—_]+", "", s).lower()

    freq: dict[str, int] = {}
    for _, items in raw:
        for txt, conf in items:
            if conf >= min_conf and len(txt) >= 2:
                freq[norm(txt)] = freq.get(norm(txt), 0) + 1
    from difflib import SequenceMatcher

    limit = max(2, int(len(frames) * 0.6))
    kept: list[str] = []
    per_frame = []
    for f, items in raw:
        texts = []
        for txt, conf in items:
            key = norm(txt)
            if conf < min_conf or len(txt) < 2 or freq.get(key, 0) >= limit:
                continue
            dup = None
            for j, prev in enumerate(kept):
                if SequenceMatcher(None, key, norm(prev)).ratio() >= 0.70:
                    dup = j
                    break
            if dup is not None:
                if len(txt) > len(kept[dup]):
                    kept[dup] = txt
                continue
            kept.append(txt)
            texts.append(txt)
        if texts:
            per_frame.append({"frame": f.name, "text": texts})
    kept = kept[:60]
    return "\n".join(kept), per_frame


def ingest(vid: str, asr: bool = True, frames: bool = False, model: str | None = None,
           cookies_browser: str | None = None, force: bool = False) -> None:
    conn = db()
    if force:
        if asr:
            conn.execute("UPDATE videos SET transcript=NULL WHERE id=?", (vid,))
        if frames:
            conn.execute("UPDATE videos SET ocr_text=NULL WHERE id=?", (vid,))
        conn.commit()
    row = conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    if not row:
        raise SystemExit(f"unknown id: {vid} (先 search 或 import)")
    url = row["url"]
    work = MEDIA / re.sub(r"[^A-Za-z0-9_.-]", "_", vid)
    work.mkdir(parents=True, exist_ok=True)
    model = model or profile().get("whisper_model", "small")

    if asr and not row["transcript"]:
        print(f"[ingest] {vid} 下载音频 ...")
        audio = download_audio(url, work)
        print(f"[ingest] 转写 {audio.name} (model={model}) ...")
        text, cues = transcribe(audio, model)
        (work / "transcript.txt").write_text(text, encoding="utf-8")
        (work / "transcript.json").write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
        conn.execute("UPDATE videos SET transcript=?, transcript_src='asr' WHERE id=?", (text, vid))
        conn.commit()

    if frames:
        print(f"[ingest] {vid} 下载视频并抽帧 ...")
        video = download_video(url, work)
        cfg = profile().get("ocr", {})
        fdir = work / "frames"
        vtype = classify_video(row["title"], row["description"])
        mode = "ui" if vtype in ("tutorial", "course") else "scene"
        frames_list = extract_frames(video, fdir, float(cfg.get("scene_threshold", 0.25)), int(cfg.get("max_frames", 24)), mode)
        print(f"[ingest] {len(frames_list)} 帧，OCR ...")
        if mode == "ui":
            text, per = ocr_ui_frames(frames_list, float(cfg.get("min_conf", 0.5)))
            frames_list = [fdir / x["frame"] for x in per]
        else:
            text, per = ocr_frames(frames_list, float(cfg.get("min_conf", 0.5)))
        (work / "ocr.txt").write_text(text, encoding="utf-8")
        (work / "ocr.json").write_text(json.dumps(per, ensure_ascii=False), encoding="utf-8")
        manifest = write_manifest(work, frames_list, per)
        vlm = load_vlm_config()
        if vlm:
            try:
                items = json.loads(manifest.read_text(encoding="utf-8"))
                n = 0
                cap_types = ("diagram", "slide", "code/text") if vtype in ("tutorial", "course") else ("diagram", "slide")
                for it in items:
                    if it.get("type") in cap_types and n < 20:
                        cap = caption_frame(fdir / it["file"], vlm)
                        if cap:
                            it["caption"] = cap
                            n += 1
                manifest.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
                print("[ingest] 图注 " + str(n) + " 张")
            except Exception as e:
                print("[ingest] 图注失败: " + str(e)[:80])
        conn.execute("UPDATE videos SET ocr_text=?, frames_dir=? WHERE id=?", (text, str(fdir), vid))
        conn.commit()

    conn.execute("UPDATE videos SET status='ingested' WHERE id=? AND status IN ('new','ingesting')", (vid,))
    conn.commit()
    fts_sync(conn, vid)
    conn.close()
    print(f"[ingest] done {vid}")


# ---------------- output ----------------

def fmt_dur(sec: int) -> str:
    return f"{sec//60}:{sec%60:02d}" if sec else "?"


def print_rows(rows: list[sqlite3.Row], limit: int = 40) -> None:
    for i, r in enumerate(rows[:limit], 1):
        print(f"{i:>3}. [{r['score']:>6.2f}] {r['title'][:70]}")
        print(f"      {r['author'][:24]} | {fmt_dur(r['duration'] or 0)} | play={r['play']} | {r['url']}")


def digest(vid: str, max_chars: int, quiet: bool = False) -> str:
    conn = db()
    r = conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not r:
        raise SystemExit(f"unknown id: {vid}")
    parts = [
        f"# {r['title']}",
        f"- url: {r['url']}",
        f"- author: {r['author']} | duration: {fmt_dur(r['duration'] or 0)} | play: {r['play']} | like: {r['like_']}",
        f"- tags: {', '.join(json.loads(r['tags'] or '[]'))}",
        f"- desc: {(r['description'] or '')[:300]}",
    ]
    if r["transcript"]:
        parts += ["", f"## 文案转写 ({r['transcript_src'] or 'unknown'})", r["transcript"][:max_chars]]
    if r["frames_dir"]:
        man = Path(r["frames_dir"]) / "manifest.json"
        if man.exists():
            try:
                items = json.loads(man.read_text(encoding="utf-8"))
                lines = ["", "## 图文资产（" + str(len(items)) + " 帧）"]
                for it in items[:24]:
                    snippet = " / ".join(it.get("ocr", []))[:60]
                    cap = (" | " + it["caption"][:100]) if it.get("caption") else ""
                    lines.append("- [" + it.get("type", "") + "] t≈" + str(it.get("approx_second", 0)) + "s " + snippet + cap + " (" + str(Path(r["frames_dir"]) / it["file"]) + ")")
                parts += lines
            except Exception:
                pass
    if r["ocr_text"]:
        parts += ["", "## 画面文字 (OCR)", r["ocr_text"][: max_chars // 2]]
    if r["summary"]:
        parts += ["", "## 已存摘要", r["summary"]]
    out = "\n".join(parts)
    if not quiet:
        print(out)
    cached = MEDIA / re.sub(r"[^A-Za-z0-9_.-]", "_", vid)
    cached.mkdir(parents=True, exist_ok=True)
    (cached / "digest.md").write_text(out, encoding="utf-8")
    return out


def note(vid: str, summary: str, tags: str | None, json_text: str | None) -> None:
    conn = db()
    if not conn.execute("SELECT 1 FROM videos WHERE id=?", (vid,)).fetchone():
        raise SystemExit(f"unknown id: {vid}")
    conn.execute("UPDATE videos SET summary=?, notes=?, status='summarized' WHERE id=?",
                 (summary, tags or "", vid))
    if json_text:
        conn.execute("UPDATE videos SET summary_json=? WHERE id=?", (json_text, vid))
    conn.commit()
    fts_sync(conn, vid)
    conn.close()
    if JSON_MODE:
        emit({"ok": True, "id": vid})
    else:
        print(f"[note] saved {vid}")


def _fuzzy(conn, q: str, limit: int):
    base = re.sub(r"\s+", "", q)
    if len(base) < 2:
        return []
    shingles = list(dict.fromkeys(base[i:i + 2] for i in range(len(base) - 1)))
    rows = conn.execute("SELECT * FROM videos").fetchall()
    scored = []
    for r in rows:
        body = " ".join([r["description"] or "", r["transcript"] or "", r["summary"] or ""])
        head = " ".join([r["title"] or "", r["ocr_text"] or ""])
        weight = sum(2 for s in shingles if s in body) + sum(1 for s in shingles if s in head)
        coverage = sum(1 for s in shingles if s in body or s in head) / len(shingles)
        if coverage >= 0.5:
            scored.append((coverage, weight, r["score"] or 0, r))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    out = []
    for _, _, _, r in scored[:limit]:
        d = dict(r)
        src = d.get("summary") or d.get("transcript") or ""
        pos = -1
        for s in shingles:
            pos = src.find(s)
            if pos >= 0:
                break
        d["snip"] = src[max(0, pos - 40):pos + 120] if pos >= 0 else (src[:120])
        out.append(d)
    return out


EMBED_CONFIG = HOME / "embed.json"


def load_embed_config():
    import os
    cfg = {}
    if EMBED_CONFIG.exists():
        try:
            cfg = json.loads(EMBED_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    base = os.environ.get("EMBED_BASE_URL") or cfg.get("base_url")
    key = os.environ.get("EMBED_API_KEY") or cfg.get("api_key")
    model = os.environ.get("EMBED_MODEL") or cfg.get("model")
    if not (base and key and model):
        return None
    return {"base_url": base.rstrip("/"), "api_key": key, "model": model}


def embed_texts(cfg, texts):
    import requests
    out = []
    for i in range(0, len(texts), 16):
        batch = texts[i:i + 16]
        r = requests.post(cfg["base_url"] + "/embeddings",
                          headers={"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"},
                          json={"model": cfg["model"], "input": batch}, timeout=60)
        if r.status_code != 200:
            raise RuntimeError("embedding failed: " + str(r.status_code) + " " + r.text[:160])
        out.extend([d["embedding"] for d in r.json()["data"]])
    return out


def parse_transcript(text: str):
    lines = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*\[(\d{1,2}):(\d{2})\]\s*(.+)$", line)
        if m:
            lines.append((int(m.group(1)) * 60 + int(m.group(2)), m.group(3).strip()))
        elif line.strip():
            lines.append((None, line.strip()))
    return lines


def build_chunks(row):
    import json as _json
    chunks = []
    lines = parse_transcript(row["transcript"] or "")
    cur, start = [], None
    last_t = 0
    for sec, text in lines:
        if start is None:
            start = sec if sec is not None else 0
        cur.append(text)
        if sec is not None:
            last_t = sec
        if sum(len(x) for x in cur) >= 380 or (sec is not None and sec - start >= 100):
            chunks.append((start, last_t, "transcript", " ".join(cur)))
            cur, start = [], None
    if cur:
        chunks.append((start or 0, last_t, "transcript", " ".join(cur)))
    if row["summary"]:
        chunks.append((0, row["duration"] or 0, "summary", row["summary"]))
    if row["frames_dir"]:
        man = Path(row["frames_dir"]) / "manifest.json"
        if man.exists():
            try:
                for it in json.loads(man.read_text(encoding="utf-8")):
                    parts = it.get("ocr") or []
                    txt = " ".join(parts)[:300]
                    if it.get("caption"):
                        txt = (it["caption"] + " " + txt)[:400]
                    if txt:
                        chunks.append((it.get("approx_second", 0), it.get("approx_second", 0) + 20, "figure", txt))
            except Exception:
                pass
    return chunks


def parse_frontmatter(text: str):
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    meta = {}
    for line in head.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        val = val.strip()
        try:
            meta[key.strip()] = json.loads(val)
        except Exception:
            meta[key.strip()] = val.strip('"')
    return meta, body.lstrip("\n")


def cmd_import_notes(a) -> None:
    root = Path(a.dir)
    files = sorted(root.glob("*.md")) if root.exists() else []
    if not files:
        print("no markdown notes under " + str(root))
        return
    conn = db()
    n = 0
    for fp in files:
        text = fp.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        aweme = str(meta.get("aweme_id") or "").strip()
        if not aweme:
            m = re.search(r"(\d{6,30})", str(meta.get("play_url") or ""))
            aweme = m.group(1) if m else ""
        if not aweme:
            continue
        title = str(meta.get("title") or meta.get("description") or aweme)[:200]
        url = "https://www.douyin.com/video/" + aweme
        vid = "douyin:" + aweme
        transcript = (meta.get("transcript") or "")
        if not transcript:
            transcript = body.strip()[:20000]
        duration = int(float(meta.get("duration_seconds") or 0))
        author = str(meta.get("author") or "")
        conn.execute("""INSERT INTO videos (id, platform, bvid, url, title, author, duration, pubdate, play, like_,
                        description, tags, query, score, status, added_at, transcript, transcript_src)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET transcript=coalesce(nullif(excluded.transcript,''),videos.transcript),
                        status=CASE WHEN videos.status='new' THEN 'ingested' ELSE videos.status END""",
                     (vid, "douyin", aweme, url, title, author, duration, 0, 0, 0,
                      str(meta.get("description") or "")[:2000], "[]", "douyin-notes", 0, "ingested", int(time.time()),
                      transcript, str(meta.get("transcript_source") or "douyin-notes")))
        n += 1
    conn.commit()
    conn.close()
    print("imported notes: " + str(n))


def cmd_find(a) -> None:
    q = a.query
    limit = int(a.limit or 8)
    rows = []
    try:
        rows = bili_search(q, page=1)
    except Exception as e:
        if not JSON_MODE:
            print("[find] bilibili search failed: " + str(e)[:100])
    cfg = load_embed_config()
    scored = []
    if rows and cfg:
        try:
            texts = [(r["title"] + " " + (r["description"] or "")[:160]) for r in rows]
            vecs = embed_texts(cfg, texts)
            qv = embed_texts(cfg, [q])[0]
            import numpy as np
            qa = np.asarray(qv, dtype="float32")
            qn = np.linalg.norm(qa) or 1
            for r, v in zip(rows, vecs):
                va = np.asarray(v, dtype="float32")
                sim = float(va @ qa / ((np.linalg.norm(va) or 1) * qn))
                scored.append((sim, r))
            scored.sort(key=lambda x: -x[0])
        except Exception:
            scored = [(0.0, r) for r in rows]
    else:
        scored = [(0.0, r) for r in rows]
    results = []
    for sim, r in scored[:limit]:
        results.append({"id": "bili:" + r["bvid"], "title": r["title"], "url": r["url"],
                        "author": r["author"], "duration": r["duration"],
                        "concept_score": round(sim, 4)})
    local = []
    try:
        conn = db()
        local = [{"id": x["id"], "title": x["title"], "url": x["url"]} for x in kb_rows(conn, q, 3)]
        conn.close()
    except Exception:
        local = []
    if JSON_MODE:
        emit({"ok": True, "query": q, "results": results, "local": local})
    else:
        for i, r in enumerate(results, 1):
            print(str(i) + ". [" + str(r["concept_score"]) + "] " + r["title"][:60])
            print("   " + r["author"] + " | " + str(round((r["duration"] or 0) / 60)) + "m | " + r["url"])
        if local:
            print("本地命中:")
            for x in local:
                print("  - " + x["title"][:50])


def cmd_index(a) -> None:
    cfg = load_embed_config()
    if not cfg:
        print("未配置 embedding：写 ~/.dsh/harvest/embed.json 或设 EMBED_BASE_URL/EMBED_API_KEY/EMBED_MODEL")
        return
    conn = db()
    rows = conn.execute("SELECT * FROM videos WHERE length(coalesce(transcript,''))>0 OR (summary IS NOT NULL AND summary<>'')").fetchall()
    total = 0
    for r in rows:
        have = conn.execute("SELECT count(*) FROM chunks WHERE video_id=? AND model=?", (r["id"], cfg["model"])).fetchone()[0]
        if have:
            continue
        items = build_chunks(r)
        if not items:
            continue
        vecs = embed_texts(cfg, [x[3] for x in items])
        for seq, ((s, e, kind, txt), v) in enumerate(zip(items, vecs)):
            import numpy as np
            conn.execute("INSERT INTO chunks (video_id, seq, start_sec, end_sec, kind, text, model, dim, emb) VALUES (?,?,?,?,?,?,?,?,?)",
                         (r["id"], seq, s, e, kind, txt, cfg["model"], len(v), np.asarray(v, dtype="float32").tobytes()))
        total += len(items)
        print("  " + r["id"] + " chunks " + str(len(items)))
        conn.commit()
    conn.close()
    print("indexed chunks: " + str(total))


def semantic_rows(conn, q: str, limit: int, cfg: dict):
    import numpy as np
    rows = conn.execute("SELECT id, video_id, start_sec, kind, text, emb FROM chunks WHERE model=?", (cfg["model"],)).fetchall()
    if not rows:
        return []
    qv = np.asarray(embed_texts(cfg, [q])[0], dtype="float32")
    mat = np.vstack([np.frombuffer(r["emb"], dtype="float32") for r in rows])
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1
    sims = mat @ qv / (norms * (np.linalg.norm(qv) or 1))
    order = np.argsort(-sims)[:60]
    best = {}
    for idx in order:
        r = rows[int(idx)]
        cur = best.get(r["video_id"])
        if cur is None or sims[int(idx)] > cur[0]:
            best[r["video_id"]] = (float(sims[int(idx)]), r)
    # lexical ranks for fusion
    fts_order = []
    try:
        tokens = [x for x in re.split(r"\s+", q.strip()) if x]
        fts_q = " AND ".join('"' + tok.replace('"', "") + '"' for tok in tokens)
        fts_order = [x["id"] for x in conn.execute(
            "SELECT v.id FROM videos_fts JOIN videos v ON v.id=videos_fts.id WHERE videos_fts MATCH ? ORDER BY rank LIMIT 20",
            (fts_q,)).fetchall()]
    except Exception:
        fts_order = []
    fts_rank = {vid: i for i, vid in enumerate(fts_order)}
    scored = []
    for rank, (vid, (sim, r)) in enumerate(sorted(best.items(), key=lambda kv: -kv[1][0])):
        score = 1.0 / (60 + rank)
        if vid in fts_rank:
            score += 1.0 / (60 + fts_rank[vid])
        scored.append((score, vid, sim, r))
    scored.sort(key=lambda x: -x[0])
    import numpy as np2
    out = []
    for score, vid, sim, r in scored[:limit]:
        v = conn.execute("SELECT id,title,url,author,duration FROM videos WHERE id=?", (vid,)).fetchone()
        if not v:
            continue
        out.append({"id": v["id"], "title": v["title"], "url": v["url"], "author": v["author"],
                    "duration": v["duration"], "start_sec": r["start_sec"], "kind": r["kind"],
                    "score": round(score, 4), "sim": round(sim, 4),
                    "snip": "[" + str(r["start_sec"] // 60).zfill(2) + ":" + str(r["start_sec"] % 60).zfill(2) + "] " + (r["text"] or "")[:180]})
    return out


def kb_rows(conn, q: str, limit: int):
    cfg = load_embed_config()
    if cfg:
        try:
            sem = semantic_rows(conn, q, limit, cfg)
            if sem:
                return sem
        except Exception:
            pass
    rows = []
    tokens = [x for x in re.split(r"\s+", q.strip()) if x]
    try:
        fts_q = " AND ".join('"' + t.replace('"', "") + '"' for t in tokens)
        rows = conn.execute("""SELECT v.*, snippet(videos_fts, 1, '[', ']', '…', 16) AS snip
                               FROM videos_fts JOIN videos v ON v.id = videos_fts.id
                               WHERE videos_fts MATCH ? ORDER BY rank LIMIT ?""", (fts_q, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        clause = " OR ".join(["(title LIKE ? OR description LIKE ? OR transcript LIKE ? OR ocr_text LIKE ? OR summary LIKE ?)"] * len(tokens))
        params = []
        for tok in tokens:
            params += [f"%{tok}%"] * 5
        rows = conn.execute(f"SELECT *, substr(coalesce(transcript,''),1,120) AS snip FROM videos WHERE {clause} ORDER BY score DESC LIMIT ?",
                            (*params, limit)).fetchall()
    if not rows:
        picked = []
        for item in _fuzzy(conn, q, limit):
            d = item if isinstance(item, dict) else dict(zip(item.keys(), item))
            d["snip"] = (d.get("transcript") or d.get("summary") or "")[:120]
            picked.append(d)
        rows = picked
    return rows


def search_kb(q: str, limit: int) -> None:
    conn = db()
    rows = kb_rows(conn, q, limit)
    for r in rows:
        print(f"- {r['title']}\n  {r['url']}\n  {(' '.join((r['snip'] or '').split()))[:180]}")
    if not rows:
        print("(no match)")
    conn.close()


def status() -> None:
    conn = db()
    total = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
    by = conn.execute("SELECT status, count(*) c FROM videos GROUP BY status").fetchall()
    print(f"db: {DB_PATH}\nvideos: {total}")
    for r in by:
        print(f"  {r['status']}: {r['c']}")
    sample = conn.execute("SELECT title FROM videos ORDER BY added_at DESC LIMIT 3").fetchall()
    for s in sample:
        print("  latest:", s["title"][:60])
    conn.close()


# ---------------- commands ----------------

def cmd_plan(a) -> None:
    p = profile()
    brief = a.brief
    seeds = a.queries or []
    if not seeds:
        base = [brief]
        base += [f"{brief} {kw}" for kw in (p.get("interests") or [])[:2]]
        base += [f"{brief} 教程", f"{brief} 实战", f"{brief} 原理", f"{brief} 源码解析", f"{brief} 避坑"]
        seeds = base[: int(p.get("queries_per_brief", 5))]
    plan = {
        "brief": brief, "queries": seeds,
        "filters": {k: p.get(k) for k in ("min_duration", "max_duration", "exclude", "must_include_any")},
        "results_per_query": p.get("results_per_query", 20),
        "deep_ingest_top": p.get("deep_ingest_top", 3),
    }
    if JSON_MODE:
        emit({"ok": True, **plan})
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=2))


def cmd_search(a) -> None:
    p = profile()
    total_added = 0
    for q in a.query:
        rows = bili_search(q, page=a.page, order=a.order)
        added, upd = upsert_search(rows, q, p)
        total_added += added
        if not JSON_MODE:
            print(f"[search] {q!r}: {len(rows)} results, +{added} / ~{upd}")
    conn = db()
    if a.all:
        rows = conn.execute("SELECT * FROM videos ORDER BY score DESC LIMIT ?", (a.limit,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM videos WHERE duration BETWEEN ? AND ? ORDER BY score DESC LIMIT ?",
                            (p.get("min_duration", 0), p.get("max_duration", 10**9), a.limit)).fetchall()
    if JSON_MODE:
        emit({"ok": True, "added": total_added, "rows": [{
            "id": r["id"], "title": r["title"], "author": r["author"], "duration": r["duration"],
            "url": r["url"], "play": r["play"], "like": r["like_"], "score": r["score"], "status": r["status"],
        } for r in rows]})
    else:
        print_rows(rows, a.limit)
        print(f"\ntotal new: {total_added}")
    conn.close()


def cmd_pick(a) -> None:
    conn = db()
    for vid in a.ids:
        conn.execute("UPDATE videos SET status='selected' WHERE id=?", (vid,))
    conn.commit()
    print("selected:", ", ".join(a.ids))
    conn.close()


def cmd_ingest(a) -> None:
    ids = a.ids
    if not ids:
        conn = db()
        rows = conn.execute("SELECT id FROM videos WHERE status IN ('selected','new') ORDER BY score DESC LIMIT ?", (a.top,)).fetchall()
        ids = [r["id"] for r in rows]
        conn.close()
    results = []
    for vid in ids:
        conn = db()
        conn.execute("UPDATE videos SET status='ingesting' WHERE id=?", (vid,))
        conn.commit()
        conn.close()
        try:
            ingest(vid, asr=not a.no_asr, frames=a.frames, model=a.model, force=a.force)
            results.append({"id": vid, "ok": True})
        except Exception as e:
            conn = db()
            conn.execute("UPDATE videos SET status='failed' WHERE id=?", (vid,))
            conn.commit()
            conn.close()
            results.append({"id": vid, "ok": False, "error": str(e)[:300]})
            if not JSON_MODE:
                print(f"[ingest] {vid} FAILED: {e}")
    if JSON_MODE:
        emit({"ok": all(r["ok"] for r in results), "results": results})


def cmd_import(a) -> None:
    url = a.url
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    vid = f"bili:{m.group(1)}" if m else "url:" + hashlib.md5(url.encode()).hexdigest()[:12]
    conn = db()
    if not conn.execute("SELECT 1 FROM videos WHERE id=?", (vid,)).fetchone():
        title = a.title or url
        conn.execute("""INSERT INTO videos (id, platform, bvid, url, title, author, duration, pubdate, play, like_,
                        description, tags, query, score, status, added_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (vid, "bilibili" if m else "web", m.group(1) if m else "", url, title, "", 0, 0, 0, 0,
                      "", "[]", "import", 0, "selected", int(time.time())))
        conn.commit()
        fts_sync(conn, vid)
    conn.close()
    if not JSON_MODE:
        print("imported:", vid)
    ingest(vid, asr=not a.no_asr, frames=a.frames, model=a.model)
    if JSON_MODE:
        emit({"ok": True, "id": vid})


def cmd_digest(a) -> None:
    out = digest(a.id, a.max_chars, quiet=JSON_MODE)
    if JSON_MODE:
        emit({"ok": True, "id": a.id, "markdown": out})


def cmd_note(a) -> None:
    text = a.summary
    if a.summary_file:
        text = Path(a.summary_file).read_text(encoding="utf-8")
    if not text:
        raise SystemExit("need --summary or --summary-file")
    note(a.id, text, a.tags, a.json)


def cmd_kb(a) -> None:
    if JSON_MODE:
        conn = db()
        rows = kb_rows(conn, a.query, a.limit)
        emit({"ok": True, "query": a.query, "results": [{"id": r["id"], "title": r["title"], "url": r["url"]} for r in rows]})
        conn.close()
    else:
        search_kb(a.query, a.limit)


def cmd_list(a) -> None:
    conn = db()
    rows = conn.execute("SELECT * FROM videos WHERE status=? ORDER BY score DESC LIMIT ?", (a.status, a.limit)).fetchall()
    print_rows(rows, a.limit)
    conn.close()


def cmd_status(a) -> None:
    if not JSON_MODE:
        status()
        return
    conn = db()
    total = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
    by = {r["status"]: r["c"] for r in conn.execute("SELECT status, count(*) c FROM videos GROUP BY status").fetchall()}
    running = [r["id"] for r in conn.execute("SELECT id FROM videos WHERE status='ingesting'").fetchall()]
    conn.close()
    emit({"ok": True, "total": total, "byStatus": by, "running": running, "db": str(DB_PATH)})


def load_rubrics() -> dict:
    import yaml
    path = Path(__file__).parent / "rubrics.yaml"
    if not path.exists():
        return {"common": [], "types": {}}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {"common": [], "types": {}}


def classify_video(title: str, desc: str) -> str:
    text = (title + " " + (desc or "")).lower()
    if any(k in text for k in ["面试", "高频题", "八股", "答题"]):
        return "interview"
    if any(k in text for k in ["综述", "论文", "survey", "篇论文", "研究进展"]):
        return "survey"
    if any(k in text for k in ["评测", "测评", "横评", "对比", "哪个好"]):
        return "review"
    if any(k in text for k in ["合集", "全集", "全套", "系统课", "课程", "第几期", "期"]):
        return "course"
    if any(k in text for k in ["教程", "实战", "手把手", "搭建", "部署", "项目", "入门到", "速成"]):
        return "tutorial"
    if any(k in text for k in ["为什么", "观点", "解读", "新闻", "热点", "思考"]):
        return "opinion"
    return "concept"


def media_size(vid: str) -> int:
    d = MEDIA / re.sub(r"[^A-Za-z0-9_.-]", "_", vid)
    if not d.exists():
        return 0
    total = 0
    for p in d.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


def auto_checks(row) -> dict:
    checks = {}
    trans = row["transcript"] or ""
    checks["transcript"] = (len(trans) >= 800, str(len(trans)) + " 字符")
    summary = row["summary"] or ""
    secs = ["一句话结论", "核心要点", "坑与前提"]
    present = sum(1 for s in secs if s in summary)
    if "图文" in summary or "画面文字" in summary:
        present += 1
    if "锚点" in summary or "原文锚点" in summary:
        present += 1
    checks["summary_sections"] = (present >= 5, str(present) + "/5")
    ocr = row["ocr_text"] or ""
    ocr_lines = len([x for x in ocr.splitlines() if x.strip()])
    if row["frames_dir"]:
        checks["ocr"] = (ocr_lines >= 5, str(ocr_lines) + " 行")
    else:
        checks["ocr"] = (True, "n/a（未抽帧）")
    size_mb = media_size(row["id"]) / 1048576.0
    dur = row["duration"] or 0
    ok_cost = size_mb <= 80 and 120 <= dur <= 3600
    checks["cost"] = (ok_cost, str(round(size_mb, 1)) + " MB / " + str(dur) + "s")
    return checks


def cmd_audit(a) -> None:
    rub = load_rubrics()
    conn = db()
    if a.ids:
        rows = [conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone() for vid in a.ids]
        rows = [r for r in rows if r]
    else:
        status = a.status
        rows = conn.execute("SELECT * FROM videos WHERE status=? ORDER BY score DESC LIMIT ?", (status, a.limit)).fetchall()
    if not rows:
        print("(no rows)")
        return
    summary = []
    for r in rows:
        vtype = classify_video(r["title"], r["description"])
        type_cfg = (rub.get("types") or {}).get(vtype, {})
        checks = auto_checks(r)
        passed = sum(1 for ok, _ in checks.values() if ok)
        total = len(checks)
        print("## " + r["title"][:60])
        print("类型: " + vtype + "（" + str(type_cfg.get("label", "")) + "） | 自动项 " + str(passed) + "/" + str(total))
        for cid, (ok, detail) in checks.items():
            print("  [" + ("PASS" if ok else "FAIL") + "] " + cid + ": " + detail)
        judged = [c for c in (rub.get("common") or []) if c.get("kind") == "judged"]
        judged += [c for c in (type_cfg.get("criteria") or []) if c.get("kind") == "judged"]
        print("  待模型判定: " + ", ".join(c["id"] + "(" + c["name"] + ")" for c in judged))
        print("")
        summary.append((vtype, passed, total))
    print("----")
    print("汇总: " + str(len(summary)) + " 条；自动项全过 " + str(sum(1 for _, p, t in summary if p == t)) + " 条")
    conn.close()


TYPE_QUERIES = {
    "concept": ["原理", "是什么"],
    "tutorial": ["教程", "实战"],
    "survey": ["综述", "论文"],
    "interview": ["面试"],
    "review": ["对比", "评测"],
    "course": ["系统课"],
    "opinion": ["观点"],
}


def cmd_learn(a) -> None:
    brief = a.brief
    hours = max(0.25, min(4.0, float(a.hours)))
    types = [x.strip() for x in (a.types or "concept,tutorial,survey,interview,review").split(",") if x.strip()]
    target_sec = int(hours * 3600)
    p = profile()
    conn = db()
    queries = []
    for tp in types:
        for suffix in TYPE_QUERIES.get(tp, ["教程"]):
            queries.append(brief + " " + suffix)
    print("[learn] 目标 " + str(hours) + " 小时；查询 " + str(len(queries)) + " 组")
    for q in queries:
        try:
            rows = bili_search(q, page=1)
            added, _ = upsert_search(rows, q, p)
            print("  " + q + " -> +" + str(added))
        except Exception as e:
            print("  " + q + " FAILED: " + str(e)[:80])
    rows = conn.execute("SELECT * FROM videos WHERE status='new' AND duration BETWEEN 180 AND 1800 ORDER BY score DESC").fetchall()
    keys = [x.lower() for x in re.split(r"\s+", brief) if len(x) >= 2]
    def relevant(r):
        text = ((r["title"] or "") + " " + (r["description"] or "") + " " + (r["tags"] or "")).lower()
        return any(k in text for k in keys) if keys else True
    by_type = {}
    for r in rows:
        if not relevant(r):
            continue
        tp = classify_video(r["title"], r["description"])
        if tp in types:
            by_type.setdefault(tp, []).append(r)
    picked, secs = [], 0
    queues = {tp: list(by_type.get(tp, [])) for tp in types}
    used = set()
    while secs < target_sec:
        progressed = False
        for tp in types:
            choice = None
            for r in queues[tp]:
                if r["id"] in used:
                    continue
                if (r["duration"] or 0) <= max(60, target_sec - secs + 90):
                    choice = r
                    break
            if choice is None:
                for r in queues[tp]:
                    if r["id"] not in used:
                        choice = r
                        break
            if choice is not None:
                used.add(choice["id"])
                picked.append(choice)
                secs += choice["duration"] or 0
                progressed = True
            if secs >= target_sec:
                break
        if not progressed:
            break
    plan = {
        "brief": brief, "hours": hours, "types": types,
        "selected": [{"id": r["id"], "title": r["title"], "duration": r["duration"], "type": classify_video(r["title"], r["description"])} for r in picked],
        "total_seconds": secs,
    }
    out = Path(a.out) if a.out else (HOME / "learn-plan.json")
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[learn] 选中 " + str(len(picked)) + " 条，合计 " + str(round(secs / 60)) + " 分钟；计划写入 " + str(out))
    for r in picked:
        print("  [" + classify_video(r["title"], r["description"]) + "] " + str(r["duration"]) + "s " + r["title"][:50])
    if a.run and picked:
        import subprocess as sp
        frame_types = {"tutorial", "course"}
        with_frames = [r for r in picked if classify_video(r["title"], r["description"]) in frame_types]
        plain = [r for r in picked if classify_video(r["title"], r["description"]) not in frame_types]
        groups = []
        if with_frames:
            groups.append(([r["id"] for r in with_frames], True))
        if plain:
            groups.append(([r["id"] for r in plain], False))
        procs = []
        for gi, (ids, want_frames) in enumerate(groups):
            log = open(HOME / ("learn-worker-" + str(gi) + ".log"), "w", encoding="utf-8")
            argv = [sys.executable, str(Path(__file__).resolve()), "ingest", "--ids", *ids]
            if want_frames:
                argv.append("--frames")
            proc = sp.Popen(argv, stdout=log, stderr=sp.STDOUT,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            procs.append(proc)
            print("[learn] worker " + str(gi) + " pid=" + str(proc.pid) + " n=" + str(len(ids)) + (" frames" if want_frames else ""))
        print("[learn] 后台转写已启动；日志 " + str(HOME / "learn-worker-*.log"))
    conn.close()


def cmd_caption(a) -> None:
    cfg = load_vlm_config()
    if not cfg:
        print("未配置视觉模型：写 ~/.dsh/harvest/vlm.json 或设 VLM_BASE_URL/VLM_API_KEY/VLM_MODEL")
        return
    conn = db()
    ids = a.ids or [r["id"] for r in conn.execute("SELECT id FROM videos WHERE frames_dir IS NOT NULL AND frames_dir<>''").fetchall()]
    conn.close()
    for vid in ids:
        work = MEDIA / re.sub(r"[^A-Za-z0-9_.-]", "_", vid) / "frames"
        man = work / "manifest.json"
        if not man.exists():
            print("  " + vid + " 无 manifest")
            continue
        items = json.loads(man.read_text(encoding="utf-8"))
        conn = db()
        row = conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        conn.close()
        vtype = classify_video(row["title"], row["description"]) if row else "concept"
        cap_types = ("diagram", "slide", "code/text") if vtype in ("tutorial", "course") else ("diagram", "slide")
        n = 0
        for it in items:
            if it.get("type") not in cap_types:
                continue
            if n >= int(a.limit):
                break
            cap = caption_frame(work / it["file"], cfg)
            if cap:
                it["caption"] = cap
                n += 1
        man.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  " + vid + " 图注 " + str(n) + " 张")


def cmd_pass(a) -> None:
    conn = db()
    if not conn.execute("SELECT 1 FROM videos WHERE id=?", (a.id,)).fetchone():
        raise SystemExit(f"unknown id: {a.id}")
    conn.execute("INSERT INTO feedback (id, verdict, reason, at) VALUES (?,?,?,?)",
                 (a.id, a.verdict, a.reason or "", int(time.time())))
    new_status = "selected" if a.verdict == "like" else "skipped"
    conn.execute("UPDATE videos SET status=? WHERE id=?", (new_status, a.id))
    conn.commit()
    conn.close()
    print(f"[pass] {a.id} -> {a.verdict}")


def cmd_tune(a) -> None:
    conn = db()
    rows = conn.execute("SELECT reason FROM feedback WHERE verdict='skip' AND reason<>''").fetchall()
    conn.close()
    counter: dict[str, int] = {}
    for r in rows:
        for token in re.split(r"[,，;；\s]+", r["reason"]):
            token = token.strip()
            if token:
                counter[token] = counter.get(token, 0) + 1
    if not counter:
        print("(还没有跳过理由；先 harvest pass --verdict skip --reason ...)")
        return
    print("跳过理由高频词（可考虑加进 profile.exclude）：")
    for token, n in sorted(counter.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {token}: {n}")


def cmd_export(a) -> None:
    conn = db()
    rows = conn.execute("SELECT * FROM videos WHERE status IN ('summarized','ingested') ORDER BY score DESC").fetchall()
    conn.close()
    lines = [f"# 采源导出 {datetime.now():%Y-%m-%d %H:%M}", ""]
    for r in rows:
        lines.append(f"## {r['title']}")
        lines.append(f"- {r['url']} | {r['author']} | {fmt_dur(r['duration'] or 0)} | play={r['play']}")
        if r["summary"]:
            lines.append("")
            lines.append(r["summary"])
        elif r["transcript"]:
            lines.append("")
            lines.append("> " + r["transcript"][:300].replace("\n", " "))
        lines.append("")
    out = "\n".join(lines)
    path = Path(a.out) if a.out else (HOME / f"export-{datetime.now():%Y%m%d-%H%M}.md")
    path.write_text(out, encoding="utf-8")
    print(f"[export] {path} ({len(rows)} items)")


def main() -> None:
    ap = argparse.ArgumentParser(prog="harvest", description="采源: 视频知识采集与检索")
    ap.add_argument("--json", action="store_true", help="机器可读输出（stdout 单个 JSON）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="把需求转成检索计划")
    sp.add_argument("--brief", required=True)
    sp.add_argument("--queries", nargs="*")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("search", help="B站快速检索并入库")
    sp.add_argument("--query", nargs="+", required=True)
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("--order", default="totalrank", choices=["totalrank", "click", "pubdate", "dm", "stow", "scores"])
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--all", action="store_true", help="忽略时长过滤")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("pick", help="标记要深采的条目")
    sp.add_argument("--ids", nargs="+", required=True)
    sp.set_defaults(func=cmd_pick)

    sp = sub.add_parser("ingest", help="转写 / 抽帧 OCR")
    sp.add_argument("--ids", nargs="*")
    sp.add_argument("--top", type=int, default=3)
    sp.add_argument("--frames", action="store_true")
    sp.add_argument("--no-asr", action="store_true")
    sp.add_argument("--model", default=None)
    sp.add_argument("--force", action="store_true", help="清掉已有转写/OCR 重采")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("import", help="按链接导入并采集")
    sp.add_argument("--url", required=True)
    sp.add_argument("--title", default=None)
    sp.add_argument("--frames", action="store_true")
    sp.add_argument("--no-asr", action="store_true")
    sp.add_argument("--model", default=None)
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("digest", help="输出采集包给模型总结")
    sp.add_argument("--id", required=True)
    sp.add_argument("--max-chars", type=int, default=6000)
    sp.set_defaults(func=cmd_digest)

    sp = sub.add_parser("note", help="写回摘要")
    sp.add_argument("--id", required=True)
    sp.add_argument("--summary", default=None)
    sp.add_argument("--summary-file", default=None)
    sp.add_argument("--tags", default=None)
    sp.add_argument("--json", default=None)
    sp.set_defaults(func=cmd_note)

    sp = sub.add_parser("kb", help="本地知识库检索")
    sp.add_argument("--query", required=True)
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_kb)

    sp = sub.add_parser("list", help="按状态列出")
    sp.add_argument("--status", default="new")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("learn", help="长学习会话：按类型配额采集 1-4 小时视频并并行转写")
    sp.add_argument("--brief", required=True)
    sp.add_argument("--hours", type=float, default=1.0)
    sp.add_argument("--types", default="concept,tutorial,survey,interview,review")
    sp.add_argument("--out", default=None)
    sp.add_argument("--run", action="store_true")
    sp.add_argument("--workers", type=int, default=1)
    sp.set_defaults(func=cmd_learn)

    sp = sub.add_parser("find", help="概念检索视频：关键词召回后用向量重排")
    sp.add_argument("--query", required=True)
    sp.add_argument("--limit", type=int, default=8)
    sp.set_defaults(func=cmd_find)

    sp = sub.add_parser("import-notes", help="导入 douyin-favorites 知识库的 Markdown 笔记")
    sp.add_argument("--dir", required=True)
    sp.set_defaults(func=cmd_import_notes)

    sp = sub.add_parser("index", help="为素材建向量索引（需配置 embedding）")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("caption", help="给已有帧生成图注（需配置视觉模型）")
    sp.add_argument("--ids", nargs="*")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_caption)

    sp = sub.add_parser("audit", help="按类型 rubric 做验收自检")
    sp.add_argument("--ids", nargs="*")
    sp.add_argument("--status", default="summarized")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("pass", help="反馈：喜欢/跳过")
    sp.add_argument("--id", required=True)
    sp.add_argument("--verdict", required=True, choices=["like", "skip"])
    sp.add_argument("--reason", default=None)
    sp.set_defaults(func=cmd_pass)

    sp = sub.add_parser("tune", help="汇总跳过理由，给出排除词建议")
    sp.set_defaults(func=cmd_tune)

    sp = sub.add_parser("export", help="导出已采条目为 markdown")
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("status", help="库状态")
    sp.set_defaults(func=cmd_status)

    a = ap.parse_args()
    global JSON_MODE
    JSON_MODE = bool(a.json)
    try:
        a.func(a)
    except SystemExit:
        raise
    except Exception as e:
        if JSON_MODE:
            emit({"ok": False, "error": {"code": type(e).__name__, "message": str(e)[:400]}})
            raise SystemExit(1)
        raise


if __name__ == "__main__":
    main()
