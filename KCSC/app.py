import streamlit as st
import requests
from bs4 import BeautifulSoup
from openai import AzureOpenAI
import time
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


# =========================
# 2) KCSC Client
# =========================
class KCSCBot:
    """
    KCSC OpenAPI (국가건설기준센터) 연동 클라이언트

    - CodeList / CodeViewer는 JSON 응답
    - 검색 전용 엔드포인트(SearchList 등)가 불확실/비공식일 수 있어,
      1) CodeList로 전체 코드 목록을 가져오고(캐시)
      2) 이름(Name) 기반 로컬 검색
      3) CodeViewer로 본문을 가져옴
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

    # ---------- Utilities ----------
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
        if not key:
            return text
        return text.replace(key, "***REDACTED***")

    def _get_json(self, endpoint: str, params: Optional[Dict[str, Any]] = None, *, path: Optional[str] = None) -> Any:
        """
        endpoint: 'CodeList' | 'CodeViewer' ...
        path: endpoint 대신 전체 path를 지정할 때 사용 (예: 'CodeViewer/KDS/101000')
        """
        if path:
            url = f"{self.base_url}/{path}"
        else:
            url = f"{self.base_url}/{endpoint}"

        # ✅ 인증키 파라미터는 `key`(소문자)로 세팅
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
            "위 질문에서 설계기준 검색에 필요한 핵심 명사 1~2개만 뽑아 공백으로 구분해 출력해.\n"
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
            keyword = keyword.replace("-", " ").replace("/", " ").strip()
            keyword = " ".join(keyword.split())
            return keyword if keyword else user_query
        except Exception as e:
            st.warning(f"검색어 추출 실패(LLM). 원문 질문으로 검색합니다. ({type(e).__name__})")
            return user_query

    # ---------- Code List / Search ----------
    def get_code_list(self, doc_type: str = "KDS") -> List[Dict[str, Any]]:
        """
        CodeList는 전체 코드 목록을 반환. 캐시 후 로컬 검색 권장.
        """
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

    def search_codes_local(self, keyword: str, doc_type: str = "KDS", top_k: int = 10) -> List[Dict[str, Any]]:
        """
        CodeList를 가져온 뒤 Name 기반 로컬 검색
        """
        items = self.get_code_list(doc_type=doc_type)
        tokens = [t for t in keyword.split() if t]

        def name_of(item: Dict[str, Any]) -> str:
            return str(item.get("Name") or item.get("name") or "")

        def score(item: Dict[str, Any]) -> int:
            name = name_of(item)
            name_l = name.lower()
            s = 0
            for t in tokens:
                if t.lower() in name_l:
                    s += 10
            if " ".join(tokens).lower() == name_l.strip():
                s += 50
            return s

        ranked = sorted(items, key=score, reverse=True)
        ranked = [x for x in ranked if score(x) > 0]
        return ranked[:top_k]

    # ---------- Code Viewer ----------
    def get_content(self, code: str, doc_type: str = "KDS") -> Tuple[str, str]:
        """
        return (code_name, content_text)
        """
        # 1) 쿼리 파라미터 방식
        try:
            data = self._get_json("CodeViewer", params={"Type": doc_type, "Code": code})
        except Exception:
            # 2) 경로 방식 fallback
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
    doc_type = st.selectbox("기준 종류(Type)", ["KDS", "KCS", "EXCS"], index=0)
    top_k = st.slider("검색 후보 개수", 3, 30, 10, 1)
    st.caption("※ 첫 실행 시 CodeList를 불러와 캐시합니다(최대 수 초).")

if user_input := st.chat_input("질문을 입력하세요"):
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.status("KCSC 데이터를 실시간으로 분석 중...", expanded=True) as status:
            try:
                # 1) 검색어 추출
                keyword = bot.get_search_keyword(user_input)
                st.write(f"🔍 검색어 추출: **{keyword}**")

                # 2) 코드 검색(로컬)
                results = bot.search_codes_local(keyword, doc_type=doc_type, top_k=top_k)

                if not results:
                    st.error("관련 기준(코드)을 찾지 못했습니다. 검색어를 바꿔서 다시 시도해보세요.")
                    status.update(label="분석 완료", state="complete")
                    st.stop()

                best = results[0]
                code = str(best.get("Code") or best.get("code") or "")
                code_name = str(best.get("Name") or best.get("name") or "Unknown")
                st.write(f"📖 관련 기준 발견: **{code_name}** (Code: {code})")

                # 3) 본문 조회
                status.update(label="기준 본문 조회 중...", state="running")
                doc_name, content = bot.get_content(code, doc_type=doc_type)

                if not content.strip():
                    st.warning("기준 본문을 가져왔지만 내용이 비어 있습니다. 다른 코드로 재시도하세요.")
                    status.update(label="분석 완료", state="complete")
                    st.stop()

                # 4) LLM 답변 생성
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

                # 후보 목록
                with st.expander("🔎 검색 후보 보기"):
                    for i, it in enumerate(results, 1):
                        st.write(f"{i}. {it.get('Name')} (Code: {it.get('Code')})")

            except Exception as e:
                st.error(f"실행 중 오류: {type(e).__name__}: {e}")

            status.update(label="분석 완료", state="complete")
