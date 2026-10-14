"""Auto Video Studio PRO V5.1 – memory-conscious Streamlit video editor.

Keeps presenter audio, burns Thai timed subtitles, and adds timed B-roll one
image at a time so free Streamlit hosts do not decode many looping images at once.
"""
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import streamlit as st
from PIL import Image, UnidentifiedImageError

st.set_page_config(page_title="Auto Video Studio PRO V5.1", page_icon="🎬", layout="centered")
st.title("🎬 Auto Video Studio PRO V5.1")
st.caption("ผู้พูด + เสียงต้นฉบับ + ซับภาษาไทย + รูปเด้ง / ภาพเต็มจอ")

# Conservative settings for a small Streamlit Community Cloud instance.
MAX_VIDEO_MB = 120
MAX_PHOTO_MB = 12
MAX_PHOTOS = 4


def ffmpeg_run(args, timeout=600):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "FFmpeg ทำงานไม่สำเร็จ")[-1600:])
    return proc.stdout


def get_duration(path):
    value = ffmpeg_run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ]).strip()
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("ไม่สามารถอ่านความยาววิดีโอได้")
    return seconds


def parse_timing(text, limit, label):
    """Each line: start|end|text (or popup/full for image cues)."""
    output = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            raise ValueError(f"{label} บรรทัด {lineno}: ใช้รูปแบบ เริ่ม|จบ|ข้อความ")
        try:
            start, end = float(parts[0].strip()), float(parts[1].strip())
        except ValueError as exc:
            raise ValueError(f"{label} บรรทัด {lineno}: เวลาเป็นตัวเลขวินาทีเท่านั้น") from exc
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= limit + 0.01):
            raise ValueError(f"{label} บรรทัด {lineno}: ช่วงเวลาไม่ถูกต้องหรือเกิน {limit:.1f} วินาที")
        value = parts[2].strip()
        if not value:
            raise ValueError(f"{label} บรรทัด {lineno}: ข้อความว่าง")
        output.append((start, min(end, limit), value))
    return output


def ass_time(seconds):
    cs = int(round(seconds * 100))
    hours, rem = divmod(cs, 360000)
    minutes, rem = divmod(rem, 6000)
    sec, hundredths = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{sec:02d}.{hundredths:02d}"


def ass_escape(text):
    return (text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
            .replace("\n", " ").replace("\r", " "))[:180]


