# app.py
import io, re
from datetime import datetime
import numpy as np, pandas as pd
import streamlit as st
from PIL import Image, ImageOps, ExifTags

# (선택) Windows/회사망 SSL 재서명 환경에서 인증서 오류 방지
try:
    import certifi_win32  # noqa: F401
except Exception:
    pass

# HEIC 지원
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

# ---------- Firestore ----------
from google.cloud import firestore
from google.oauth2 import service_account

def get_firestore_client():
    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"]
    )
    return firestore.Client(project=creds.project_id, credentials=creds)

db = get_firestore_client()
COLLECTION = st.secrets.get("firestore", {}).get("collection", "measurements")

# ---------- 설정/상수 ----------
DEFAULT_TZ = st.secrets.get("TIMEZONE", "Asia/Seoul")
COLUMNS = ["date", "temperature_c", "humidity_pct", "lat", "lng"]

st.set_page_config(page_title="현장 온·습도 OCR (Firestore)", layout="wide")

# ---------- OCR ----------
import easyocr

@st.cache_resource(show_spinner=False)
def get_reader():
    return easyocr.Reader(["ko","en"], gpu=False)

TEMP_RE = re.compile(r"(-?\d{1,2}(?:[\.,]\d{1,2})?)\s*(?:°?C|℃|도C|도)")
HUMI_RE = re.compile(r"(\d{1,3})\s*%")

def normalize_text(s: str) -> str:
    s = (s.replace("℃", "°C").replace("도 C", "도C").replace("도 c", "도C"))
    s = re.sub(r"(?<=\d)[Oo](?=\d)", "0", s)
    s = re.sub(r"(?<=\d)[lI](?=\d)", "1", s)
    return s

def parse_temp_humi(text: str):
    t = normalize_text(text)
    temp = None; humi = None
    mt = TEMP_RE.search(t)
    if mt:
        try: temp = float(mt.group(1).replace(",", "."))
        except: pass
    mh = HUMI_RE.search(t)
    if mh:
        try:
            hv = int(mh.group(1))
            if 0 <= hv <= 100: humi = hv
        except: pass
    return temp, humi

# ---------- EXIF ----------
def extract_gps_from_bytes(b: bytes):
    try:
        im = Image.open(io.BytesIO(b))
        exif = getattr(im, "_getexif", lambda: None)() or {}
        tags = {ExifTags.TAGS.get(k,k): v for k,v in exif.items()}
        gps = tags.get("GPSInfo")
        if not gps: return None, None
        gps = {ExifTags.GPSTAGS.get(k,k): v for k,v in gps.items()}
        def to_deg(v):
            d=v[0][0]/v[0][1]; m=v[1][0]/v[1][1]; s=v[2][0]/v[2][1]
            return d + m/60 + s/3600
        lat = to_deg(gps["GPSLatitude"]); lon = to_deg(gps["GPSLongitude"])
        if gps.get("GPSLatitudeRef","N").upper() == "S": lat = -lat
        if gps.get("GPSLongitudeRef","E").upper() == "W": lon = -lon
        return lat, lon
    except Exception:
        return None, None

def extract_date_from_exif(b: bytes):
    try:
        im = Image.open(io.BytesIO(b))
        exif = getattr(im, "_getexif", lambda: None)() or {}
        tags = {ExifTags.TAGS.get(k,k): v for k,v in exif.items()}
        dt_str = tags.get("DateTimeOriginal") or tags.get("DateTime") or tags.get("DateTimeDigitized")
        if not dt_str:
            return datetime.now().strftime("%Y-%m-%d")
        dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")

# ---------- Firestore 저장/불러오기 ----------
def save_rows_to_firestore(rows: list[dict]):
    batch = db.batch()
    for r in rows:
        doc = {
            "date": r.get("date"),
            "temperature_c": r.get("temperature_c"),
            "humidity_pct": r.get("humidity_pct"),
            "lat": r.get("lat"),
            "lng": r.get("lng"),
            "created_utc": firestore.SERVER_TIMESTAMP,  # 정렬용 메타
        }
        batch.set(db.collection(COLLECTION).document(), doc)
    batch.commit()

def fetch_recent_from_firestore(limit_n: int = 100):
    docs = (
        db.collection(COLLECTION)
          .order_by("created_utc", direction=firestore.Query.DESCENDING)
          .limit(int(limit_n))
          .stream()
    )
    return [{
        "date": d.get("date"),
        "temperature_c": d.get("temperature_c"),
        "humidity_pct": d.get("humidity_pct"),
        "lat": d.get("lat"),
        "lng": d.get("lng"),
    } for d in (doc.to_dict() for doc in docs)]

# ---------- 세션/초기 표 ----------
if "records" not in st.session_state:
    st.session_state.records = pd.DataFrame(columns=COLUMNS)

# ---------- UI ----------
st.title("현장 온·습도 OCR → Firestore 저장")
st.caption("사진 업로드 → 5가지 정보(JSON) Firestore 저장 → 표에 즉시 반영")

files = st.file_uploader(
    "갤러리에서 사진 선택 (여러 장 가능)",
    type=["jpg","jpeg","png","heic","heif"],
    accept_multiple_files=True
)

if files:
    reader = get_reader()
    new_rows = []
    for f in files:
        b = f.getvalue()
        im = Image.open(io.BytesIO(b)).convert("RGB")
        im = ImageOps.exif_transpose(im)  # 회전 보정
        st.image(im, caption=getattr(f, "name", "gallery.jpg"), use_container_width=True)

        lat, lng   = extract_gps_from_bytes(b)
        date_str   = extract_date_from_exif(b)
        with st.spinner("OCR 인식 중..."):
            text = "\n".join(reader.readtext(np.array(im), detail=0))
        temp, humi = parse_temp_humi(text)

        new_rows.append({
            "date": date_str,
            "temperature_c": temp,
            "humidity_pct": humi,
            "lat": lat,
            "lng": lng,
        })

    # 업로드 즉시 Firestore 저장 + 표 반영
    try:
        save_rows_to_firestore(new_rows)
        st.success(f"Firestore에 {len(new_rows)}건 저장 완료.")
    except Exception as e:
        st.error(f"Firestore 저장 실패: {e}")

    st.session_state.records = pd.concat(
        [pd.DataFrame(new_rows), st.session_state.records],
        ignore_index=True
    )

st.subheader("데이터 표")
edited = st.data_editor(
    st.session_state.records,
    use_container_width=True,
    num_rows="dynamic",
    column_config={
        "date": st.column_config.TextColumn(help="YYYY-MM-DD"),
        "temperature_c": st.column_config.NumberColumn(format="%.1f"),
        "humidity_pct": st.column_config.NumberColumn(min_value=0, max_value=100),
        "lat": st.column_config.NumberColumn(format="%.6f"),
        "lng": st.column_config.NumberColumn(format="%.6f"),
    },
)
st.session_state.records = edited

col_a, col_b = st.columns(2)
with col_a:
    n = st.number_input("최근 불러올 건수", 1, 1000, 100)
    if st.button("Firestore에서 불러오기", use_container_width=True):
        try:
            rows = fetch_recent_from_firestore(int(n))
            if rows:
                st.session_state.records = pd.DataFrame(rows)
                st.success(f"{len(rows)}건 불러왔습니다.")
            else:
                st.warning("데이터가 없습니다.")
        except Exception as e:
            st.error(f"불러오기 실패: {e}")

with col_b:
    st.download_button(
        "CSV 다운로드",
        data=st.session_state.records.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"measurements_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        use_container_width=True,
    )
