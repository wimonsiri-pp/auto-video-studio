import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw, ImageFont

st.set_page_config(page_title="Auto Video Studio", page_icon="🎬")
st.title("🎬 Auto Video Studio")
st.caption("รวมคลิปอัตโนมัติ • แนวตั้ง 9:16 • ดาวน์โหลด MP4")

if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    st.error("ไม่พบ FFmpeg กรุณาตรวจสอบ packages.txt")
    st.stop()

videos = st.file_uploader("1. เลือกวิดีโอตามลำดับที่ต้องการ", type=["mp4", "mov", "m4v"], accept_multiple_files=True)
music = st.file_uploader("2. เพลงประกอบ (ไม่บังคับ)", type=["mp3", "m4a", "wav"])
duration = st.selectbox("3. ความยาวสูงสุด", [30, 60], index=1)
resolution = st.selectbox("4. ความละเอียด", ["720p", "1080p"])
opening = st.text_input("5. ข้อความเปิดคลิป (ไม่บังคับ)", "")
ending = st.text_input("6. ข้อความปิดคลิป (ไม่บังคับ)", "")


def cmd(args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if p.returncode:
        raise RuntimeError(p.stderr[-2500:])
    return p.stdout


def seconds(path):
    out = cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(out.strip())


def title_png(text, target, width, height):
    # ใช้ Pillow วาดข้อความ รองรับภาษาไทยเมื่อมีฟอนต์ไทยติดตั้ง
    candidates = [
        "/usr/share/fonts/truetype/tlwg/Garuda.ttf",
        "/usr/share/fonts/truetype/tlwg/Loma.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    font_path = next((p for p in candidates if Path(p).exists()), None)
    if not font_path:
        raise RuntimeError("ไม่พบฟอนต์สำหรับข้อความ กรุณาติดตั้ง fonts-thai-tlwg")
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(font_path, max(24, width // 22))
    # จำกัดความกว้างข้อความด้วยการแบ่งบรรทัด
    words = text.split()
    lines, line = [], ""
    for word in words:
        trial = (line + " " + word).strip()
        if draw.textbbox((0, 0), trial, font=font)[2] > width - 90 and line:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    lines = lines[:3]
    line_h = int(font.size * 1.55)
    top = int(height * 0.75) - len(lines) * line_h // 2
    draw.rounded_rectangle((25, top - 18, width - 25, top + len(lines) * line_h + 18), radius=20, fill=(0, 0, 0, 145))
    for i, line in enumerate(lines):
        draw.text((width // 2, top + i * line_h), line, font=font, anchor="mt", fill="white", stroke_width=1, stroke_fill="black")
    canvas.save(target)


if st.button("✨ สร้างวิดีโออัตโนมัติ", type="primary", disabled=not videos):
    if len(videos) > 12:
        st.error("กรุณาเลือกไม่เกิน 12 คลิปต่อครั้ง")
        st.stop()
    try:
        with st.spinner("กำลังตัดต่อวิดีโอ กรุณาเปิดหน้านี้ไว้..."):
            with tempfile.TemporaryDirectory() as td:
                work = Path(td)
                inputs = []
                for i, upload in enumerate(videos):
                    ext = Path(upload.name).suffix.lower()
                    path = work / f"input_{i}{ext}"
                    with path.open("wb") as f:
                        shutil.copyfileobj(upload, f)
                    inputs.append(path)
                durations = [seconds(p) for p in inputs]
                total_available = sum(durations)
                if total_available < 1:
                    raise ValueError("วิดีโอไม่มีความยาวที่ใช้งานได้")
                target = min(float(duration), total_available)
                # แบ่งเวลาฉากใกล้เคียงกัน และกระจายเวลาที่เหลือให้คลิปยาวกว่า
                allocated = [0.0] * len(inputs)
                remaining = target
                active = set(range(len(inputs)))
                while active and remaining > 0.01:
                    share = remaining / len(active)
                    completed = []
                    for i in active:
                        give = min(share, durations[i] - allocated[i])
                        allocated[i] += give
                        remaining -= give
                        if durations[i] - allocated[i] < 0.01:
                            completed.append(i)
                    active.difference_update(completed)
                w, h = (720, 1280) if resolution == "720p" else (1080, 1920)
                scenes = []
                for i, (src, length) in enumerate(zip(inputs, allocated)):
                    if length < 0.1:
                        continue
                    dest = work / f"scene_{i:02}.mp4"
                    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30,format=yuv420p"
                    cmd(["ffmpeg", "-y", "-i", str(src), "-t", f"{length:.3f}", "-vf", vf, "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25", "-movflags", "+faststart", str(dest)])
                    scenes.append(dest)
                if not scenes:
                    raise ValueError("ไม่พบฉากที่ตัดต่อได้")
                concat = work / "concat.txt"
                concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in scenes), encoding="utf-8")
                joined = work / "joined.mp4"
                cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(joined)])
                filters = []
                video_input = "0:v"
                extra = []
                # ข้อความแบบ PNG overlay เพื่อรองรับอักษรไทย
                for text, start, stop, idx in [(opening, 0, 4, 0), (ending, max(0, target - 5), target, 1)]:
                    if text.strip():
                        png = work / f"title_{idx}.png"
                        title_png(text.strip(), png, w, h)
                        extra += ["-loop", "1", "-i", str(png)]
                        stream_idx = 1 + (1 if music else 0) + sum(1 for x in (opening, ending)[:idx] if x.strip())
                        next_label = f"v{idx}"
                        filters.append(f"[{video_input}][{stream_idx}:v]overlay=0:0:enable='between(t,{start:.3f},{stop:.3f})'[{next_label}]")
                        video_input = next_label
                out = work / "result.mp4"
                command = ["ffmpeg", "-y", "-i", str(joined)]
                if music:
                    mp = work / ("music" + Path(music.name).suffix.lower())
                    with mp.open("wb") as f:
                        shutil.copyfileobj(music, f)
                    command += ["-stream_loop", "-1", "-i", str(mp)]
                command += extra
                audio_input = None
                if music:
                    filters.append(f"[1:a]volume=0.22,atrim=duration={target:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={max(0,target-2):.3f}:d=2[aout]")
                    audio_input = "[aout]"
                if filters:
                    command += ["-filter_complex", ";".join(filters)]
                command += ["-map", f"[{video_input}]" if video_input != "0:v" else "0:v:0"]
                if audio_input:
                    command += ["-map", audio_input, "-c:a", "aac", "-b:a", "128k"]
                else:
                    command += ["-an"]
                command += ["-t", f"{target:.3f}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
                cmd(command)
                st.session_state["video_bytes"] = out.read_bytes()
                st.session_state["video_duration"] = target
        st.success("ตัดต่อสำเร็จ!")
    except Exception as e:
        st.error(f"ประมวลผลไม่สำเร็จ: {e}")

if st.session_state.get("video_bytes"):
    st.video(st.session_state["video_bytes"])
    st.caption(f"ความยาวประมาณ {st.session_state['video_duration']:.1f} วินาที")
    st.download_button("⬇️ ดาวน์โหลด MP4", data=st.session_state["video_bytes"], file_name="auto_video_studio.mp4", mime="video/mp4")

st.caption("หมายเหตุ: รุ่นนี้ตัดต่อแบบอัตโนมัติตามลำดับไฟล์ ยังไม่ได้วิเคราะห์เนื้อหาด้วย AI และไม่ได้ใส่ transition ระหว่างฉาก")
