"""
Auto Video Studio PRO V6
- Offline Thai speech-to-text with faster-whisper (CPU)
- Timed ASS karaoke captions (when word timestamps are available)
- Keyword-matched user-uploaded B-roll and slide-in popups
- Preserves original voice, Streamlit Cloud-friendly 720p default

Run: streamlit run v6_app.py
"""
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import streamlit as st
from PIL import Image, ImageOps, ImageDraw

APP_TITLE = "Auto Video Studio PRO V6"
MAX_UPLOAD_MB = 120
MAX_VIDEO_SECONDS = 60
MAX_IMAGES = 5

st.set_page_config(page_title=APP_TITLE, page_icon="🎬", layout="centered")
st.title("🎬 " + APP_TITLE)
st.caption("คลิปผู้พูด + ซับไทยอัตโนมัติ + Karaoke + ภาพเด้งตามคำพูด • แนวคลิปอธิบายแบบมืออาชีพ")

if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    st.error("ไม่พบ FFmpeg/FFprobe — ตรวจสอบไฟล์ packages.txt ว่ามี ffmpeg")
    st.stop()


@dataclass
class Cue:
    start: float
    end: float
    text: str
    words: list | None = None


def command(args, *, cwd=None, timeout=600):
    try:
        p = subprocess.run(
            [str(a) for a in args], cwd=cwd, capture_output=True,
            text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ประมวลผลนานเกินกำหนด กรุณาลองคลิปสั้นลงหรือใช้ 720p") from exc
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "FFmpeg ล้มเหลว")[-2500:])
    return p.stdout


