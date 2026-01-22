import requests
import json

API_KEY = "-VjbMAN7Sp_Xy0xWLZP2C6evV-Q-RQe7JQxA2Zt4EPc"
BASE_URL = "https://kcsc.re.kr/OpenApi/CodeList"

def test(name, params):
    print(f"--- Testing {name} ---")
    try:
        if "key" not in params and "Key" not in params:
            params["key"] = API_KEY
            
        res = requests.get(BASE_URL, params=params, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        print(f"URL: {res.url}")
        print(f"Status: {res.status_code}")
        print("Headers:", res.headers)
        try:
            data = res.json()
            if isinstance(data, list):
                print(f"Result Count: {len(data)}")
                if len(data) > 0:
                    print("First item sample:", data[0])
            else:
                print("Result is not a list:", str(data)[:200])
        except:
            print("Response is not JSON.")
            print(res.text[:200])
    except Exception as e:
        print(f"Error: {e}")
    print("\n")

# 1. With pagination params
test("1. Pagination", {"Type": "KDS", "pageNo": 1, "numOfRows": 10})

# 2. HTTP instead of HTTPS
BASE_URL_HTTP = "http://kcsc.re.kr/OpenApi/CodeList"
print("--- Testing HTTP ---")
try:
    res = requests.get(BASE_URL_HTTP, params={"Type": "KDS", "key": API_KEY}, timeout=10)
    print(f"URL: {res.url}")
    print(f"Status: {res.status_code}")
    print("Response:", res.text[:200])
except Exception as e:
    print(f"Error: {e}")

# 3. Try 'serviceKey' with pagination (common pattern)
test("3. serviceKey + Pagination", {"Type": "KDS", "serviceKey": API_KEY, "pageNo": 1, "numOfRows": 10})
