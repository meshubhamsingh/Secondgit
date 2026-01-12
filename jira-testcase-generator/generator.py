from google import genai
import pandas as pd
import json
import os

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def load_ui_structure():
    with open("ui_structure.txt", "r", encoding="utf-8") as f:
        return f.read()

def generate_test_cases(jira_text):
    ui_structure = load_ui_structure()

    prompt = f"""
You are an expert QA Engineer.
Use the following UI structure when writing test steps:

--- UI STRUCTURE ---
{ui_structure}
---------------------

JIRA STORY:
{jira_text}

TASK:
1. Generate Test Scenarios
2. For each scenario, generate test cases with detailed UI-level steps.
3. Output ONLY valid JSON.
"""

    response = client.models.generate_content(
        model="gemini-1.5-pro",
        contents=prompt,
        config={
            "temperature": 0.2,
            "response_mime_type": "application/json"
        }
    )

    return json.loads(response.text)

def write_to_excel(json_data, output_path):
    rows = []

    for scenario in json_data:
        for step in scenario["steps"]:
            rows.append({
                "Scenario": scenario["scenario"],
                "Precondition": scenario["precondition"],
                "User Role": scenario.get("user_role", ""),
                "Step Number": step["step_number"],
                "Test Step Action": step["action"],
                "Expected Result": step["expected_result"],
                "Example Record For Testing": step["example_record"],
                "Notes": step["notes"]
            })

    pd.DataFrame(rows).to_excel(output_path, index=False)