def probe(path):
    raw = command([
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", path
    ], timeout=40)
    info = json.loads(raw)
    streams = info.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise ValueError("ไฟล์นี้ไม่มีภาพวิดีโอ")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = float(info.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("อ่านความยาววิดีโอไม่ได้")
    return duration, has_audio


def save_upload(upload, path, max_mb):
    if upload.size > max_mb * 1024 * 1024:
        raise ValueError(f"ไฟล์ {upload.name} ใหญ่เกิน {max_mb} MB")
    with open(path, "wb") as f:
        f.write(upload.getbuffer())


def clean_ass(text):
    # ASS special characters can otherwise inject override tags.
    return (text.replace("\\", "＼").replace("{", "(").replace("}", ")")
                .replace("\r", " ").replace("\n", " ").strip())


def ass_time(t):
    cs = max(0, round(float(t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def parse_cues(text, limit):
    result = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            raise ValueError(f"ซับบรรทัดที่ {lineno} ต้องเป็น เริ่ม|จบ|ข้อความ")
        try:
            start, end = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise ValueError(f"เวลาในซับบรรทัดที่ {lineno} ไม่ใช่ตัวเลข") from exc
        if not (math.isfinite(start) and math.isfinite(end) and
                0 <= start < end <= limit + 0.05):
            raise ValueError(f"ซับบรรทัดที่ {lineno} มีช่วงเวลาไม่ถูกต้อง (คลิปยาว {limit:.1f} วินาที)")
        caption = parts[2].strip()
        if not caption:
            continue
        result.append(Cue(start, min(end, limit), caption[:180]))
    if len(result) > 100:
        raise ValueError("จำนวนซับเกิน 100 บรรทัด")
    return sorted(result, key=lambda x: x.start)


def group_whisper_segments(segments, max_chars=42, max_span=3.3):
    """Group timestamped words into readable, short Thai captions."""
    cues = []
    for seg in segments:
        words = []
        for w in (getattr(seg, "words", None) or []):
            if w.start is None or w.end is None or not w.word.strip():
                continue
            words.append({
                "start": float(w.start),
                "end": float(w.end),
                "text": str(w.word),
            })
        if not words:
            text = str(seg.text).strip()
            if text:
                cues.append(Cue(float(seg.start), float(seg.end), text[:180]))
            continue
        bucket = []
        for word in words:
            current = "".join(x["text"] for x in bucket)
            duration = word["end"] - bucket[0]["start"] if bucket else 0
            if bucket and (len(current + word["text"]) > max_chars or duration > max_span):
                cues.append(Cue(bucket[0]["start"], bucket[-1]["end"],
                                "".join(x["text"] for x in bucket).strip(), bucket))
                bucket = []
            bucket.append(word)
        if bucket:
            cues.append(Cue(bucket[0]["start"], bucket[-1]["end"],
                            "".join(x["text"] for x in bucket).strip(), bucket))
    return cues


@st.cache_resource(show_spinner="กำลังโหลดโมเดลถอดเสียงครั้งแรก...")
def load_whisper(model_size):
    from faster_whisper import WhisperModel
    return WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=2,
                        num_workers=1)


def transcribe(src, work, model_size, seconds):
    audio = work / "speech.wav"
    command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src, "-t", str(seconds), "-vn", "-ac", "1",
        "-ar", "16000", "-c:a", "pcm_s16le", audio
    ], timeout=120)
    model = load_whisper(model_size)
    segments, info = model.transcribe(
        str(audio), language="th", beam_size=2, vad_filter=True,
        word_timestamps=True, condition_on_previous_text=False
    )
    return group_whisper_segments(list(segments))


def cue_key(cue):
    return (round(cue.start, 2), round(cue.end, 2), cue.text.strip())


def ass_karaoke(cue):
    """Use actual Whisper word timings; fallback to plain text if edited."""
    if not cue.words:
        return clean_ass(cue.text), "Plain"
    chunks = []
    cursor = cue.start
    for word in cue.words:
        start = max(cue.start, min(float(word["start"]), cue.end))
        end = max(start, min(float(word["end"]), cue.end))
        if start > cursor + 0.015:
            chunks.append(r"{\k" + str(max(1, round((start - cursor) * 100))) + "}")
        chunks.append(r"{\k" + str(max(1, round((end - start) * 100))) + "}" +
                      clean_ass(word["text"]))
        cursor = max(cursor, end)
    return "".join(chunks), "Karaoke"


def create_ass(path, cues, duration, intro, outro, brand, accent):
    # ASS colours are &HAABBGGRR. Karaoke: upcoming words white; spoken words accent.
    accents = {
        "ฟ้า": "&H00FFD65E",      # RGB 5ED6FF
        "เหลือง": "&H004AD8FF",  # RGB FFD84A
        "ชมพู": "&H00D8A1FF",    # RGB FFA1D8
    }
    selected = accents.get(accent, accents["ฟ้า"])
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 720\nPlayResY: 1280\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Plain,Garuda,45,&H00FFFFFF,&H00FFFFFF,&H00122540,&H9007192A,"
        "1,0,0,0,100,100,0,0,1,3,1,2,45,45,185,1\n"
        f"Style: Karaoke,Garuda,45,{selected},&H00FFFFFF,&H00122540,&H9007192A,"
        "1,0,0,0,100,100,0,0,1,3,1,2,45,45,185,1\n"
        "Style: Title,Garuda,48,&H00FFFFFF,&H00FFFFFF,&H00122540,&H9007192A,"
        "1,0,0,0,100,100,0,0,1,3,1,8,45,45,235,1\n"
        "Style: Brand,Garuda,24,&H00FFFFFF,&H00FFFFFF,&H00122540,&H9007192A,"
        "1,0,0,0,100,100,0,0,1,2,25,25,55,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for cue in cues:
        caption, style = ass_karaoke(cue)
        if not caption:
            continue
        events.append(f"Dialogue: 5,{ass_time(cue.start)},{ass_time(cue.end)},"
                      f"{style},,0,0,0,,{caption}")
    if intro.strip():
        events.append(f"Dialogue: 8,{ass_time(0)},{ass_time(min(2.7, duration))},"
                      f"Title,,0,0,0,,{clean_ass(intro[:100])}")
    if outro.strip():
        events.append(f"Dialogue: 8,{ass_time(max(0, duration-2.7))},{ass_time(duration)},"
                      f"Title,,0,0,0,,{clean_ass(outro[:100])}")
    if brand.strip():
        events.append(f"Dialogue: 9,{ass_time(0)},{ass_time(duration)},"
                      f"Brand,,0,0,0,,{clean_ass(brand[:60])}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")


def keywords_from_filename(name):
    stem = Path(name).stem
    # Accept descriptive Thai/English filenames such as กล้วยฉาบ_สินค้า.jpg
    return [p.lower() for p in re.split(r"[_\-\s,]+", stem) if len(p.strip()) >= 2]


def find_image_cues(images, cues, duration, fallback):
    """Only match by supplied keywords/filenames, never invent semantic matches."""
    matches = []
    unmatched = []
    for image in images:
        terms = [k.strip().lower() for k in re.split(r"[,，]+", image["keywords"]) if k.strip()]
        if not terms:
            terms = keywords_from_filename(image["name"])
        match = next((cue for cue in cues if any(k in cue.text.lower() for k in terms)), None)
        if match:
            start = max(0, match.start)
            end = min(duration, max(start + 0.6, min(match.end, start + 3.2)))
            matches.append({**image, "start": start, "end": end, "matched": True})
        else:
            unmatched.append(image)
    if fallback and unmatched:
        n = len(unmatched)
        for i, image in enumerate(unmatched):
            start = min(max(0, duration-0.7), (i+1)*duration/(n+1))
            matches.append({**image, "start": start,
                            "end": min(duration, start + 2.2), "matched": False})
    return sorted(matches, key=lambda x: x["start"]), unmatched


def make_overlay(upload, dest, mode, width=720, height=1280):
    im = Image.open(upload).convert("RGB")
    if mode == "เต็มจอ":
        result = ImageOps.fit(im, (width, height), method=Image.Resampling.LANCZOS)
        result.save(dest, format="PNG")
        return
    # Smaller PNG with rounded corners and drop shadow for slide-in animation.
    maxw, maxh = round(width * 0.66), round(height * 0.43)
    im.thumbnail((maxw - 28, maxh - 28), Image.Resampling.LANCZOS)
    pw, ph = im.size
    canvas = Image.new("RGBA", (pw + 30, ph + 30), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (pw + 30, ph + 30), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((9, 11, pw+21, ph+23), radius=20, fill=(0, 0, 0, 95))
    canvas.alpha_composite(shadow)
    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, pw-1, ph-1), radius=17, fill=255)
    canvas.paste(im, (12, 12), mask)
    canvas.save(dest)


