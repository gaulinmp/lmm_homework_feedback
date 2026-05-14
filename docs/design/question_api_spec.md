# Design Document: Question & Rubric API Specification

## 1. Overview
This document specifies the format and requirements for the Question API. The Socratic Homework Tutor frontend calls this API to fetch a specific question variant and its associated grading rubric. This data initializes the client-side `LangGraph.js` state machine to evaluate student submissions securely and entirely within the browser, without requiring backend processing of student responses.

## 2. Endpoint Structure

**Endpoint:** `GET /api/questions/{question_id}` (or an equivalent static URL if pre-rendered)

- **Method:** `GET`
- **Headers:** Configurable based on deployment (e.g., public, or using a simple institutional token if needed).
- **Path Parameters:**
  - `question_id`: The unique identifier for the question (e.g., `q_linear_regression_01`).

## 3. Response Payload Format

The API must return a JSON response containing the question prompt, any dynamically generated parameters (if applicable), and the strict rubric the LLM will use to govern the Socratic grading loop.

### 3.1. JSON Schema

```json
{
  "question_id": "string",
  "title": "string",
  "content": {
    "text": "string (Markdown format supported)",
    "dataset_url": "string (Optional URL to a CSV/JSON file for the student to use)",
    "parameters": {
      "[key: string]": "any (Dynamic values or seeds injected into the question to ensure uniqueness per student)"
    }
  },
  "rubric": {
    "requirements": [
      "string (A list of explicit criteria the LLM must verify to consider the submission correct)"
    ],
    "hints": [
      "string (An ordered list of hints the LLM can reveal progressively if the student is struggling)"
    ],
    "solution_explanation": "string (The final explanation/answer provided to the student *only* after successful completion)"
  }
}
```

### 3.2. Example Response

```json
{
  "question_id": "q_week3_linear_regression",
  "title": "Week 3: Linear Regression Analysis",
  "content": {
    "text": "Using the provided dataset, create a line plot of **Book Value of Equity** and **Market Value of Equity** over time (years). When you are ready, submit your graph as an image (png or jpg).",
    "dataset_url": "https://example-lms.edu/datasets/advertising_data.csv",
    "parameters": {
        "random_seed": 42,
        "market_value_max": 3_500_000_000_000_000,
        "book_value_max": 300_000_000_000
    }
  },
  "rubric": {
    "requirements": [
        "The submitted image must show a clear line plot with two lines.",
        "The x-axis must be labeled Value or equivalent.",
        "The y-axis must be labeled Year or Time or equivalent.",
        "There should be a legend clearly identifying the two lines."
    ],
    "hints": [
        "Have you checked if your axes are labeled correctly?",
        "Have you properly scaled your data to reflect the correct units & values?",
        "Have you included a legend or other clear identification of both lines?"
    ],
    "solution_explanation": "A correct line graph clearly shows the positive trend of both Book Value of Equity and Market Value of Equity over time, with Market Value of Equity growing significantly faster than Book Value of Equity."
}
```

## 4. Usage in the Frontend Loop

1. **Initialization:** The frontend fetches this JSON on load.
2. **Persistence:** The JSON is immediately saved to `localStorage`. If the user refreshes, the active parameters and rubric persist.
3. **Prompt Injection:** The `rubric.requirements` and `rubric.hints` are injected into the system prompt for the `LangGraph` LLM node. The LLM acts as the evaluator, comparing the student's submission against the explicitly defined requirements.
4. **Completion:** When the LLM evaluates that all requirements are met, it sets the internal graph state to `completed`, displaying the `solution_explanation` and unlocking the submission receipt download.
