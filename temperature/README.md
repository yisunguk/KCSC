# 현장 온습도 OCR (간단 버전)
- **표 컬럼**: date, temperature_c, humidity_pct, lat, lng
- 갤러리 사진의 **EXIF**에서 위도/경도/촬영일자 추출
- EasyOCR로 온도/습도 추출(규칙 기반)

## 실행
```bash
pip install -r requirements.txt
streamlit run app_min.py
```
## 시트 스키마
```
date | temperature_c | humidity_pct | lat | lng
```