def build_video(src, cues, overlays, duration, intro, outro, brand,
                accent, resolution, work, progress):
    w, h = (720, 1280) if resolution == "720p" else (1080, 1920)
    ass = work / "captions.ass"
    # Subtitles use a fixed 720x1280 logical canvas and scale to video size.
    create_ass(ass, cues, duration, intro, outro, brand, accent)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-threads", "2", "-i", str(src)]
    graph = [f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
             f"crop={w}:{h},setsar=1,fps=24,format=yuv420p[v0]"]
    for i, item in enumerate(overlays):
        dest = work / f"overlay_{i}.png"
        make_overlay(item["upload"], dest, item["mode"], w, h)
        cmd += ["-loop", "1", "-framerate", "24", "-i", str(dest)]
        graph.append(f"[{i+1}:v]format=rgba[im{i}]")
        start, end = item["start"], item["end"]
        if item["mode"] == "เต็มจอ":
            x, y = "0", "0"
        else:
            # Slide from the right over the first 0.22 s.
            x = (f"if(lt(t\\,{start+0.22:.3f})\\,"
                 f"W-(w+28)*((t-{start:.3f})/0.22)\\,W-w-28)")
            y = "H*0.24"
        graph.append(f"[v{i}][im{i}]overlay=x='{x}':y='{y}':"
                     f"enable='between(t\\,{start:.3f}\\,{end:.3f})':"
                     f"eof_action=pass:eval=frame[v{i+1}]")
    last = f"v{len(overlays)}"
    graph.append(f"[{last}]ass=captions.ass,format=yuv420p[vout]")
    output = work / "auto_video_studio_v6.mp4"
    cmd += [
        "-filter_complex", ";".join(graph),
        "-map", "[vout]", "-map", "0:a?",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast" if resolution == "1080p" else "veryfast",
        "-crf", "27" if resolution == "1080p" else "25",
        "-pix_fmt", "yuv420p", "-threads", "2",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(output)
    ]
    progress.progress(65, "กำลังเรนเดอร์คลิป พร้อมเสียงและซับ...")
    command(cmd, cwd=work, timeout=900)
    if not output.exists() or output.stat().st_size < 1000:
        raise RuntimeError("FFmpeg ไม่ได้สร้างไฟล์วิดีโอ")
    progress.progress(100, "สร้างวิดีโอสำเร็จ")
    return output.read_bytes()


def video_fingerprint(upload):
    # Small hash for detecting upload change; full upload is read only when processing.
    return hashlib.sha256(
        upload.name.encode("utf-8") + str(upload.size).encode() +
        upload.getvalue()[:262144]
    ).hexdigest()


