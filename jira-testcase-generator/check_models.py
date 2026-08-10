import google.generativeai as genai
import os
from dotenv import load_dotenv

# Load your API Key
load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

print("Fetching available models for your API key...")
try:
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(f"- {m.name}")
except Exception as e:
    print(f"Error: {e}")