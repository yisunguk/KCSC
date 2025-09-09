# -*- coding: utf-8 -*-
import json, io
from typing import Optional, Dict, Any
from PIL import Image

def pil_to_jpeg_bytes(im: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()

def is_valid_temp(x):
    try:
        v = float(x)
        return -30.0 <= v <= 60.0
    except Exception:
        return False

def is_valid_humi(x):
    try:
        v = int(float(x))
        return 0 <= v <= 100
    except Exception:
        return False

def merge_fields(base: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k in ("temperature_c", "humidity_pct"):
        if out.get(k) in (None, "", 0) and cand.get(k) not in (None, ""):
            out[k] = cand.get(k)
    out["llm_reason"] = cand.get("reason", "")
    return out

def use_llm_if_needed(parsed: Dict[str, Any], image: Image.Image, ocr_text: str, api_key: Optional[str]) -> Dict[str, Any]:
    # 필요할 때만 호출: 값이 없거나 범위 밖이면 LLM 호출
    temp = parsed.get("temperature_c")
    humi = parsed.get("humidity_pct")
    needs = (temp is None or not is_valid_temp(temp) or humi is None or not is_valid_humi(humi))
    if not needs or not api_key:
        return parsed

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "You are an expert extracting air temperature (°C) and relative humidity (%) from thermometer/"
            "hygrometer photos and their OCR text.\n"
            "Return strict JSON with keys: temperature_c (float or null), humidity_pct (int 0-100 or null), "
            "reason (short explanation).\n"
            "Rules: prefer numbers adjacent to units (°C, C, ℃, '도C', '도' for temperature; %, RH for humidity). "
            "If no unit, use plausible ranges: temperature -30..60, humidity 0..100. "
            "If multiple candidates, pick the one most visually or contextually linked to the label.\n"
        )
        # Convert to JPEG bytes for robustness
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=90)
        img_bytes = buf.getvalue()
        resp = model.generate_content(
            [prompt, {"mime_type": "image/jpeg", "data": img_bytes}, "OCR TEXT:\n" + ocr_text],
            generation_config={"temperature": 0, "response_mime_type": "application/json"}
        )
        js = json.loads(resp.text)
        # Normalize
        out = {
            "temperature_c": float(js.get("temperature_c")) if js.get("temperature_c") not in (None, "") else None,
            "humidity_pct": int(js.get("humidity_pct")) if js.get("humidity_pct") not in (None, "") else None,
            "reason": js.get("reason","")
        }
        return merge_fields(parsed, out)
    except Exception as e:
        parsed["llm_reason"] = f"LLM error: {e}"
        return parsed
