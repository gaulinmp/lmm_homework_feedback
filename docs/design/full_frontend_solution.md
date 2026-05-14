# Design Document: Full Frontend Solution

## 1. Architecture Overview
This document outlines a pure frontend, zero-backend architecture for the Socratic homework tutor. The goal is to maximize ease of deployment, minimize maintenance costs, and ensure strict FERPA compliance by keeping student data localized to their browser until submission to the Learning Management System (LMS).

- **Core Paradigm:** The application is a static site (HTML, JS, CSS). All student interaction runs client-side, making calls to a central "question" server, and LLM calls to the Gemini API.
- **State Orchestration:** `LangGraph.js` is used within the browser to manage the Socratic grading loop state machine.
- **Hosting:** The built static assets will be hosted on a free static provider (e.g., GitHub Pages).
- **Question Server:** A central "question" server is used to provide questions to the student via API, providing questions, and a "hint" / "solution" rubric for the Socratic loop grader.


## 2. LLM & API Strategy (BYOK)
To eliminate backend API key proxying, the app uses a **Bring Your Own Key (BYOK)** model.

- **Provider:** Google Gemini API.
- **Key Distribution:** Students will use their educational credentials to access the Google AI Studio and generate their own free-tier Gemini API keys (which often include Pro model access for students). The key is stored purely in the browser's `localStorage`.
- **Pedagogical Gating via Rate Limits:** The free tier of Gemini comes with strict rate limits (e.g., 15 RPM, 1500 RPD). Rather than viewing this as a limitation, we treat it as a pedagogical feature. 
  - Rapid-fire guessing by the student will trigger a `429 Too Many Requests` error.
  - The UI will gracefully catch this error and display a message encouraging the student to take a break, review their notes, and think carefully before their next attempt. This naturally prevents students from trying to "brute force" the Socratic tutor.


## 3. State Management & Persistence
Because the learning process is non-linear (students may need to read a question, leave the app to build a graph in Excel, and return hours later), robust local state management is critical.

- **Local Storage Engine:** We will utilize the browser's `localStorage` (or `IndexedDB` if payload sizes grow large) to persist the active session.
- **Question State (Primary Persistence):** When a dynamic question is generated (e.g., randomized numbers or dataset columns), the exact parameters and the unique question ID are immediately serialized to local storage. When a student re-opens the app, they will be presented with the exact same question they left, preventing the tutor from dynamically changing the requirements midway through their work.
- **Chat History (Secondary Persistence):** The ongoing Socratic back-and-forth transcript will also be continuously saved to local storage. This allows students to pick up the conversation exactly where they left off.
- **"Start Over" Capability:** The UI will feature a prominent "Reset Chat" button. Clicking this will wipe the Socratic chat history from local storage but *retain the original question parameters*. This gives students a clean slate to try again without losing their specific assignment variant.


## 4. The Audit Trail (Receipt System)
Since there is no server side tracking to log student interactions, the burden of proof shifts to the student. We must ensure they completed the interactive loop without relying on a Micro-backend that could trigger FERPA compliance issues.

- **Submission File Mechanism:**
  1. Once the LangGraph loop evaluates a submission as `correct` consistent with question API returned rubric/requirements, a "Generate Submission Receipt" button is unlocked.
  2. The frontend gathers the full JSON transcript (the Socratic conversation, the rubric evaluation, and timestamps).
  3. The browser triggers a forced download of this json file with the extension .ada (e.g., `{username}_week-{#}_question-{#}.ada`).
  4. The student uploads this `.ada` file directly to the LMS (Canvas).
- **Tamper Resistance:** While a highly technical student could realize the `.ada` file is a json and manually edit the JSON transcript to fake a passing grade, this level of reverse-engineering demonstrates the very data literacy skills the class aims to teach. For the vast majority, the custom extension provides sufficient friction to deter casual tampering.
- **FERPA Compliance:** Because the receipt data flows directly from the student's browser to the university-approved LMS, no 3rd-party non-institutional servers are involved, ensuring strict FERPA compliance.
