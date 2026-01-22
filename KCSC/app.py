import streamlit as st
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# --- 1. 초기 설정 ---
# Secrets handling with fallback for local development if needed, 
# though user specified they will put keys in secrets.
try:
    KCSC_API_KEY = st.secrets["KCSC_API_KEY"]
    GENAI_API_KEY = st.secrets["GENAI_API_KEY"]
except FileNotFoundError:
    st.error("Secrets file not found. Please set up .streamlit/secrets.toml")
    st.stop()
except KeyError as e:
    st.error(f"Missing secret: {e}")
    st.stop()

genai.configure(api_key=GENAI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

class KCSCBot:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://www.kcsc.re.kr/api"

    def get_search_keyword(self, user_query):
        """질문에서 KCSC 검색에 적합한 단어 1~2개 추출"""
        prompt = f"사용자 질문: '{user_query}'\n위 질문에서 설계기준 검색을 위한 핵심 명사만 추출해줘. (예: 콘크리트 피복두께)"
        try:
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            st.error(f"Error generating search keyword: {e}")
            return user_query # Fallback to user query

    def search_codes(self, keyword):
        """검색어로 KDS/KCS 목록 조회"""
        params = {
            "apiKey": self.api_key,
            "searchWord": keyword,
            "pageSize": 5,
            "pageNum": 1
        }
        # 실제 API 엔드포인트는 KCSC 가이드를 참조하여 SearchList 등으로 수정 필요
        # Assuming SearchList is the correct endpoint based on user input
        try:
            res = requests.get(f"{self.base_url}/SearchList", params=params)
            res.raise_for_status() # Raise error for bad status codes
            return res.json().get('list', [])
        except requests.exceptions.RequestException as e:
            st.error(f"API Request Error (Search): {e}")
            return []
        except ValueError:
            st.error("API Response Error: Invalid JSON")
            return []

    def get_content(self, target_code):
        """특정 코드의 상세 내용 가져오기 및 HTML 정리"""
        params = {"apiKey": self.api_key, "targetCode": target_code}
        try:
            res = requests.get(f"{self.base_url}/CodeViewer", params=params)
            res.raise_for_status()
            html_content = res.json().get('content', '')
            
            # HTML 태그 제거 및 텍스트만 추출 (LLM 토큰 절약)
            soup = BeautifulSoup(html_content, 'html.parser')
            return soup.get_text(separator="\n", strip=True)
        except requests.exceptions.RequestException as e:
            st.error(f"API Request Error (Content): {e}")
            return ""
        except ValueError:
            return ""

# --- 2. Streamlit UI ---
st.set_page_config(page_title="KCSC 설계기준 챗봇", layout="wide")

# Initialize bot only if API key is available
if 'KCSC_API_KEY' in locals():
    bot = KCSCBot(KCSC_API_KEY)

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

st.title("🏗️ 실시간 설계기준 AI 검색")

if user_input := st.chat_input("질문을 입력하세요"):
    with st.chat_message("user"):
        st.markdown(user_input)
    
    with st.chat_message("assistant"):
        with st.status("KCSC 데이터를 실시간으로 분석 중...", expanded=True) as status:
            # 1단계: 검색어 추출
            keyword = bot.get_search_keyword(user_input)
            st.write(f"🔍 검색어 추출: **{keyword}**")
            
            # 2단계: 관련 코드 검색
            search_results = bot.search_codes(keyword)
            
            if search_results:
                # 3단계: 가장 관련성 높은 상위 1개 코드의 내용 가져오기
                best_match = search_results[0]
                st.write(f"📖 관련 기준 발견: {best_match.get('code_nm', 'Unknown Code')}")
                # Assuming 'target_code' is the correct key, but user code used 'target_code' 
                # while API might return something else. Keeping user's key for now.
                target_code = best_match.get('target_code')
                if target_code:
                    content = bot.get_content(target_code)
                    
                    # 4단계: LLM 답변 생성
                    status.update(label="답변 생성 중...", state="running")
                    final_prompt = f"기준서 내용:\n{content[:4000]}\n\n질문: {user_input}\n\n위 내용을 바탕으로 질문에 답해줘."
                    try:
                        response = model.generate_content(final_prompt)
                        st.markdown(response.text)
                        st.info(f"출처: {best_match.get('code_nm')} ({target_code})")
                    except Exception as e:
                        st.error(f"Error generating answer: {e}")
                else:
                    st.error("Target code not found in search result.")
            else:
                st.error("관련된 기준을 찾을 수 없습니다.")
            
            status.update(label="분석 완료", state="complete")
