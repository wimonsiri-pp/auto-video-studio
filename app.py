import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw, ImageFont

st.set_page_config(page_title="Auto Video Studio PRO", page_icon="🎬", layout="centered")
st.title("🎬 Auto Video Studio PRO")
st.caption("Cinematic cuts · Soft transitions · Thai titles · Music · MP4")

if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    st.error("ไม่พบ FFmpeg/FFprobe: ตรวจสอบ packages.txt")
    st.stop()

uploads = st.file_uploader("1 · อัปโหลดคลิปตามลำดับเรื่อง", type=["mp4", "mov", "m4v"], accept_multiple_files=True)
music = st.file_uploader("2 · เพลงประกอบ (ไม่บังคับ)", type=["mp3", "m4a", "wav"])
col1, col2 = st.columns(2)
with col1:
    duration = st.selectbox("ความยาวเป้าหมาย", [30, 60], index=1)
    resolution = st.selectbox("ความละเอียด", ["720p", "1080p"])
    transition = st.selectbox("เปลี่ยนฉาก", ["Crossfade", "Cut"])
with col2:
    style = st.selectbox("โทนสี", ["Natural", "Warm cinematic", "Vivid", "Soft film"])
    music_volume = st.slider("ระดับเสียงเพลง", 0, 100, 28, 1)
    opening_seconds = st.slider("เวลาแสดงข้อความ", 2, 6, 4)
opening = st.text_input("ข้อความเปิด", "จากสวนกล้วย สู่ผลิตภัณฑ์ชุมชน")
ending = st.text_input("ข้อความปิด", "อุดหนุนผลิตภัณฑ์ชุมชน")
st.info("แนะนำเริ่มที่ 720p และคลิปสั้น ๆ ก่อน เพราะเซิร์ฟเวอร์ฟรีมีข้อจำกัดด้านหน่วยความจำ")


def run(args, timeout=900):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        raise RuntimeError(proc.stderr[-1800:] or "FFmpeg failed")
    return proc.stdout


def length(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]).strip())


def allocate(lengths, goal):
    # Water-filling: redistribute unused time from short clips to longer clips.
    result = [0.0] * len(lengths)
    remain = goal
    active = list(range(len(lengths)))
    while active and remain > 0.01:
        share = remain / len(active)
        nxt = []
        for i in active:
            give = min(share, max(0.0, lengths[i] - result[i]))
            result[i] += give
            remain -= give
            if lengths[i] - result[i] > 0.01:
                nxt.append(i)
        active = nxt
    return result


