
import streamlit as st

st.set_page_config(
    page_title="Auto Video Studio",
    page_icon="🎬"
)

st.title("🎬 Auto Video Studio")
st.success("ระบบเว็บแอปพร้อมใช้งาน!")

videos = st.file_uploader(
    "อัปโหลดวิดีโอ",
    type=["mp4", "mov"],
    accept_multiple_files=True
)

if videos:
    st.write(f"อัปโหลดแล้ว {len(videos)} ไฟล์")
    for video in videos:
        st.write(video.name)

st.info("ขั้นต่อไป: เพิ่มระบบตัดต่ออัตโนมัติ")