st.markdown("### 1. คลิปผู้พูด")
video = st.file_uploader("อัปโหลด MP4 / MOV / M4V", type=["mp4", "mov", "m4v"])
st.caption("แนะนำคลิป 10–30 วินาที ขนาดไม่เกิน 120 MB • V6 ประมวลผลสูงสุด 60 วินาที")
model_size = st.selectbox("ความแม่นยำการถอดเสียง", ["tiny", "base"], index=0,
                          format_func=lambda s: "เร็ว / ใช้ RAM น้อย (tiny)" if s == "tiny"
                          else "ละเอียดขึ้น / ช้ากว่า (base)")
if video is not None:
    signature = video_fingerprint(video)
    if st.session_state.get("v6_video_sig") != signature:
        st.session_state["v6_video_sig"] = signature
        st.session_state["v6_transcript_text"] = ""
        st.session_state["v6_word_map"] = {}
        st.session_state.pop("v6_rendered", None)

st.markdown("### 2. ถอดเสียงและสร้างซับอัตโนมัติ")
if st.button("🎙️ AI ถอดเสียงภาษาไทย", disabled=video is None, use_container_width=True):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            src = work / ("source" + Path(video.name).suffix.lower())
            save_upload(video, src, MAX_UPLOAD_MB)
            total, has_audio = probe(src)
            if not has_audio:
                raise ValueError("วิดีโอไม่มีเสียงพูด กรุณาใส่ซับด้วยตนเอง")
            seconds = min(total, MAX_VIDEO_SECONDS)
            with st.spinner("กำลังถอดเสียงด้วย Whisper (ครั้งแรกอาจต้องดาวน์โหลดโมเดล)..."):
                found = transcribe(src, work, model_size, seconds)
            found = [c for c in found if c.start < seconds]
            for c in found:
                c.end = min(c.end, seconds)
            st.session_state["v6_transcript_text"] = "\n".join(
                f"{c.start:.2f}|{c.end:.2f}|{c.text}" for c in found
            )
            st.session_state["v6_word_map"] = {cue_key(c): c.words for c in found}
            if found:
                st.success(f"ถอดเสียงสำเร็จ {len(found)} ช่วง — ตรวจแก้ข้อความก่อนสร้างคลิปได้")
            else:
                st.warning("ไม่พบเสียงพูดที่ถอดเป็นข้อความได้ กรุณากรอกซับเอง")
    except Exception as exc:
        st.error(f"ถอดเสียงไม่สำเร็จ: {exc}")
        st.caption("หาก Streamlit Cloud หน่วยความจำไม่พอ ให้ใช้ซับที่กรอกเอง หรือเลือกโมเดล tiny")

st.text_area(
    "ซับ (แก้ไขได้): เริ่ม|จบ|ข้อความ",
    key="v6_transcript_text",
    height=180,
    placeholder="0|2.5|สวัสดีค่ะ วันนี้เรามีเรื่องมาเล่า\n2.5|5|นี่คือผลิตภัณฑ์ของชุมชน"
)
st.caption("เมื่อใช้ AI ถอดเสียง ซับ Karaoke จะไฮไลต์ตามคำที่โมเดลจับเวลาได้ "
           "หากแก้ข้อความบรรทัดนั้น ระบบจะใช้ซับธรรมดาเพื่อป้องกันไฮไลต์ผิดคำ")

st.markdown("### 3. ภาพเด้งอัตโนมัติตามคำพูด")
photos = st.file_uploader("อัปโหลดภาพสินค้า / B-roll (สูงสุด 5 ภาพ)",
                          type=["png", "jpg", "jpeg"], accept_multiple_files=True)
image_specs = []
for i, photo in enumerate((photos or [])[:MAX_IMAGES]):
    st.markdown(f"**ภาพ {i+1}: {photo.name}**")
    c1, c2 = st.columns([2, 1])
    with c1:
        keywords = st.text_input(
            "คำพูดที่ให้ภาพปรากฏ (คั่นด้วย ,)",
            key=f"v6_kw_{i}_{photo.name}",
            placeholder="เช่น กล้วยฉาบ,สินค้า,ชุมชน"
        )
    with c2:
        mode = st.selectbox("การแสดงภาพ", ["เด้งข้างผู้พูด", "เต็มจอ"],
                            key=f"v6_mode_{i}_{photo.name}")
    image_specs.append({"upload": photo, "name": photo.name,
                        "keywords": keywords, "mode": mode})
