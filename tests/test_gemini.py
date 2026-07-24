import os
import sys
from dotenv import load_dotenv

# Add project root directory to sys.path to find core package
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv(override=True)

try:
    from google import genai
    print("google-genai package is installed.")
except ImportError:
    print("google-genai package is NOT installed.")
    sys.exit(1)

api_key = os.getenv("GEMINI_API_KEY")
print(f"API Key present: {bool(api_key)}")

if api_key:
    client = genai.Client(api_key=api_key)
    # Test different model names
    models_to_test = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.0-flash"]
    for m in models_to_test:
        print(f"\nTesting model: {m}...")
        try:
            response = client.models.generate_content(
                model=m,
                contents="Hello, say 'Model works' and specify your model name."
            )
            print(f"Success with {m}!")
            print(f"Response: {response.text.strip()}")
        except Exception as e:
            print(f"Failed with {m}: {e}")
else:
    print("No API Key. Cannot test models.")
