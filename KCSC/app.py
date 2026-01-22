import streamlit as st
import requests
from bs4 import BeautifulSoup
from openai import AzureOpenAI
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 1. 초기 설정 ---
try:
    KCSC_API_KEY = st.secrets["KCSC_API_KEY"]
    AZURE_OPENAI_ENDPOINT = st.secrets["AZURE_OPENAI_ENDPOINT"]
    AZURE_OPENAI_KEY = st.secrets["AZURE_OPENAI_KEY"]
    AZURE_OPENAI_DEPLOYMENT_NAME = st.secrets["AZURE_OPENAI_DEPLOYMENT_NAME"]
    AZURE_OPENAI_API_VERSION = st.secrets["AZURE_OPENAI_API_VERSION"]
except FileNotFoundError:
    st.error("Secrets file not found. Please set up .streamlit/secrets.toml")
    st.stop()
except KeyError as e:
    st.error(f"Missing secret: {e}")
    st.stop()

client = AzureOpenAI(
    api_key=AZURE_OPENAI_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT
)

class KCSCBot:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://www.kcsc.re.kr/OpenApi"

    def get_search_keyword(self, user_query):
        """질문에서 KCSC 검색에 적합한 단어 1~2개 추출"""
        prompt = f"사용자 질문: '{user_query}'\n위 질문에서 설계기준 검색을 위한 핵심 명사만 1~2개 추출해서 공백으로 구분해줘. 설명이나 특수문자 없이 단어만 출력해. (예: 콘크리트 피복두께)"
        try:
            response = client.chat.completions.create(
                model=AZURE_OPENAI_DEPLOYMENT_NAME,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that extracts search keywords. Output only the keywords separated by spaces. No bullets, no explanations."},
                    {"role": "user", "content": prompt}
                ]
            )
            keyword = response.choices[0].message.content.strip()
            # 첫 줄만 사용하고, 불필요한 특수문자 제거
            keyword = keyword.split('\n')[0].replace('-', '').strip()
            return keyword
        except Exception as e:
            st.error(f"Error generating search keyword: {e}")
            return user_query

    def mock_search(self, keyword):
        """API 권한 문제 시 테스트를 위한 모의 검색 결과 반환"""
        mock_data = {
            "콘크리트": [{"code_nm": "콘크리트구조 설계기준", "target_code": "KDS 14 20 00"}],
            "피복두께": [{"code_nm": "콘크리트구조 철근상세 설계기준", "target_code": "KDS 14 20 50"}],
            "이음": [{"code_nm": "콘크리트구조 철근상세 설계기준", "target_code": "KDS 14 20 50"}],
            "정착": [{"code_nm": "콘크리트구조 철근상세 설계기준", "target_code": "KDS 14 20 50"}]
        }
        for key, results in mock_data.items():
            if key in keyword:
                return results
        return []

    def search_codes(self, keyword):
        """검색어로 KDS/KCS 목록 조회"""
        params = {
            "Key": self.api_key,
            "searchWord": keyword,
            "pageSize": 5,
            "pageNum": 1
        }
        try:
            res = requests.get(f"{self.base_url}/SearchList", params=params, verify=False) # Disable SSL verify for testing
            res.raise_for_status()
            return res.json().get('list', [])
        except requests.exceptions.RequestException as e:
            st.error(f"API Request Error (SearchList): {e}")
            if 'res' in locals():
                st.error(f"Status Code: {res.status_code}")
                # Try to parse as text/html if JSON fails
                try:
                    st.text(f"Response Text: {res.text[:500]}")
                except:
                    pass
            
            # Fallback to Mock Search
            st.warning("⚠️ 검색 API 호출 실패 (권한 또는 엔드포인트 문제). 데모를 위해 모의 데이터를 사용합니다.")
            return self.mock_search(keyword)

        except ValueError:
            st.error("API Response Error: Invalid JSON (HTML/XML received?)")
            if 'res' in locals():
                st.text(f"Response Text: {res.text[:500]}")
            
            st.warning("⚠️ 검색 API 응답 형식 오류. 데모를 위해 모의 데이터를 사용합니다.")
            return self.mock_search(keyword)

    def get_content(self, target_code):
        """특정 코드의 상세 내용 가져오기 및 HTML 정리"""
        params = {"Key": self.api_key, "targetCode": target_code}
        try:
            res = requests.get(f"{self.base_url}/CodeViewer", params=params, verify=False)
            res.raise_for_status()
            html_content = res.json().get('content', '')
            
            soup = BeautifulSoup(html_content, 'html.parser')
            return soup.get_text(separator="\n", strip=True)
        except requests.exceptions.RequestException as e:
            st.error(f"API Request Error (Content): {e}")
            return ""
        except ValueError:
            return ""

# --- 2. Streamlit UI ---
st.set_page_config(page_title="KCSC 설계기준 챗봇", layout="wide")

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
                
                target_code = best_match.get('target_code')
                if target_code:
                    content = bot.get_content(target_code)
                    
                    # 4단계: LLM 답변 생성
                    status.update(label="답변 생성 중...", state="running")
                    final_prompt = f"기준서 내용:\n{content[:4000]}\n\n질문: {user_input}\n\n위 내용을 바탕으로 질문에 답해줘."
                    try:
                        response = client.chat.completions.create(
                            model=AZURE_OPENAI_DEPLOYMENT_NAME,
                            messages=[
                                {"role": "system", "content": "You are a helpful assistant explaining construction standards."},
                                {"role": "user", "content": final_prompt}
                            ]
                        )
                        st.markdown(response.choices[0].message.content)
                        st.info(f"출처: {best_match.get('code_nm')} ({target_code})")
                    except Exception as e:
                        st.error(f"Error generating answer: {e}")
                else:
                    st.error("Target code not found in search result.")
            else:
                st.error("관련된 기준을 찾을 수 없습니다.")
            
            status.update(label="분석 완료", state="complete")