if photos and len(photos) > MAX_IMAGES:
    st.warning("ระบบใช้เฉพาะ 5 ภาพแรก เพื่อลดโอกาสเซิร์ฟเวอร์หน่วยความจำเต็ม")
fallback = st.checkbox("แสดงภาพที่หาคำตรงกันไม่พบ โดยกระจายตามเวลา",
                       value=False)
st.caption("AI ถอดเสียงอัตโนมัติ แต่การจับคู่ภาพรุ่นนี้ใช้คำสำคัญหรือชื่อไฟล์ "
           "เพื่อป้องกันภาพไม่ตรงเนื้อหา ไม่ได้ค้นหรือสร้างภาพจากอินเทอร์เน็ต")

st.markdown("### 4. กราฟิกและส่งออก")
intro = st.text_input("ข้อความเปิด (ไม่บังคับ)", "")
outro = st.text_input("ข้อความปิด (ไม่บังคับ)", "ฝากกดติดตามด้วยนะคะ")
brand = st.text_input("ชื่อเพจ / แบรนด์", "12Plus ของดีวิถีเมืองลุ่มภู")
col1, col2 = st.columns(2)
with col1:
    accent = st.selectbox("สีไฮไลต์ Karaoke", ["ฟ้า", "เหลือง", "ชมพู"])
with col2:
    resolution = st.selectbox("ความละเอียด", ["720p", "1080p"])
if resolution == "1080p":
    st.warning("1080p ใช้ RAM และเวลาสูงกว่า แนะนำ 720p บน Streamlit Cloud ฟรี")

if st.button("✨ สร้างวิดีโอ V6 อัตโนมัติ", type="primary",
             disabled=video is None, use_container_width=True):
    st.session_state.pop("v6_rendered", None)
    progress = st.progress(0, "ตรวจสอบข้อมูล...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            src = work / ("source" + Path(video.name).suffix.lower())
            save_upload(video, src, MAX_UPLOAD_MB)
            total, has_audio = probe(src)
            seconds = min(total, MAX_VIDEO_SECONDS)
            if total > MAX_VIDEO_SECONDS:
                st.info("คลิปยาวเกิน 60 วินาที ระบบจะใช้ 60 วินาทีแรก")
            cues = parse_cues(st.session_state.get("v6_transcript_text", ""), seconds)
            word_map = st.session_state.get("v6_word_map", {})
            for cue in cues:
                cue.words = word_map.get(cue_key(cue))
            matches, unmatched = find_image_cues(image_specs, cues, seconds, fallback)
            if unmatched and not fallback:
                st.warning(f"มีภาพ {len(unmatched)} รูปที่ไม่พบคำสำคัญตรงกับซับ จึงไม่แสดงภาพเหล่านั้น")
            if not has_audio:
                st.warning("คลิปนี้ไม่มีเสียงต้นฉบับ ผลลัพธ์จะไม่มีเสียง")
            progress.progress(30, "เตรียมซับและภาพประกอบ...")
            output = build_video(src, cues, matches, seconds, intro, outro, brand,
                                 accent, resolution, work, progress)
            st.session_state["v6_rendered"] = output
            st.success("สร้างวิดีโอ V6 สำเร็จ!")
    except Exception as exc:
        st.error(f"สร้างวิดีโอไม่สำเร็จ: {exc}")
        st.caption("ลอง 720p, คลิปสั้นลง หรือภาพประกอบน้อยลง หากยังล้มเหลวให้เปิด Streamlit Logs")

if st.session_state.get("v6_rendered"):
    st.video(st.session_state["v6_rendered"])
    st.download_button(
        "⬇️ ดาวน์โหลด MP4",
        data=st.session_state["v6_rendered"],
        file_name="auto_video_studio_v6.mp4",
        mime="video/mp4",
        use_container_width=True
    )
st.caption("V6 ใช้ Whisper ในเครื่อง (ไม่มี API Key) • อาจต้องดาวน์โหลดโมเดลเมื่อเริ่มใช้งานครั้งแรก "
           "• การจับเวลาคำภาษาไทยเป็นค่าประมาณ โปรดตรวจทานก่อนเผยแพร่")