def write_subtitles(path, cues):
    # ASS supports Unicode Thai, line wrapping and a semi-transparent boxed style.
    header = """[Script Info]
Title: Auto Video Studio PRO V5.1
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Thai,Garuda,46,&H00FFFFFF,&H00FFFFFF,&H001C3854,&H920A1A2D,1,0,0,0,100,100,0,0,3,2,0,2,65,65,195,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Thai,,0,0,0,,{ass_escape(t)}"
              for a, b, t in cues]
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def save_photo(upload, target):
    if upload.size > MAX_PHOTO_MB * 1024 * 1024:
        raise ValueError(f"ภาพ {upload.name} ใหญ่เกิน {MAX_PHOTO_MB} MB")
    try:
        upload.seek(0)
        with Image.open(upload) as image:
            if image.width * image.height > 25_000_000:
                raise ValueError("ภาพความละเอียดสูงเกินไป กรุณาย่อภาพก่อน")
            image = image.convert("RGB")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            image.save(target, format="JPEG", quality=88)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"อ่านภาพ {upload.name} ไม่สำเร็จ") from exc


def base_render(src, target, ass, width, height, seconds):
    # Render once from the uploaded video; audio is kept rather than discarded.
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},setsar=1,fps=25,"
          f"ass={ass.as_posix()},format=yuv420p")
    ffmpeg_run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-t", str(seconds), "-vf", vf, "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "27", "-threads", "2",
        "-c:a", "aac", "-b:a", "112k", "-ar", "44100",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)
    ])


def add_photo(source, photo, target, cue, width, height, seconds):
    a, b, mode = cue
    if mode == "full":
        # Keep aspect ratio and fill the entire screen.
        vf = (f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},setsar=1[photo];"
              f"[0:v][photo]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})':"
              f"eof_action=pass:repeatlast=0[v]")
    else:
        popup_width = int(width * 0.45)
        vf = (f"[1:v]scale={popup_width}:-2,setsar=1[photo];"
              f"[0:v][photo]overlay=W-w-24:H*0.24:"
              f"enable='between(t,{a:.3f},{b:.3f})':"
              f"eof_action=pass:repeatlast=0[v]")
    ffmpeg_run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-loop", "1", "-framerate", "1", "-i", str(photo),
        "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "27",
        "-threads", "2", "-c:a", "copy", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(target)
    ])


if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    st.error("ไม่พบ FFmpeg / FFprobe กรุณาตรวจสอบ packages.txt")
    st.stop()

with st.form("studio_form"):
    video = st.file_uploader("1. อัปโหลดวิดีโอผู้พูด", type=["mp4", "mov", "m4v"])
    captions = st.text_area("2. ช่วงเวลาและข้อความซับ (เริ่ม|จบ|ข้อความ)",
                            placeholder="0|3|รู้ไหมคะ กล้วยธรรมดาสร้างรายได้ได้\n3|6|วันนี้พามารู้จักกล้วยฉาบหนองบัวลำภู",
                            height=130)
    photos = st.file_uploader("3. อัปโหลดภาพประกอบตามลำดับ (สูงสุด 4 ภาพ)",
                              type=["jpg", "jpeg", "png"], accept_multiple_files=True)
    photo_times = st.text_area("4. เวลาภาพประกอบ (เริ่ม|จบ|popup หรือ full)",
                               placeholder="3|5|popup\n8|11|full", height=100)
    resolution = st.selectbox("ความละเอียด", ["720p (แนะนำ)", "1080p (ใช้ทรัพยากรมาก)"])
    submitted = st.form_submit_button("✨ สร้างคลิป V5.1", type="primary", use_container_width=True)

st.caption("แนะนำทดสอบคลิป 10–20 วินาทีที่ 720p ก่อน · ระบบยังไม่ถอดเสียงหรือเลือกภาพด้วย AI")

if submitted:
    st.session_state.pop("v5_output", None)
    try:
        if video is None:
            raise ValueError("กรุณาอัปโหลดคลิปผู้พูดก่อน")
        if video.size > MAX_VIDEO_MB * 1024 * 1024:
            raise ValueError(f"ไฟล์วิดีโอใหญ่เกิน {MAX_VIDEO_MB} MB")
        if len(photos or []) > MAX_PHOTOS:
            raise ValueError(f"อัปโหลดภาพได้ไม่เกิน {MAX_PHOTOS} ภาพ")
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            source = work / ("source" + Path(video.name).suffix.lower())
            video.seek(0)
            with source.open("wb") as dst:
                shutil.copyfileobj(video, dst, length=1024 * 1024)
            full_duration = get_duration(source)
            is_hd = resolution.startswith("1080")
            max_seconds = 30 if is_hd else 60
            seconds = min(full_duration, max_seconds)
            if full_duration > max_seconds:
                st.warning(f"เพื่อป้องกันเซิร์ฟเวอร์ล่ม ระบบจะใช้ช่วงต้น {max_seconds} วินาทีของคลิป")
            captions_cues = parse_timing(captions, seconds, "ซับ")
            image_cues = parse_timing(photo_times, seconds, "ภาพ")
            if len(image_cues) != len(photos or []):
                raise ValueError("จำนวนบรรทัดเวลาภาพต้องตรงกับจำนวนภาพที่อัปโหลด")
            if any(mode.lower() not in ("popup", "full") for _, _, mode in image_cues):
                raise ValueError("โหมดภาพต้องเป็น popup หรือ full เท่านั้น")
            w, h = (1080, 1920) if is_hd else (720, 1280)
            subtitle_file = work / "captions.ass"
            write_subtitles(subtitle_file, captions_cues)
            progress = st.progress(5, text="เตรียมคลิปและซับภาษาไทย...")
            current = work / "pass_0.mp4"
            base_render(source, current, subtitle_file, w, h, seconds)
            progress.progress(40, text="เพิ่มภาพประกอบ...")
            for i, (uploaded, cue) in enumerate(zip(photos or [], image_cues), 1):
                image_path = work / f"image_{i}.jpg"
                save_photo(uploaded, image_path)
                output_path = work / f"pass_{i}.mp4"
                add_photo(current, image_path, output_path,
                          (cue[0], cue[1], cue[2].lower()), w, h, seconds)
                current.unlink(missing_ok=True)  # Keep disk usage down.
                current = output_path
                progress.progress(40 + int(55 * i / len(image_cues)), text=f"เพิ่มภาพ {i}/{len(image_cues)}")
            st.session_state["v5_output"] = current.read_bytes()
            progress.progress(100, text="สำเร็จ")
            st.success("สร้างคลิปเรียบร้อยแล้ว")
    except subprocess.TimeoutExpired:
        st.error("เซิร์ฟเวอร์ประมวลผลนานเกินไป ลองใช้คลิป 10–20 วินาทีและเลือก 720p")
    except Exception as exc:
        st.error("สร้างคลิปไม่สำเร็จ โปรดตรวจสอบรายละเอียดด้านล่าง")
        st.exception(exc)

if st.session_state.get("v5_output"):
    st.video(st.session_state["v5_output"])
    st.download_button("⬇️ ดาวน์โหลด MP4", data=st.session_state["v5_output"],
                       file_name="auto_video_studio_v5_1.mp4", mime="video/mp4",
                       use_container_width=True)

st.info("V5.1 เป็นรุ่นปรับปรุงเสถียรภาพและลดการใช้หน่วยความจำ ยังไม่ใช่ AI ตัดต่ออัตโนมัติหรือซับ Karaoke ระดับคำ")
