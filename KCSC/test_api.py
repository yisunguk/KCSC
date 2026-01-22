import requests
import xmltodict
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_KEY = "-VjbMAN7Sp_Xy0xWLZP2C6evV-Q-RQe7JQxA2Zt4EPc"

def test_user_example():
    print("--- Testing User Example (CodeList) ---")
    url = "https://www.kcsc.re.kr/openApi/CodeList"

    params = {
        "Key": API_KEY,
        "Type": "KDS",
        "Code": "142050"   # Trying a longer code
    }

    try:
        # Added verify=False for local dev, and User-Agent just in case
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, params=params, headers=headers, timeout=10, verify=False)
        
        print("Status:", response.status_code)
        print("Content Start:", response.text[:1000])  # 무조건 text부터 확인
        
        if response.text.strip().startswith("<?xml"):
            print("✅ XML Detected")
            try:
                data = xmltodict.parse(response.text)
                print("Parsed Data Keys:", data.keys())
            except Exception as e:
                print(f"XML Parse Error: {e}")
        else:
            print("❌ Not XML")

    except Exception as e:
        print(f"Error: {e}")

def test_search_list():
    print("\n--- Testing User Example (SearchList) ---")
    url = "https://www.kcsc.re.kr/openApi/SearchList"

    params = {
        "Key": API_KEY,
        "Keyword": "최소피복두께",
        "Type": "KDS"
    }

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, params=params, headers=headers, timeout=10, verify=False)
        
        print("Status:", response.status_code)
        print("Content Start:", response.text[:1000])
        
        if response.text.strip().startswith("<?xml"):
            print("✅ XML Detected")
            try:
                data = xmltodict.parse(response.text)
                print("Parsed Data Keys:", data.keys())
            except Exception as e:
                print(f"XML Parse Error: {e}")
        else:
            print("❌ Not XML")

    except Exception as e:
        print(f"Error: {e}")

def test_code_viewer_manual():
    print("\n--- Testing CodeViewer Manual URL ---")
    # Using a code that might exist. 142050 was used before.
    full_url = f"https://www.kcsc.re.kr/openApi/CodeViewer?Key={API_KEY}&Type=KDS&Code=142050"
    print(f"Testing Manual URL: {full_url}")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(full_url, headers=headers, verify=False, timeout=10)
        print(f"Status: {res.status_code}")
        print(f"Content Length: {len(res.content)}")
        print(f"Content Start: {res.text[:500]}")
        
        if res.text.strip().startswith("<?xml"):
             print("✅ XML Detected")
        elif res.text.strip().startswith("<html"):
             print("❌ HTML Detected")
    except Exception as e:
        print(f"Error: {e}")

def test_code_list_manual():
    print("\n--- Testing CodeList Manual URL ---")
    # Using "14" as per user example
    full_url = f"https://www.kcsc.re.kr/openApi/CodeList?Key={API_KEY}&Type=KDS&Code=14"
    print(f"Testing Manual URL: {full_url}")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(full_url, headers=headers, verify=False, timeout=10)
        print(f"Status: {res.status_code}")
        print(f"Content Length: {len(res.content)}")
        print(f"Content Start: {res.text[:500]}")
        
        if res.text.strip().startswith("<?xml"):
             print("✅ XML Detected")
             try:
                data = xmltodict.parse(res.text)
                print("Parsed Data Keys:", data.keys())
             except Exception as e:
                print(f"XML Parse Error: {e}")
        elif res.text.strip().startswith("<html"):
             print("❌ HTML Detected")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    # test_user_example()
    # test_search_list()
    # test_code_viewer_manual()
    # test_code_list_manual()
    
    print("\n--- Testing SearchList Manual URL (Structure Check) ---")
    full_url = f"https://www.kcsc.re.kr/openApi/SearchList?Key={API_KEY}&Keyword=콘크리트&Type=KDS"
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(full_url, headers=headers, verify=False, timeout=10)
        print(f"Status: {res.status_code}")
        if res.text.strip().startswith("<?xml"):
             print("✅ XML Detected")
             try:
                data = xmltodict.parse(res.text)
                print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])
             except Exception as e:
                print(f"XML Parse Error: {e}")
        else:
             print(f"Content Start: {res.text[:500]}")
    except Exception as e:
        print(f"Error: {e}")
