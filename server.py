# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn",
# ]
# ///

import os
import re
from pathlib import Path
from pprint import pprint
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Socratic Tutor Question API")

# Configure CORS so the frontend (e.g., Vite dev server) can fetch from this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for local development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def parse_markdown_question(filepath: Path, assignment_id: int):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    question = {
        "question_id": f"{filepath.parent.name}/{filepath.stem}",
        "assignment_id": assignment_id,
        "title": "",
        "content": {
            "text": "",
            "dataset_url": "",
            "parameters": {}
        },
        "rubric": {
            "requirements": [],
            "hints": [],
            "solution_explanation": ""
        }
    }

    current_section = "text"
    text_lines = []
    sol_lines = []

    # Parse YAML-like frontmatter
    if lines and lines[0].strip() == "---":
        end_idx = -1
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx != -1:
            for i in range(1, end_idx):
                line = lines[i].strip()
                if ":" in line:
                    key, val = line.split(":", 1)
                    question[key.strip()] = val.strip()
            lines = lines[end_idx + 1:]

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_section == "text":
                text_lines.append(line)
            elif current_section == "Solution Explanation":
                sol_lines.append(line)
            continue
            
        if line.startswith("# "):
            question["title"] = stripped[2:]
        elif line.startswith("## Dataset"):
            current_section = "Dataset"
        elif line.startswith("## Parameters"):
            current_section = "Parameters"
        elif line.startswith("## Requirements"):
            current_section = "Requirements"
        elif line.startswith("## Hints"):
            current_section = "Hints"
        elif line.startswith("## Solution Explanation"):
            current_section = "Solution Explanation"
        elif line.startswith("---"):
            pass
        elif line.startswith("id: ") and not question["title"]:
            question["question_id"] = stripped[4:]
        else:
            if current_section == "text":
                text_lines.append(line)
            elif current_section == "Dataset":
                question["content"]["dataset_url"] = stripped
            elif current_section == "Parameters":
                if ":" in stripped:
                    key, val = stripped.lstrip('- ').split(":", 1)
                    key = key.strip()
                    val = val.strip()
                    try:
                        if '.' in val:
                            val = float(val)
                        else:
                            val = int(val)
                    except ValueError:
                        pass
                    question["content"]["parameters"][key] = val
            elif current_section == "Requirements":
                if stripped.startswith("- "):
                    question["rubric"]["requirements"].append(stripped[2:])
            elif current_section == "Hints":
                if stripped.startswith("- "):
                    question["rubric"]["hints"].append(stripped[2:])
            elif current_section == "Solution Explanation":
                sol_lines.append(line)

    question["content"]["text"] = "".join(text_lines).strip()
    question["rubric"]["solution_explanation"] = "".join(sol_lines).strip()
    return question

def load_questions():
    questions_db = {}
    assignments_list = []
    
    q_dir = Path("questions")
    if not q_dir.exists():
        print(f"Warning: {q_dir} directory not found.")
        return questions_db, assignments_list

    for assign_dir in sorted(q_dir.iterdir()):
        if not assign_dir.is_dir():
            continue
            
        m = re.match(r"^(\d+)-(.*)$", assign_dir.name)
        if not m:
            continue
            
        assign_id = int(m.group(1))
        assign_title = m.group(2).replace("_", " ")
        
        assignment = {
            "assignment_id": assign_id,
            "title": f"Week {assign_id} - {assign_title}",
            "questions": []
        }
        
        for q_file in sorted(assign_dir.glob("*.md")):
            q_data = parse_markdown_question(q_file, assign_id)
            questions_db[q_data["question_id"]] = q_data
            assignment["questions"].append({
                "question_id": q_data["question_id"],
                "title": q_data["title"]
            })
            
        assignments_list.append(assignment)
        
    return questions_db, assignments_list

QUESTIONS, ASSIGNMENTS = load_questions()

@app.get("/api/assignments")
def list_assignments():
    """List all assignments and their questions."""
    return ASSIGNMENTS

@app.get("/api/questions")
def list_questions():
    """List all available questions."""
    return [{"question_id": q_id, "assignment_id": q.get("assignment_id"), "title": q["title"]} for q_id, q in QUESTIONS.items()]

@app.get("/api/questions/{question_id}")
def get_question(question_id: str):
    """Get a specific question by ID."""
    if question_id not in QUESTIONS:
        raise HTTPException(status_code=404, detail="Question not found")
    return QUESTIONS[question_id]

if __name__ == "__main__":
    print("Starting Socratic Tutor Question API on http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
