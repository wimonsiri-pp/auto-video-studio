import subprocess, tempfile, shutil, re
from pathlib import Path
import streamlit as st
from PIL import Image, ImageDraw, ImageFont

st.set_page_config(page_title="Auto Video Studio V5",page_icon="🎬",layout="centered")
st.title("🎬 Auto Video Studio PRO V5")
st.caption("Presenter + B-roll + timed captions + picture pop-ups · Thai social video")
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    st.error("ต้องติดตั้ง ffmpeg และ ffprobe ใน packages.txt")
    st.stop()

def run(cmd):
    p=subprocess.run(cmd,capture_output=True,text=True,timeout=1200)
    if p.returncode: raise RuntimeError(p.stderr[-2500:])
    return p.stdout

def duration(path):
    return float(run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)]).strip())

def fontpath():
    for f in ["/usr/share/fonts/truetype/tlwg/Garuda.ttf","/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf","/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if Path(f).exists(): return f
    raise RuntimeError("ไม่พบฟอนต์ไทย โปรดติดตั้ง fonts-thai-tlwg")

def safe_text(s):
    return s.strip()[:220]

def make_caption(text,path,w=720,h=1280):
    im=Image.new("RGBA",(w,h),(0,0,0,0)); d=ImageDraw.Draw(im)
    f=ImageFont.truetype(fontpath(),38)
    words=text.split()
    lines=[]; cur=""
    for word in words:
        trial=(cur+" "+word).strip()
        if d.textbbox((0,0),trial,font=f)[2]>w*.84 and cur:
            lines.append(cur);cur=word
        else: cur=trial
    if cur:lines.append(cur)
    lines=lines[:3]
    y=int(h*.74)
    d.rounded_rectangle((40,y-24,w-40,y+len(lines)*58+20),radius=24,fill=(5,24,46,208))
    for i,line in enumerate(lines):
        d.text((w//2,y+i*58),line,font=f,anchor="mt",fill="white",stroke_width=1,stroke_fill="#13263b")
    im.save(path)

def parse_cues(raw, total):
    # one cue per line: start|end|caption, seconds are relative to video
    cues=[]
    for n,line in enumerate(raw.splitlines(),1):
        if not line.strip():continue
        parts=line.split("|",2)
        if len(parts)!=3:raise ValueError(f"บรรทัด {n}: ต้องใช้ เริ่ม|จบ|ข้อความ")
        a,b=float(parts[0]),float(parts[1])
        if not (0<=a<b<=total):raise ValueError(f"บรรทัด {n}: เวลาไม่ถูกต้องหรือเกินความยาวคลิป")
        cues.append((a,b,safe_text(parts[2])))
    return cues

video=st.file_uploader("1. อัปโหลดคลิปผู้พูด (รักษาเสียงต้นฉบับ)",type=["mp4","mov","m4v"])
st.markdown("**2. ซับตามเวลา**")
st.caption("รูปแบบแต่ละบรรทัด: วินาทีเริ่ม|วินาทีจบ|ข้อความ เช่น 0|3|สวัสดีค่ะ วันนี้เรามีเรื่องมาเล่า")
captions=st.text_area("ช่วงเวลาและข้อความ",height=130,placeholder="0|3|วันนี้เราจะพามารู้จักผลิตภัณฑ์ชุมชน\n3|6|เริ่มต้นจากวัตถุดิบในท้องถิ่น")
st.markdown("**3. ภาพประกอบเด้ง / B-roll**")
photos=st.file_uploader("อัปโหลดภาพประกอบ (เรียงตามลำดับ)",type=["png","jpg","jpeg"],accept_multiple_files=True)
st.caption("กำหนดเวลาแต่ละภาพหนึ่งบรรทัดตามลำดับที่อัปโหลด: เริ่ม|จบ|รูปแบบ โดยรูปแบบใช้ popup หรือ full")
photo_times=st.text_area("กำหนดจังหวะภาพ",height=100,placeholder="3|5|popup\n8|11|full")
resolution=st.selectbox("ความละเอียด",["720p","1080p"])
if st.button("✨ สร้างคลิป V5",type="primary",disabled=video is None):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp)
            src=work/("source"+Path(video.name).suffix.lower());src.write_bytes(video.getvalue())
            total=duration(src)
            if total<=0:raise ValueError("วิดีโอไม่ถูกต้อง")
            cues=parse_cues(captions,total)
            pop=parse_cues(photo_times,total) if photo_times.strip() else []
            if len(pop)!=len(photos or []):raise ValueError("จำนวนบรรทัดเวลาภาพต้องตรงกับจำนวนภาพ")
            if any(mode not in ("popup","full") for _,_,mode in pop):raise ValueError("รูปแบบภาพต้องเป็น popup หรือ full")
            w,h=(720,1280) if resolution=="720p" else (1080,1920)
            inputs=["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(src)]
            overlays=[]
            for i,(a,b,text) in enumerate(cues):
                png=work/f"caption_{i}.png";make_caption(text,png,w,h)
                inputs+=["-loop","1","-i",str(png)]
                overlays.append((len(overlays)+1,a,b,"caption"))
            for i,(photo,(a,b,mode)) in enumerate(zip(photos or [],pop)):
                path=work/f"photo_{i}.png"
                Image.open(photo).convert("RGBA").save(path)
                inputs+=["-loop","1","-i",str(path)]
                overlays.append((len(overlays)+1,a,b,mode))
            graph=[f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30,format=yuv420p[v0]"]
            # input indexes: source 0; captions 1..N; photos N+1..N+M
            for i,(idx,a,b,mode) in enumerate(overlays):
                label=f"v{i}";nxt=f"v{i+1}"
                if mode=="caption":
                    graph.append(f"[{idx}:v]format=rgba[ov{i}]")
                    x,y="0","0"
                elif mode=="full":
                    graph.append(f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},format=rgba[ov{i}]")
                    x,y="0","0"
                else:
                    graph.append(f"[{idx}:v]scale={int(w*.46)}:-1:force_original_aspect_ratio=decrease,format=rgba[ov{i}]")
                    x,y="W-w-28","int(H*0.22)"
                graph.append(f"[{label}][ov{i}]overlay=x={x}:y={y}:enable='between(t,{a},{b})':eof_action=pass[{nxt}]")
            last=f"v{len(overlays)}"
            graph.append(f"[{last}]format=yuv420p[vout]")
            output=work/"auto_video_v5.mp4"
            cmd=inputs+["-filter_complex",";".join(graph),"-map","[vout]","-map","0:a?","-c:v","libx264","-preset","veryfast","-crf","24","-c:a","aac","-b:a","160k","-t",str(total),"-movflags","+faststart",str(output)]
            with st.spinner("กำลังตัดต่อและเรนเดอร์..."):
                run(cmd)
            st.session_state["v5_output"]=output.read_bytes()
            st.success("สร้างคลิปสำเร็จ")
    except Exception as e:
        st.error(f"เกิดข้อผิดพลาด: {e}")
if st.session_state.get("v5_output"):
    st.video(st.session_state["v5_output"])
    st.download_button("⬇️ ดาวน์โหลด MP4",st.session_state["v5_output"],"auto_video_v5.mp4","video/mp4")
st.info("รุ่นนี้รักษาเสียงต้นฉบับและเพิ่มภาพ/ซับตามเวลาที่กำหนด ยังไม่ใช่การถอดเสียงหรือเลือกภาพด้วย AI อัตโนมัติ")