def thai_font():
    candidates = [
        "/usr/share/fonts/truetype/tlwg/Garuda.ttf",
        "/usr/share/fonts/truetype/tlwg/Loma.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    return next((p for p in candidates if Path(p).exists()), None)


def make_title(message, path, w, h):
    font_path = thai_font()
    if not font_path:
        raise RuntimeError("ไม่พบฟอนต์ กรุณาติดตั้ง fonts-thai-tlwg")
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, int(w * 0.055))
    lines = []
    line = ""
    for word in message.split():
        trial = (line + " " + word).strip()
        if draw.textbbox((0, 0), trial, font=font)[2] > w * .83 and line:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    lines = lines[:3]
    step = int(font.size * 1.55)
    top = int(h * .72) - step * len(lines) // 2
    draw.rounded_rectangle((int(w*.06), top-25, int(w*.94), top+step*len(lines)+25), radius=24, fill=(10, 17, 24, 168))
    for j, line in enumerate(lines):
        draw.text((w//2, top+j*step), line, font=font, anchor="mt", fill="white", stroke_width=1, stroke_fill="#111111")
    img.save(path)


color_filters = {
    "Natural": "eq=contrast=1.03:saturation=1.05:brightness=0.005",
    "Warm cinematic": "eq=contrast=1.09:saturation=1.12:brightness=0.008,colorbalance=rs=0.04:gs=0.01:bs=-0.035",
    "Vivid": "eq=contrast=1.07:saturation=1.23:brightness=0.01",
    "Soft film": "eq=contrast=0.96:saturation=0.93:brightness=0.018",
}

if st.button("✨ สร้างวิดีโอแบบมืออาชีพ", type="primary", disabled=not uploads, use_container_width=True):
    st.session_state.pop("rendered", None)
    if len(uploads) > 10:
        st.error("กรุณาเลือกไม่เกิน 10 คลิป")
        st.stop()
    progress = st.progress(0, text="เตรียมไฟล์...")
    try:
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            sources = []
            for i, upload in enumerate(uploads):
                path = work / f"source_{i}{Path(upload.name).suffix.lower()}"
                path.write_bytes(upload.getvalue())
                sources.append(path)
            lengths = [length(p) for p in sources]
            # Drop unusably short files; transition needs room on both sides.
            usable = [(p, n) for p, n in zip(sources, lengths) if n >= 0.5]
            if not usable:
                raise ValueError("ไม่พบคลิปที่ยาวอย่างน้อย 0.5 วินาที")
            sources, lengths = map(list, zip(*usable))
            target = min(float(duration), sum(lengths))
            cross = 0.4 if transition == "Crossfade" and len(sources) > 1 else 0.0
            # Each overlap reduces total output length. Budget it in segment lengths.
            overlap_budget = cross * (len(sources) - 1)
            if target + overlap_budget > sum(lengths):
                target = max(0.5, sum(lengths) - overlap_budget)
            portions = allocate(lengths, target + overlap_budget)
            if cross and any(p <= cross + 0.1 for p in portions):
                cross = 0.0
                portions = allocate(lengths, min(float(duration), sum(lengths)))
                target = sum(portions)
            else:
                target = sum(portions) - cross * (len(portions)-1)
            w, h = (720, 1280) if resolution == "720p" else (1080, 1920)
            fps = 30
            segments = []
            for i, (src, seconds) in enumerate(zip(sources, portions)):
                progress.progress(int(10 + 55*(i+1)/len(sources)), text=f"ปรับภาพฉาก {i+1}/{len(sources)}")
                out = work / f"segment_{i:02}.mp4"
                vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                      f"crop={w}:{h},setsar=1,fps={fps},{color_filters[style]},format=yuv420p")
                run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                     "-t", f"{seconds:.3f}", "-vf", vf, "-an", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", str(out)])
                segments.append(out)
            joined = work / "joined.mp4"
            progress.progress(72, text="เชื่อมฉาก...")
            if len(segments) == 1:
                shutil.copyfile(segments[0], joined)
            elif cross:
                # xfade chain with cumulative offsets; all inputs share resolution, fps, timebase.
                args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
                for segment in segments:
                    args += ["-i", str(segment)]
                filters = []
                offset = portions[0] - cross
                for i in range(1, len(segments)):
                    left = "[0:v]" if i == 1 else f"[x{i-1}]"
                    filters.append(f"{left}[{i}:v]xfade=transition=fade:duration={cross:.3f}:offset={offset:.3f}[x{i}]")
                    if i < len(segments)-1:
                        offset += portions[i] - cross
                args += ["-filter_complex", ";".join(filters), "-map", f"[x{len(segments)-1}]",
                         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", str(joined)]
                run(args)
            else:
                manifest = work / "concat.txt"
                manifest.write_text("".join(f"file '{p.as_posix()}'\n" for p in segments), encoding="utf-8")
                run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(manifest), "-c", "copy", str(joined)])
            # Titles are PNG overlays, avoiding FFmpeg drawtext Thai font problems.
            progress.progress(84, text="เพิ่มข้อความและเพลง...")
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(joined)]
            audio_idx = None
            if music:
                path = work / f"music{Path(music.name).suffix.lower()}"
                path.write_bytes(music.getvalue())
                audio_idx = len(cmd)  # only used as sentinel
                cmd += ["-stream_loop", "-1", "-i", str(path)]
            title_items = []
            for message, begin, end in [(opening.strip(), 0, min(opening_seconds, target)),
                                         (ending.strip(), max(0, target-opening_seconds), target)]:
                if message:
                    path = work / f"title_{len(title_items)}.png"
                    make_title(message, path, w, h)
                    input_id = 1 + (1 if music else 0) + len(title_items)
                    cmd += ["-loop", "1", "-i", str(path)]
                    title_items.append((input_id, begin, end))
            graph = []
            v_label = "[0:v]"
            for i, (idx, begin, end) in enumerate(title_items):
                next_label = f"[v{i}]"
                graph.append(f"{v_label}[{idx}:v]overlay=0:0:enable='between(t,{begin:.3f},{end:.3f})'{next_label}")
                v_label = next_label
            fade_out_start = max(0, target-0.65)
            graph.append(f"{v_label}fade=t=in:st=0:d=0.35,fade=t=out:st={fade_out_start:.3f}:d=0.65[vout]")
            if music:
                graph.append(f"[1:a]volume={music_volume/100:.2f},atrim=duration={target:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.4,afade=t=out:st={max(0,target-1.5):.3f}:d=1.5[aout]")
            output = work / "studio_pro.mp4"
            cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
            if music:
                cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
            else:
                cmd += ["-an"]
            cmd += ["-t", f"{target:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
            run(cmd)
            st.session_state["rendered"] = output.read_bytes()
            st.session_state["length"] = target
            progress.progress(100, text="สำเร็จ")
        st.success("ตัดต่อเสร็จแล้ว!")
    except Exception as exc:
        st.error(f"ตัดต่อไม่สำเร็จ: {exc}")

if st.session_state.get("rendered"):
    st.video(st.session_state["rendered"])
    st.caption(f"ความยาวประมาณ {st.session_state['length']:.1f} วินาที")
    st.download_button("⬇️ ดาวน์โหลด MP4", st.session_state["rendered"], file_name="auto_video_studio_pro.mp4", mime="video/mp4", use_container_width=True)

st.caption("ระบบนี้ตัดต่ออัตโนมัติตามลำดับคลิป ไม่ได้ใช้ AI วิเคราะห์เนื้อหาภาพ • ใช้เพลงที่คุณมีสิทธิ์เผยแพร่")
