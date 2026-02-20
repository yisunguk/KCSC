import streamlit as st
import requests
import urllib3
from bs4 import BeautifulSoup
from openai import AzureOpenAI
import time
import re
import json
import uuid
import os
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional, Dict, Tuple, List, Any

# KCSC 서버 self-signed 인증서 경고 숨김
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================================================
# 1) Secrets / Clients
# =========================================================
try:
    KCSC_API_KEY = st.secrets["KCSC_API_KEY"]

    AZURE_OPENAI_ENDPOINT = st.secrets["AZURE_OPENAI_ENDPOINT"]
    AZURE_OPENAI_KEY = st.secrets["AZURE_OPENAI_KEY"]
    AZURE_OPENAI_DEPLOYMENT_NAME = st.secrets["AZURE_OPENAI_DEPLOYMENT_NAME"]
    AZURE_OPENAI_API_VERSION = st.secrets["AZURE_OPENAI_API_VERSION"]
except KeyError as e:
    st.error(f"Secrets 설정 누락: {e}\n(Streamlit Cloud → App → Settings → Secrets 확인)")
    st.stop()

client = AzureOpenAI(
    api_key=AZURE_OPENAI_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT
)

# =========================================================
# 2) Chat Persistence Manager
# =========================================================
class ChatManager:
    HISTORY_FILE = "chat_history.json"

    @classmethod
    def load_history(cls) -> Dict[str, Any]:
        if not os.path.exists(cls.HISTORY_FILE):
            return {}
        try:
            with open(cls.HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @classmethod
    def save_history(cls, history: Dict[str, Any]):
        with open(cls.HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    @classmethod
    def get_session(cls, session_id: str) -> List[Dict[str, Any]]:
        history = cls.load_history()
        return history.get(session_id, {}).get("messages", [])

    @classmethod
    def save_message(cls, session_id: str, role: str, content: str):
        history = cls.load_history()
        if session_id not in history:
            history[session_id] = {
                "created_at": datetime.now().isoformat(),
                "title": content[:20] + "..." if role == "user" else "New Chat",
                "messages": []
            }
        
        # 첫 사용자 메시지로 제목 업데이트
        if role == "user" and len(history[session_id]["messages"]) == 0:
             history[session_id]["title"] = content[:20] + "..."

        history[session_id]["messages"].append({"role": role, "content": content})
        cls.save_history(history)

    @classmethod
    def delete_session(cls, session_id: str):
        history = cls.load_history()
        if session_id in history:
            del history[session_id]
            cls.save_history(history)

    @classmethod
    def get_all_sessions(cls) -> List[Dict[str, Any]]:
        history = cls.load_history()
        sessions = []
        for sid, data in history.items():
            sessions.append({
                "id": sid,
                "title": data.get("title", "Untitled"),
                "created_at": data.get("created_at", "")
            })
        # 최신순 정렬
        sessions.sort(key=lambda x: x["created_at"], reverse=True)
        return sessions


# =========================================================
# 3) KCSC Client
# =========================================================
class KCSCBot:
    """
    KCSC OpenAPI (국가건설기준센터) 연동 클라이언트

    공식 문서(지원>API 서비스) 기준:
      - GET https://kcsc.re.kr/OpenApi/CodeList (JSON)
      - GET https://kcsc.re.kr/OpenApi/CodeViewer (JSON)
      - 요청 변수: Type, Code, Key(인증키)   ※ Key 대/소문자 중요할 수 있음

    따라서 인증키 전달을 '가장 튼튼하게' 하기 위해:
      - Query에 Key, key 둘 다 세팅
      - Header에 X-Api-Key도 세팅(비공식/대체 경로 대비)
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://kcsc.re.kr/OpenApi"

        self.session = requests.Session()
        self.session.verify = False  # KCSC 서버 self-signed 인증서 대응
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Streamlit; KCSC-Client)",
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
        })

    # ---------- Utilities ----------
    @staticmethod
    def _strip_html(s: str) -> str:
        if not s:
            return ""
        if "<" not in s or ">" not in s:
            return s

        soup = BeautifulSoup(s, "html.parser")

        # HTML 테이블을 Markdown 테이블로 변환
        for table in soup.find_all("table"):
            md_rows: List[str] = []
            rows = table.find_all("tr")
            for i, row in enumerate(rows):
                cells = row.find_all(["th", "td"])
                cell_texts = [c.get_text(strip=True).replace("|", "/") for c in cells]
                md_rows.append("| " + " | ".join(cell_texts) + " |")
                if i == 0:
                    md_rows.append("| " + " | ".join(["---"] * len(cell_texts)) + " |")
            table.replace_with("\n" + "\n".join(md_rows) + "\n")

        # 이미지 태그를 [그림] 플레이스홀더로 변환 (alt 텍스트 보존)
        for img in soup.find_all("img"):
            alt = img.get("alt", "").strip()
            placeholder = f"[그림: {alt}]" if alt else "[그림]"
            img.replace_with(placeholder)

        return soup.get_text(separator="\n", strip=True)

    @staticmethod
    def _redact_key(text: str, key: str) -> str:
        return (text or "").replace(key, "***REDACTED***") if key else (text or "")

    @staticmethod
    def _get_first(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
        for k in keys:
            v = item.get(k)
            if v not in (None, ""):
                return str(v)
        return default

    def _get_json(self, endpoint: str, params: Optional[Dict[str, Any]] = None, *, path: Optional[str] = None) -> Any:
        url = f"{self.base_url}/{path}" if path else f"{self.base_url}/{endpoint}"

        params = dict(params or {})
        params.setdefault("key", self.api_key)

        res = self.session.get(url, params=params, timeout=25)
        res.raise_for_status()

        text = (res.text or "").lstrip()

        # HTML이 오면 API 실패로 간주
        if text.lower().startswith("<!doctype html") or text.lower().startswith("<html"):
            snippet = self._redact_key(text[:500], self.api_key)
            raise RuntimeError(
                "KCSC OpenAPI가 JSON 대신 HTML을 반환했습니다.\n"
                f"- 요청 URL: {self._redact_key(res.url, self.api_key)}\n"
                f"- 응답 앞부분(500자): {snippet}"
            )

        try:
            return res.json()
        except Exception as e:
            snippet = self._redact_key(text[:500], self.api_key)
            raise RuntimeError(
                "KCSC OpenAPI 응답을 JSON으로 파싱하지 못했습니다.\n"
                f"- 요청 URL: {self._redact_key(res.url, self.api_key)}\n"
                f"- 응답 앞부분(500자): {snippet}\n"
                f"- 원인: {type(e).__name__}: {e}"
            )

    # ---------- Keyword Extraction (LLM) ----------
    def get_search_keyword(self, user_query: str) -> str:
        prompt = (
            f"사용자 질문: '{user_query}'\n"
            "국가건설기준(KDS/KCS) 검색용 핵심 단어를 1~3개만 뽑아 공백으로 구분해 출력해.\n"
            "너무 긴 합성어 대신 기준서 제목에 들어갈 법한 단어를 사용해. 예: 피복두께 염해 내구성\n"
            "설명/문장/따옴표/특수문자 없이 단어만."
        )
        try:
            response = client.chat.completions.create(
                model=AZURE_OPENAI_DEPLOYMENT_NAME,
                messages=[
                    {"role": "system", "content": "Output only Korean keywords separated by a single space."},
                    {"role": "user", "content": prompt}
                ]
            )
            keyword = response.choices[0].message.content.strip().splitlines()[0]
            keyword = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", keyword)
            keyword = " ".join(keyword.split())
            return keyword if keyword else user_query
        except Exception:
            return user_query

    # ---------- Code List / Search ----------
    def get_code_list(self, doc_type: str = "KCS") -> List[Dict[str, Any]]:
        cache_key = f"kcsc_codelist_{doc_type}"
        ts_key = f"{cache_key}_ts"
        now = time.time()

        if cache_key in st.session_state and ts_key in st.session_state:
            if now - st.session_state[ts_key] < 6 * 3600:
                return st.session_state[cache_key]

        data = self._get_json("CodeList", params={"Type": doc_type})

        # 문서상 CodeList는 list
        if not isinstance(data, list):
            raise RuntimeError(f"CodeList 응답 형식이 예상과 다릅니다: {type(data)}")

        st.session_state[cache_key] = data
        st.session_state[ts_key] = now
        return data

    def _normalize_tokens(self, keyword: str) -> List[str]:
        raw = [t for t in keyword.split() if t]
        expanded: List[str] = []

        strip_patterns = [
            (r"^(최소|최대|기준|규정|설계|시공|내구|내구성|환경|노출|조건)", ""),
            (r"(기준|규정|환경|노출|조건)$", ""),
        ]

        for t in raw:
            t0 = t
            for pat, rep in strip_patterns:
                t0 = re.sub(pat, rep, t0)
            t0 = t0.strip()
            if t0 and t0 not in raw:
                expanded.append(t0)

            if "피복" in t:
                expanded += ["피복", "피복두께"]
            if "피복두께" in t:
                expanded += ["피복두께", "피복"]
            if "염해" in t or "해안" in t:
                expanded += ["염해", "해안", "염분"]
            if "내구" in t:
                expanded += ["내구", "내구성", "내구설계"]
            if "철근" in t:
                expanded += ["철근", "철근콘크리트", "RC"]
            if "콘크리트" in t:
                expanded += ["콘크리트", "철근콘크리트", "RC"]

        tokens = raw + expanded
        uniq: List[str] = []
        for t in tokens:
            t = t.strip()
            if len(t) < 2:
                continue
            if t not in uniq:
                uniq.append(t)
        return uniq

    def extract_code_number(self, query: str) -> Optional[str]:
        # 14 20 10, 14.20.10, 14-20-10, 142010 등
        # KCS 14 20 10 처럼 앞에 영문이 있을 수도 있음
        # 단순히 연속된 숫자(공백/./- 포함)가 4자리 이상이면 코드로 의심
        match = re.search(r"(\d{1,2}[\s\.-]?\d{2}[\s\.-]?\d{2,3})", query)
        if match:
            # 공백, ., - 제거하고 순수 숫자만 반환
            return re.sub(r"[\s\.-]", "", match.group(1))
        return None

    def search_codes_local(self, keyword: str, doc_type: str = "KCS", top_k: int = 10) -> List[Dict[str, Any]]:
        items = self.get_code_list(doc_type=doc_type)
        
        # 디버그 정보 초기 저장 (Fast Track 등 조기 리턴 시에도 반영되도록 상단 이동)
        st.session_state["__last_loaded_count__"] = len(items)
        
        # 1) Fast Track: 코드 번호 추출
        extracted_code = self.extract_code_number(keyword)
        fast_track_results = []
        
        name_keys = ["Name", "name", "TITLE", "Title"]
        code_keys = ["Code", "code", "CODE", "FullCode", "fullCode"]

        def get_name(it: Dict[str, Any]) -> str:
            return self._get_first(it, name_keys, default="")

        def get_code(it: Dict[str, Any]) -> str:
            return self._get_first(it, code_keys, default="")

        if extracted_code:
            # 코드 번호로 필터링 (Code, FullCode 등 모든 가능성 체크)
            for it in items:
                # 검사할 후보 값들 수집
                candidates = []
                for k in ["Code", "code", "CODE", "FullCode", "fullCode"]:
                    val = it.get(k)
                    if val:
                        candidates.append(str(val))
                
                matched = False
                for c_val in candidates:
                    c_clean = c_val.replace(" ", "").replace(".", "").replace("-", "")
                    if extracted_code in c_clean:
                        matched = True
                        break
                
                if matched:
                    fast_track_results.append(it)
            
            # Fast Track 결과가 있으면 그것만 반환하거나 최상단에 배치
            if fast_track_results:
                # 정확도순 정렬 (길이가 짧을수록, 즉 더 정확하게 일치할수록 우선)
                fast_track_results.sort(key=lambda x: len(get_code(x)))
                
                # 디버그 정보 업데이트 (Fast Track)
                st.session_state["__last_tokens__"] = [extracted_code]
                st.session_state["__last_top_preview__"] = [
                    {"name": get_name(it), "code": get_code(it)}
                    for it in fast_track_results[:10]
                ]
                return fast_track_results[:top_k]

        # 2) 일반 키워드 검색
        tokens = self._normalize_tokens(keyword)

        def score_contains(it: Dict[str, Any]) -> int:
            name = get_name(it)
            if not name:
                return 0
            name_l = name.lower()
            s = 0
            for t in tokens:
                if t.lower() in name_l:
                    s += 10
            return s

        ranked = sorted(items, key=score_contains, reverse=True)
        ranked = [x for x in ranked if score_contains(x) > 0]

        # fallback: fuzzy
        if not ranked:
            key_compact = "".join(tokens) if tokens else keyword

            def ratio(it: Dict[str, Any]) -> float:
                name = get_name(it)
                if not name:
                    return 0.0
                return SequenceMatcher(None, key_compact.lower(), name.lower()).ratio()

            fuzzy = sorted(items, key=ratio, reverse=True)
            fuzzy = [x for x in fuzzy if ratio(x) >= 0.20]
            ranked = fuzzy

        cleaned: List[Dict[str, Any]] = []
        for it in ranked:
            if get_code(it).strip():
                cleaned.append(it)
            if len(cleaned) >= top_k:
                break

        # 디버그 저장 (일반 검색)
        st.session_state["__last_tokens__"] = tokens
        st.session_state["__last_top_preview__"] = [
            {"name": get_name(it), "code": get_code(it)}
            for it in cleaned[:10]
        ]
        return cleaned

    # ---------- Code Viewer ----------
    def _fetch_raw_sections(self, code: str, doc_type: str) -> Tuple[str, List[Dict[str, Any]]]:
        """API에서 원본 섹션 리스트를 가져온다. (code_name, sections)"""
        try:
            data = self._get_json("", params={}, path=f"CodeViewer/{doc_type}/{code}")
        except Exception:
            data = self._get_json("CodeViewer", params={"Type": doc_type, "Code": code})

        if isinstance(data, list):
            if not data:
                return "", []
            data = data[0]

        code_name = str(data.get("Name") or data.get("name") or "")
        lst = data.get("List") or data.get("list") or []
        if not isinstance(lst, list):
            lst = [{"title": "", "contents": str(lst)}]
        return code_name, lst

    def _sections_to_text(self, sections: List[Dict[str, Any]]) -> str:
        """섹션 리스트를 텍스트로 변환"""
        parts: List[str] = []
        for sec in sections:
            title = str(sec.get("Title") or sec.get("title") or "").strip()
            # title에 포함된 HTML 이미지 태그 제거
            title = re.sub(r"<img[^>]*>", "", title).strip()
            contents = sec.get("Contents") or sec.get("contents") or ""
            contents = self._strip_html(str(contents))
            if title:
                parts.append(f"## {title}\n{contents}".strip())
            elif contents.strip():
                parts.append(contents.strip())
        return "\n\n".join([p for p in parts if p])

    @staticmethod
    def _expand_tokens(tokens: List[str]) -> List[Tuple[str, float]]:
        """
        한국어 복합어를 서브토큰으로 분해하여 (token, weight) 리스트 반환.
        원본 토큰은 높은 가중치, 서브토큰은 낮은 가중치.
        예: "적설하중" → [("적설하중", 6), ("적설", 2), ("하중", 2)]
        """
        result: List[Tuple[str, float]] = []
        seen: set = set()
        for t in tokens:
            t_low = t.lower()
            if t_low not in seen:
                # 원본 토큰 (길이 기반 가중치)
                result.append((t_low, min(len(t), 6)))
                seen.add(t_low)

            # 3글자 이상이면 2글자 서브토큰 생성
            if len(t) >= 3:
                for j in range(len(t) - 1):
                    sub = t[j:j+2].lower()
                    if len(sub) >= 2 and sub not in seen:
                        result.append((sub, 1.0))
                        seen.add(sub)
            # 4글자 이상이면 3글자 서브토큰 생성
            if len(t) >= 4:
                for j in range(len(t) - 2):
                    sub = t[j:j+3].lower()
                    if sub not in seen:
                        result.append((sub, 2.0))
                        seen.add(sub)
        return result

    def _extract_relevant_sections(
        self, sections: List[Dict[str, Any]], query: str, keyword: str, max_chars: int = 15000
    ) -> str:
        """
        사용자 질문과 관련된 섹션만 추출.
        전체 문서가 max_chars 이하이면 그대로 반환하고,
        초과하면 키워드 매칭 기반으로 관련 섹션을 선별한다.
        """
        full_text = self._sections_to_text(sections)
        if len(full_text) <= max_chars:
            return full_text

        # 키워드 토큰 추출 (중복 제거)
        combined = f"{query} {keyword}"
        raw_tokens = [t for t in combined.split() if len(t) >= 2]
        stopwords = {"에서", "이란", "무엇", "얼마", "어떻게", "대한", "대해", "알려줘",
                      "설명해", "기준", "지역의", "지역", "대하여", "관한", "관련"}
        seen: set = set()
        unique_tokens: List[str] = []
        for t in raw_tokens:
            t_low = t.lower()
            if t_low not in seen and t_low not in stopwords:
                unique_tokens.append(t)
                seen.add(t_low)

        # 서브토큰 포함 확장
        weighted_tokens = self._expand_tokens(unique_tokens)

        # 각 섹션의 관련도 점수 계산
        scored: List[Tuple[float, int, str]] = []
        for i, sec in enumerate(sections):
            title = str(sec.get("Title") or sec.get("title") or "")
            title = re.sub(r"<img[^>]*>", "", title).strip()
            contents_raw = str(sec.get("Contents") or sec.get("contents") or "")
            contents_text = self._strip_html(contents_raw)
            searchable = f"{title} {contents_text}".lower()

            score = 0.0
            matched_count = 0
            for t_low, weight in weighted_tokens:
                if t_low in searchable:
                    score += weight
                    matched_count += 1
                    if t_low in title.lower():
                        score += weight * 0.5

            # 다중 매칭 보너스
            if matched_count >= 3:
                score *= (1.0 + 0.2 * matched_count)

            if score > 0:
                block = f"## {title}\n{contents_text}".strip() if title else contents_text.strip()
                scored.append((score, i, block))

        scored.sort(key=lambda x: (-x[0], x[1]))

        # 상위 섹션 + 인접 섹션 포함 (전후 컨텍스트)
        top_indices: set = set()
        for _, idx, _ in scored[:20]:
            top_indices.add(idx)
            # 전후 1개 섹션도 포함 (연속된 내용일 수 있음)
            if idx > 0:
                top_indices.add(idx - 1)
            if idx < len(sections) - 1:
                top_indices.add(idx + 1)

        # 인접 섹션의 블록 텍스트 생성
        all_blocks: Dict[int, str] = {}
        for _, idx, block in scored:
            all_blocks[idx] = block
        for idx in top_indices:
            if idx not in all_blocks:
                sec = sections[idx]
                title = str(sec.get("Title") or sec.get("title") or "")
                title = re.sub(r"<img[^>]*>", "", title).strip()
                contents_text = self._strip_html(str(sec.get("Contents") or sec.get("contents") or ""))
                block = f"## {title}\n{contents_text}".strip() if title else contents_text.strip()
                all_blocks[idx] = block

        # 점수 기반으로 선택 (인접 섹션은 원본 점수의 50%로 평가)
        score_map: Dict[int, float] = {}
        for s, idx, _ in scored:
            score_map[idx] = s
        candidates = []
        for idx in top_indices:
            s = score_map.get(idx, score_map.get(idx - 1, 0) * 0.5)
            candidates.append((s, idx, all_blocks[idx]))
        candidates.sort(key=lambda x: (-x[0], x[1]))

        selected: List[Tuple[int, str]] = []
        total_len = 0
        for score, idx, block in candidates:
            if not block.strip():
                continue
            if total_len + len(block) > max_chars:
                remaining = max_chars - total_len
                if remaining > 200:
                    selected.append((idx, block[:remaining] + "\n... (이하 생략)"))
                break
            selected.append((idx, block))
            total_len += len(block) + 2

        if not selected:
            return full_text[:max_chars]

        selected.sort(key=lambda x: x[0])
        return "\n\n".join([text for _, text in selected])

    def get_content(self, code: str, doc_type: str = "KCS", query: str = "", keyword: str = "") -> Tuple[str, str]:
        code_name, sections = self._fetch_raw_sections(code, doc_type)
        if not sections:
            return code_name, ""

        if query or keyword:
            content = self._extract_relevant_sections(sections, query, keyword)
        else:
            content = self._sections_to_text(sections)
        return code_name, content


# =========================================================
# 3) Streamlit UI
# =========================================================
st.set_page_config(page_title="KCSC 설계기준 챗봇", layout="wide")

# Custom CSS for Gemini-like greeting & Centered Layout
st.markdown("""
<style>
    /* 1. Greeting Container Centering */
    .greeting-container {
        display: flex;
        flex-direction: column;
        align-items: center; /* Center horizontally */
        justify-content: center;
        margin-top: 10vh;
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, Roboto, 'Helvetica Neue', 'Segoe UI', 'Apple SD Gothic Neo', 'Noto Sans KR', 'Malgun Gothic', 'Apple Color Emoji', 'Segoe UI Emoji', 'Segoe UI Symbol', sans-serif;
        text-align: center;
    }
    .greeting-sub {
        font-size: 2.5rem;
        font-weight: 500;
        color: #6e6e6e;
        margin-bottom: 10px;
        background: -webkit-linear-gradient(45deg, #4285f4, #9b72cb, #d96570);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        display: inline-block;
    }
    .greeting-main {
        font-size: 3.5rem;
        font-weight: 600;
        color: #c4c7c5;
        line-height: 1.2;
    }
    
    /* 2. Chat Message Centering & Width Control */
    .stChatMessage {
        max-width: 800px; /* Limit width */
        margin-left: auto !important;
        margin-right: auto !important;
    }
    
    /* 3. Chat Input Centering */
    .stChatInput {
        max-width: 800px;
        margin-left: auto !important;
        margin-right: auto !important;
    }

    /* Hide the default title if we are showing the greeting */
    .stApp header {
        background-color: transparent;
    }
</style>
""", unsafe_allow_html=True)

bot = KCSCBot(KCSC_API_KEY)

# Session Management
if "current_session_id" not in st.session_state:
    st.session_state.current_session_id = str(uuid.uuid4())

# Load messages for current session
st.session_state.messages = ChatManager.get_session(st.session_state.current_session_id)

with st.sidebar:
    st.subheader("검색 설정")
    doc_type_selected = st.selectbox("기준 종류(Type)", ["KDS", "KCS", "KWCS"], index=1)
    top_k = st.slider("검색 후보 개수", 3, 30, 18, 1)
    debug = st.checkbox("디버그 보기", value=False)
    st.caption("※ 첫 실행 시 CodeList를 불러와 캐시합니다(최대 수 초).")
    
    st.divider()
    
    # Chat History UI
    st.subheader("💬 대화 기록")
    if st.button("➕ 새 대화 시작", use_container_width=True):
        st.session_state.current_session_id = str(uuid.uuid4())
        st.session_state.messages = []
        st.rerun()

    sessions = ChatManager.get_all_sessions()
    for s in sessions:
        label = s["title"]
        if s["id"] == st.session_state.current_session_id:
            label = f"📌 {label}"
        
        col1, col2 = st.columns([0.8, 0.2])
        if col1.button(label, key=f"btn_{s['id']}", help=s["created_at"]):
            st.session_state.current_session_id = s["id"]
            st.rerun()
        if col2.button("🗑️", key=f"del_{s['id']}"):
            ChatManager.delete_session(s["id"])
            if st.session_state.current_session_id == s["id"]:
                st.session_state.current_session_id = str(uuid.uuid4())
            st.rerun()

if debug:
    with st.sidebar.expander("디버그 정보", expanded=True):
        st.write("Session ID:", st.session_state.current_session_id)
        try:
            items = bot.get_code_list(doc_type=doc_type_selected)
            st.write("CodeList 개수:", len(items))
            if items:
                st.write("첫 항목 키:", list(items[0].keys()))
                st.write("첫 항목 샘플:", items[0])
        except Exception as e:
            st.error(f"CodeList 로드 실패: {type(e).__name__}: {e}")

# Display chat messages from history on app rerun
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Show greeting if history is empty
if not st.session_state.messages:
    st.markdown("""
        <div class="greeting-container">
            <div class="greeting-sub">✨ 사용자님, 안녕하세요</div>
            <div class="greeting-main">무엇을 도와드릴까요?</div>
        </div>
    """, unsafe_allow_html=True)

if user_input := st.chat_input("질문을 입력하세요"):
    # Add user message to chat history
    st.session_state.messages.append({"role": "user", "content": user_input})
    ChatManager.save_message(st.session_state.current_session_id, "user", user_input)

    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.status("KCSC 데이터를 실시간으로 분석 중...", expanded=True) as status:
            try:
                keyword = bot.get_search_keyword(user_input)
                st.write(f"🔍 검색어 추출: **{keyword}**")

                # 1) 우선 선택된 doc_type으로 검색
                target_doc_type = doc_type_selected
                results = bot.search_codes_local(keyword, doc_type=target_doc_type, top_k=top_k)

                # 2) Auto-Retry: 결과가 없으면 다른 타입 검색
                if not results:
                    st.warning(f"'{target_doc_type}'에서 결과를 찾지 못했습니다. 다른 기준을 검색합니다...")
                    other_types = [t for t in ["KDS", "KCS", "KWCS"] if t != target_doc_type]
                    
                    for t in other_types:
                        results = bot.search_codes_local(keyword, doc_type=t, top_k=top_k)
                        if results:
                            target_doc_type = t
                            st.success(f"'{target_doc_type}'에서 관련 기준을 발견했습니다!")
                            break

                if debug:
                    st.write("🔧 CodeList 로드 개수:", st.session_state.get("__last_loaded_count__", None))
                    st.write("🔧 디버그 토큰:", st.session_state.get("__last_tokens__", []))
                    st.write("🔧 상위 후보 미리보기:", st.session_state.get("__last_top_preview__", []))

                # ✅ CodeList가 0개면 인증키 전달 방식 문제일 가능성이 매우 큼
                if st.session_state.get("__last_loaded_count__", 0) == 0:
                    st.error("CodeList가 0개로 로드되었습니다. (인증키 Key 파라미터/헤더 전달 문제 가능성)")
                    st.info("디버그 보기를 켜서 CodeList 개수가 0인지 확인해보세요.")
                    status.update(label="분석 완료", state="complete")
                    st.stop()

                if not results:
                    st.error("관련 기준(코드)을 찾지 못했습니다. 검색어를 바꿔서 다시 시도해보세요.")
                    st.info("추천 검색어 예: '피복두께', '염해', '내구성', '철근콘크리트 피복', '염해 내구 설계'")
                    status.update(label="분석 완료", state="complete")
                    st.stop()

                best = results[0]
                code = str(best.get("Code") or best.get("code") or best.get("CODE") or best.get("FullCode") or best.get("fullCode") or "")
                code_name = str(best.get("Name") or best.get("name") or best.get("TITLE") or best.get("Title") or "Unknown")
                st.write(f"📖 관련 기준 발견: **{code_name}** (Code: {code})")

                status.update(label="기준 본문 조회 중...", state="running")
                # 여기서 target_doc_type을 써야 함 (Auto-Retry로 바뀌었을 수 있음)
                doc_name, content = bot.get_content(
                    code, doc_type=target_doc_type,
                    query=user_input, keyword=keyword
                )

                if not content.strip():
                    st.warning("기준 본문을 가져왔지만 내용이 비어 있습니다. 다른 코드로 재시도하세요.")
                    status.update(label="분석 완료", state="complete")
                    st.stop()

                status.update(label="답변 생성 중...", state="running")
                final_prompt = (
                    f"기준서 내용 (질문과 관련된 섹션 발췌):\n{content[:15000]}\n\n"
                    f"질문: {user_input}\n\n"
                    "위 기준서 내용을 근거로, 실무자가 이해하기 쉽도록 요점 위주로 답변해줘. "
                    "가능하면 '근거 문장(기준서 발췌)'도 함께 제시해줘. "
                    "[그림] 표시가 있으면 해당 그림/도표를 참조해야 한다고 안내해줘."
                )

                # 대화 기록 포함 (Context 유지)
                messages_payload = [
                    {"role": "system", "content": "You are a helpful assistant explaining construction standards."}
                ]
                # 현재 질문(user_input)은 session_state에 이미 추가됨.
                # 이전 대화 기록만 가져오기 (마지막 항목 제외)
                for m in st.session_state.messages[:-1]:
                    messages_payload.append({"role": m["role"], "content": m["content"]})
                
                # 이번 턴의 질문(Context 포함) 추가
                messages_payload.append({"role": "user", "content": final_prompt})

                response = client.chat.completions.create(
                    model=AZURE_OPENAI_DEPLOYMENT_NAME,
                    messages=messages_payload,
                    stream=True
                )
                full_response = st.write_stream(response)
                
                # 응답 저장
                st.session_state.messages.append({"role": "assistant", "content": full_response})
                ChatManager.save_message(st.session_state.current_session_id, "assistant", full_response)
                
                st.info(f"출처: {doc_name or code_name} (KCSC {target_doc_type} / {code})")

                with st.expander("🔎 검색 후보 보기"):
                    for i, it in enumerate(results, 1):
                        nm = it.get("Name") or it.get("name") or it.get("TITLE") or it.get("Title")
                        cd = it.get("Code") or it.get("code") or it.get("CODE") or it.get("FullCode") or it.get("fullCode")
                        st.write(f"{i}. {nm} (Code: {cd})")

            except Exception as e:
                st.error(f"실행 중 오류: {type(e).__name__}: {e}")

            status.update(label="분석 완료", state="complete")
