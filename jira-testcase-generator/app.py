from fastapi import FastAPI, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from generator import generate_test_cases, write_to_excel

app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/generate")
def generate(
    jira_description: str = Form(...),
    acceptance_criteria: str = Form(...)
):
    jira_text = f"""
DESCRIPTION:
{jira_description}

ACCEPTANCE CRITERIA:
{acceptance_criteria}
"""

    json_data = generate_test_cases(jira_text)

    output_path = "output/Generated_TestCases.xlsx"
    write_to_excel(json_data, output_path)

    return FileResponse(
        output_path,
        filename="Generated_TestCases.xlsx"
    )
