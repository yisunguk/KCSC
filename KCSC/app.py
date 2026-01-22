import streamlit as st
import requests
from bs4 import BeautifulSoup
from openai import AzureOpenAI
import time
import re
from difflib import SequenceMatcher
from typing import Optional, Dict, Tuple, List, Any

# =========================
# 1) Secrets / Clients
# =========================
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

class KCSCBot:
    """
    KCSC OpenAPI (국가건설기준센터) 연동 클라이언트

    - /OpenApi/CodeList : 코드 목록(JSON)
    - /OpenApi/CodeViewer : 코드 본문(JSON)
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        # ✅ 대/소문자 중요
        self.base_url = "https://kcsc.re.kr/OpenApi"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Streamlit; KCSC-Client)",
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
        })

    @staticmethod
    def _strip_html(s: str) -> str:
        if not s:
            return ""
        if "<" in s and ">" in s:
            soup = BeautifulSoup(s, "html.parser")
            return soup.get_text(separator="\n", strip=True)
        return s

    @staticmethod
    def _redact_key(text: str, key: str) -> str:
        return (text or "").replace(key, "***REDACTED***") if key else (text or "")

    @staticmethod
    def _get_first(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
        for k in keys:
            if k in item and item.get(k) not in (None, ""):
                return str(item.get(k))
        return default

    def _get_json(self, endpoint: str, params: Optional[Dict[str, Any]] = None, *, path: Optional[str] = None) -> Any:
        url = f"{self.base_url}/{path}" if path else f"{self.base_url}/{endpoint}"

        params = dict(params or {})
        params.setdefault("key", self.api_key)

        res = self.session.get(url, params=params, timeout=25)
        res.raise_for_status()

        text = (res.text or "").lstrip()

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
            "국가건설기준 검색용 핵심 단어를 1~3개만 뽑아 공백으로 구분해 출력해.\n"
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
    def get_code_list(self, doc_type: str = "KDS") -> List[Dict[str, Any]]:
        cache_key = f"kcsc_codelist_{doc_type}"
        ts_key = f"{cache_key}_ts"
        now = time.time()

        # 6시간 캐시
        if cache_key in st.session_state and ts_key in st.session_state:
            if now - st.session_state[ts_key] < 6 * 3600:
                return st.session_state[cache_key]

        data = self._get_json("CodeList", params={"Type": doc_type})
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
            (r"(철근콘크리트|콘크리트|철근)$", ""),
        ]

        for t in raw:
            t0 = t
            for pat, rep in strip_patterns:
                t0 = re.sub(pat, rep, t0)
            t0 = t0.strip()
            if t0 and t0 not in raw:
                expanded.append(t0)

            if "피복두께" in t:
                expanded += ["피복두께", "피복"]
            if "염해" in t:
                expanded += ["염해", "해안"]
            if "해안" in t:
                expanded += ["해안", "염해"]
            if "내구" in t:
                expanded += ["내구", "내구성"]
            if "철근콘크리트" in t or "RC" in t.upper():
                expanded += ["철근콘크리트", "콘크리트", "철근"]

        tokens = raw + expanded
        uniq: List[str] = []
        for t in tokens:
            t = t.strip()
            if len(t) < 2:
                continue
            if t not in uniq:
                uniq.append(t)
        return uniq

    def search_codes_local(self, keyword: str, doc_type: str = "KDS", top_k: int = 10) -> List[Dict[str, Any]]:
        items = self.get_code_list(doc_type=doc_type)
        tokens = self._normalize_tokens(keyword)

        name_keys = ["Name", "name", "TITLE", "Title", "code_nm", "codeName", "CodeName", "KNAME", "KName"]
        code_keys = ["Code", "code", "CODE", "target_code", "targetCode"]

        def get_name(it: Dict[str, Any]) -> str:
            return self._get_first(it, name_keys, default="")

        def get_code(it: Dict[str, Any]) -> str:
            return self._get_first(it, code_keys, default="")

        def score_contains(it: Dict[str, Any]) -> int:
            name = get_name(it)
            if not name:
                return 0
            name_l = name.lower()
            s = 0
            for t in tokens:
                if t.lower() in name_l:
                    s += 10
            compact = " ".join(tokens).lower()
            if compact and compact in name_l:
                s += 20
            return s

        ranked = sorted(items, key=score_contains, reverse=True)
        ranked = [x for x in ranked if score_contains(x) > 0]

        if not ranked:
            key_compact = "".join(tokens) if tokens else keyword

            def ratio(it: Dict[str, Any]) -> float:
                name = get_name(it)
                if not name:
                    return 0.0
                return SequenceMatcher(None, key_compact.lower(), name.lower()).ratio()

            fuzzy = sorted(items, key=ratio, reverse=True)
            fuzzy = [x for x in fuzzy if ratio(x) >= 0.25]
            ranked = fuzzy

        cleaned: List[Dict[str, Any]] = []
        for it in ranked:
            if get_code(it).strip():
                cleaned.append(it)
            if len(cleaned) >= top_k:
                break

        st.session_state["__last_tokens__"] = tokens
        st.session_state["__last_top_preview__"] = [
            {"name": self._get_first(it, name_keys, ""), "code": self._get_first(it, code_keys, "")}
            for it in cleaned[:10]
        ]
        return cleaned

    # ---------- Code Viewer ----------
    def get_content(self, code: str, doc_type: str = "KDS") -> Tuple[str, str]:
        try:
            data = self._get_json("CodeViewer", params={"Type": doc_type, "Code": code})
        except Exception:
            data = self._get_json("", params={}, path=f"CodeViewer/{doc_type}/{code}")

        code_name = str(data.get("Name") or data.get("name") or "")
        lst = data.get("List") or data.get("list") or []

        parts: List[str] = []
        if isinstance(lst, list):
            for sec in lst:
                title = str(sec.get("Title") or sec.get("title") or "").strip()
                contents = sec.get("Contents") or sec.get("contents") or ""
                contents = self._strip_html(str(contents))

                if title:
                    parts.append(f"## {title}\n{contents}".strip())
                else:
                    parts.append(contents.strip())
        else:
            parts.append(self._strip_html(str(lst)))

        return code_name, "\n\n".join([p for p in parts if p])


# =========================
# 3) Streamlit UI
# =========================
st.set_page_config(page_title="KCSC 설계기준 챗봇", layout="wide")
st.title("🏗️ 실시간 설계기준 AI 검색")

bot = KCSCBot(KCSC_API_KEY)

with st.sidebar:
    st.subheader("검색 설정")
    doc_type = st.selectbox("기준 종류(Type)", ["KDS", "KCS", "EXCS"], index=1)
    top_k = st.slider("검색 후보 개수", 3, 30, 10, 1)
    debug = st.checkbox("디버그 보기", value=False)
    st.caption("※ 첫 실행 시 CodeList를 불러와 캐시합니다(최대 수 초).")

if debug:
    with st.sidebar.expander("디버그: CodeList 샘플/필드 확인", expanded=False):
        try:
            sample = bot.get_code_list(doc_type=doc_type)[:3]
            st.write("샘플 3개:", sample)
            if sample:
                st.write("첫 항목 키:", list(sample[0].keys()))
        except Exception as e:
            st.error(f"CodeList 로드 실패: {type(e).__name__}: {e}")

if user_input := st.chat_input("질문을 입력하세요"):
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.status("KCSC 데이터를 실시간으로 분석 중...", expanded=True) as status:
            try:
                keyword = bot.get_search_keyword(user_input)
                st.write(f"🔍 검색어 추출: **{keyword}**")

                results = bot.search_codes_local(keyword, doc_type=doc_type, top_k=top_k)

                if debug:
                    st.write("🔧 디버그 토큰:", st.session_state.get("__last_tokens__", []))
                    st.write("🔧 상위 후보 미리보기:", st.session_state.get("__last_top_preview__", []))

                if not results:
                    st.error("관련 기준(코드)을 찾지 못했습니다. 검색어를 바꿔서 다시 시도해보세요.")
                    st.info("추천 검색어 예: '피복두께', '염해', '내구성', '철근콘크리트 피복'")
                    status.update(label="분석 완료", state="complete")
                    st.stop()

                best = results[0]
                code = str(best.get("Code") or best.get("code") or best.get("CODE") or best.get("target_code") or best.get("targetCode") or "")
                code_name = str(best.get("Name") or best.get("name") or best.get("TITLE") or best.get("Title") or best.get("code_nm") or "Unknown")
                st.write(f"📖 관련 기준 발견: **{code_name}** (Code: {code})")

                status.update(label="기준 본문 조회 중...", state="running")
                doc_name, content = bot.get_content(code, doc_type=doc_type)

                if not content.strip():
                    st.warning("기준 본문을 가져왔지만 내용이 비어 있습니다. 다른 코드로 재시도하세요.")
                    status.update(label="분석 완료", state="complete")
                    st.stop()

                status.update(label="답변 생성 중...", state="running")
                final_prompt = (
                    f"기준서 내용:\n{content[:12000]}\n\n"
                    f"질문: {user_input}\n\n"
                    "위 기준서 내용을 근거로, 실무자가 이해하기 쉽도록 요점 위주로 답변해줘. "
                    "가능하면 '근거 문장(기준서 발췌)'도 함께 제시해줘."
                )

                response = client.chat.completions.create(
                    model=AZURE_OPENAI_DEPLOYMENT_NAME,
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant explaining construction standards."},
                        {"role": "user", "content": final_prompt}
                    ]
                )
                st.markdown(response.choices[0].message.content)
                st.info(f"출처: {doc_name or code_name} (KCSC {doc_type} / {code})")

                with st.expander("🔎 검색 후보 보기"):
                    for i, it in enumerate(results, 1):
                        nm = it.get("Name") or it.get("name") or it.get("TITLE") or it.get("Title") or it.get("code_nm")
                        cd = it.get("Code") or it.get("code") or it.get("CODE") or it.get("target_code") or it.get("targetCode")
                        st.write(f"{i}. {nm} (Code: {cd})")

            except Exception as e:
                st.error(f"실행 중 오류: {type(e).__name__}: {e}")

            status.update(label="분석 완료", state="complete")
