import { ChatGoogleGenerativeAI } from '@langchain/google-genai';
import { StateGraph, END, START } from '@langchain/langgraph';
import { HumanMessage, AIMessage, SystemMessage } from '@langchain/core/messages';
import { getTutorPrompt, getEvaluatorPrompt, getDonePrompt } from './prompts.js';
import { IndexedDBMemorySaver } from './indexedDbSaver.js';


// --- UI Elements ---
const apiKeyInput = document.getElementById('api-key-input');
const apiUrlInput = document.getElementById('api-url-input');
const configAccordion = document.getElementById('config-accordion');
const configHeader = document.getElementById('config-header');
const saveKeyBtn = document.getElementById('save-key-btn');
const chatMessages = document.getElementById('chat-messages');
const chatInput = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');
const fileUpload = document.getElementById('file-upload');
const fileUploadLabel = document.getElementById('file-upload-label');
const downloadReceiptBtn = document.getElementById('download-receipt-btn');
const clearChatBtn = document.getElementById('clear-chat-btn');
const confirmationModal = document.getElementById('confirmation-modal');
const modalCancelBtn = document.getElementById('modal-cancel-btn');
const modalConfirmBtn = document.getElementById('modal-confirm-btn');
const sidebarToggleBtn = document.getElementById('sidebar-toggle-btn');
const sidebar = document.querySelector('.sidebar');
const pasteWarningModal = document.getElementById('paste-warning-modal');
const pasteModalCloseBtn = document.getElementById('paste-modal-close-btn');

// --- State Variables ---
let geminiApiKey = localStorage.getItem('socratic_gemini_api_key') || '';
let serverUrl = localStorage.getItem('socratic_server_url') || '.';
let currentQuestion = null;
let currentImageBase64 = null;
let graphApp = null;
let threadId = null; // Will be set per question


// Restore Configuration
if (geminiApiKey) {
  apiKeyInput.value = geminiApiKey;
  enableChat();
}
if (serverUrl && apiUrlInput) {
  apiUrlInput.value = serverUrl;
}

saveKeyBtn.addEventListener('click', () => {
  geminiApiKey = apiKeyInput.value.trim();
  const newServerUrl = apiUrlInput.value.trim() || '.';
  const urlChanged = newServerUrl !== serverUrl;
  serverUrl = newServerUrl;

  if (geminiApiKey) {
    localStorage.setItem('socratic_gemini_api_key', geminiApiKey);
    localStorage.setItem('socratic_server_url', serverUrl);
    enableChat();
    appendMessage('system', 'Configuration saved. You can now start the assignment.');

    // Close accordion
    if (configAccordion) {
      configAccordion.classList.remove('active');
    }

    if (urlChanged) {
      fetchAssignments()
        .then(renderAssignments)
        .catch(e => {
          document.getElementById('assignments-tree').innerHTML = '<p class="loading-text" style="color: var(--danger)">Failed to load assignments from new URL</p>';
        });
    }
  }
});

if (configHeader && configAccordion) {
  configHeader.addEventListener('click', () => {
    configAccordion.classList.toggle('active');
  });
}

sidebarToggleBtn.addEventListener('click', () => {
  if (sidebar) sidebar.classList.toggle('collapsed');
});

function updateProgressCounts() {
  const accordionItems = document.querySelectorAll('#assignments-tree .accordion-item');
  accordionItems.forEach(item => {
    const body = item.querySelector('.accordion-body');
    if (!body) return;

    const questions = body.querySelectorAll('.question-btn');
    if (questions.length === 0) return;

    const total = questions.length;
    let completed = 0;
    questions.forEach(q => {
      if (q.textContent.includes('✅')) {
        completed++;
      }
    });

    const titleSpan = item.querySelector('.accordion-header span:first-child');
    if (titleSpan && titleSpan.dataset.baseTitle) {
      titleSpan.textContent = `${titleSpan.dataset.baseTitle} (${completed}/${total})`;
    }
  });
}

function markQuestionAsCompleteInUI(questionId) {
  const btn = document.querySelector(`.question-btn[data-question-id="${questionId}"]`);
  if (btn && !btn.textContent.includes('✅')) {
    btn.textContent = btn.textContent + ' ✅ 💯';
    updateProgressCounts();
  }
}

function unmarkQuestionAsCompleteInUI(questionId) {
  const btn = document.querySelector(`.question-btn[data-question-id="${questionId}"]`);
  if (btn && btn.textContent.includes('✅')) {
    btn.textContent = btn.textContent.replace(' ✅ 💯', '');
    updateProgressCounts();
  }
}

clearChatBtn.addEventListener('click', () => {
  confirmationModal.style.display = 'flex';
});

modalCancelBtn.addEventListener('click', () => {
  confirmationModal.style.display = 'none';
});

modalConfirmBtn.addEventListener('click', () => {
  confirmationModal.style.display = 'none';
  localStorage.removeItem('socratic_chat_history');
  chatMessages.innerHTML = '';

  if (graphApp && threadId) {
    const memory = graphApp.checkpointer;
    if (memory && typeof memory.deleteThread === 'function') {
      memory.deleteThread(threadId);
    }
  }

  threadId = 'socratic_session_' + Date.now();
  if (currentQuestion) {
    localStorage.removeItem(`socratic_complete_${currentQuestion.question_id}`);
    unmarkQuestionAsCompleteInUI(currentQuestion.question_id);
    localStorage.setItem(`socratic_thread_${currentQuestion.question_id}`, threadId);
    const questionText = `**${currentQuestion.title}**\n\n${currentQuestion.content.text}`;
    appendMessage('system', questionText);
  }

  // Re-init graph so memory is fresh
  graphApp = null;
  initLangGraph();
});

fileUpload.addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (file) {
    const reader = new FileReader();
    reader.onload = (event) => {
      currentImageBase64 = event.target.result;
      document.getElementById('file-upload-text').textContent = `📎 Image Attached`;
      fileUploadLabel.style.color = 'var(--accent-primary)';
    };
    reader.readAsDataURL(file);
  }
});

function enableChat() {
  chatInput.disabled = false;
  sendBtn.disabled = false;
  fileUpload.disabled = false;
  fileUploadLabel.removeAttribute('disabled');
  if (currentQuestion && !graphApp && geminiApiKey) {
    initLangGraph();
  }
}

// --- Anti-Paste Logic ---
let pasteAttemptCount = 0;

chatInput.addEventListener('paste', (e) => {
  e.preventDefault();
  pasteAttemptCount++;

  if (pasteAttemptCount >= 3) {
    pasteWarningModal.style.display = 'flex';
    pasteAttemptCount = 0; // Reset after showing
  }
});

chatInput.addEventListener('input', () => {
  // Reset on actual manual typing
  pasteAttemptCount = 0;
});

pasteModalCloseBtn.addEventListener('click', () => {
  pasteWarningModal.style.display = 'none';
  chatInput.focus();
});

// --- API Fetch ---
let questionsDbCache = null;

async function fetchDb() {
  if (!questionsDbCache) {
    const url = serverUrl.endsWith('/') ? `${serverUrl}questions.json` : `${serverUrl}/questions.json`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`API Error: ${response.status}`);
    questionsDbCache = await response.json();
  }
  return questionsDbCache;
}

async function fetchAssignments() {
  const db = await fetchDb();
  return db.assignments;
}

function renderAssignments(assignments) {
  const treeContainer = document.getElementById('assignments-tree');
  treeContainer.innerHTML = '<div class="accordion"></div>';
  const accordion = treeContainer.querySelector('.accordion');

  assignments.forEach((assignment, index) => {
    const item = document.createElement('div');
    item.className = 'accordion-item';
    let isActive = false;

    if (localStorage.getItem('socratic_last_expanded_week') == assignment.assignment_id) {
      isActive = true;
    }

    const totalQuestions = assignment.questions.length;
    let completedCount = 0;
    assignment.questions.forEach(q => {
      if (localStorage.getItem(`socratic_complete_${q.question_id}`) === 'true') {
        completedCount++;
      }
    });

    item.innerHTML = `
      <div class="accordion-header">
        <span data-base-title="${assignment.title.replace(/"/g, '&quot;')}">${assignment.title} (${completedCount}/${totalQuestions})</span>
        <span>▼</span>
      </div>
      <div class="accordion-body">
      </div>
    `;

    const body = item.querySelector('.accordion-body');
    assignment.questions.forEach(q => {
      console.log(q);
      const btn = document.createElement('button');
      btn.className = 'question-btn';
      btn.dataset.questionId = q.question_id;

      let titleText = q.title;
      if (localStorage.getItem(`socratic_complete_${q.question_id}`) === 'true') {
        titleText += ' ✅';
      }
      btn.textContent = titleText;

      if (currentQuestion && currentQuestion.question_id === q.question_id) {
        btn.classList.add('selected');
        isActive = true;
        localStorage.setItem('socratic_last_expanded_week', assignment.assignment_id);
      }
      btn.addEventListener('click', () => {
        document.querySelectorAll('.question-btn').forEach(b => b.classList.remove('selected'));
        btn.classList.add('selected');
        loadSpecificQuestion(q.question_id);
      });
      body.appendChild(btn);
    });

    if (isActive) item.classList.add('active');

    item.querySelector('.accordion-header').addEventListener('click', () => {
      item.classList.toggle('active');
      if (item.classList.contains('active')) {
        localStorage.setItem('socratic_last_expanded_week', assignment.assignment_id);
      } else if (localStorage.getItem('socratic_last_expanded_week') == assignment.assignment_id) {
        localStorage.removeItem('socratic_last_expanded_week');
      }
    });

    accordion.appendChild(item);
  });
}

async function loadSpecificQuestion(questionId) {
  localStorage.removeItem('socratic_chat_history');
  chatMessages.innerHTML = '';
  graphApp = null;
  currentQuestion = null;
  localStorage.removeItem('socratic_current_question');

  await loadQuestion(questionId);
}

async function initApp() {
  try {
    const assignments = await fetchAssignments();
    renderAssignments(assignments);
  } catch (e) {
    document.getElementById('assignments-tree').innerHTML = '<p class="loading-text" style="color: var(--danger)">Failed to load assignments</p>';
  }

  const storedQ = localStorage.getItem('socratic_current_question');
  if (storedQ) {
    loadQuestion();
  }
}

function appendMessage(role, content, imageBase64 = null) {
  const msgDiv = document.createElement('div');
  msgDiv.className = `message ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '🧑‍🎓' : '🤖';

  const contentDiv = document.createElement('div');
  contentDiv.className = 'content';

  // Basic markdown-like rendering for bold
  let htmlContent = content.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  htmlContent = htmlContent.replace(/\n/g, '<br/>');
  contentDiv.innerHTML = htmlContent;

  if (imageBase64) {
    const img = document.createElement('img');
    img.src = imageBase64;
    contentDiv.appendChild(img);
  }

  msgDiv.appendChild(avatar);
  msgDiv.appendChild(contentDiv);
  chatMessages.appendChild(msgDiv);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

// --- API Fetch Question ---
async function fetchQuestionAPI(questionId = 'q_week3_linear_regression') {
  const db = await fetchDb();
  const question = db.questions[questionId];
  if (!question) {
    throw new Error(`Question not found`);
  }
  return question;
}

async function loadQuestion(questionId = null) {
  const activeAssignmentSection = document.getElementById('active-assignment-section');
  if (activeAssignmentSection) activeAssignmentSection.style.display = 'block';

  try {
    const storedQ = localStorage.getItem('socratic_current_question');
    if (storedQ && !questionId) {
      currentQuestion = JSON.parse(storedQ);
      if (!currentQuestion || !currentQuestion.content || !currentQuestion.content.text) {
        localStorage.removeItem('socratic_current_question');
        currentQuestion = await fetchQuestionAPI(questionId || 'q_week3_linear_regression');
        localStorage.setItem('socratic_current_question', JSON.stringify(currentQuestion));
      }
    } else {
      currentQuestion = await fetchQuestionAPI(questionId || 'q_week3_linear_regression');
      localStorage.setItem('socratic_current_question', JSON.stringify(currentQuestion));
    }

    chatMessages.innerHTML = '';
    const questionText = `**${currentQuestion.title}**\n\n${currentQuestion.content.text}`;
    appendMessage('system', questionText);

    if (geminiApiKey && !graphApp) {
      threadId = localStorage.getItem(`socratic_thread_${currentQuestion.question_id}`);
      if (!threadId) {
        threadId = 'socratic_session_' + Date.now();
        localStorage.setItem(`socratic_thread_${currentQuestion.question_id}`, threadId);
      }
      initLangGraph();
    }
  } catch (err) {
    console.error("Failed to load question:", err);
    appendMessage('system', `Error loading question: ${err.message}`);
  }
}

// --- LangGraph Setup ---
async function initLangGraph() {
  // Define State
  const graphState = {
    messages: {
      value: (x, y) => x.concat(y),
      default: () => [],
    },
    isComplete: {
      value: (x, y) => y,
      default: () => false,
    }
  };

  const model = new ChatGoogleGenerativeAI({
    model: "gemini-3-flash-preview", // Use a fast model
    maxOutputTokens: 2048,
    apiKey: geminiApiKey
  });

  // Define Nodes
  const evaluatorNode = async (state) => {
    const sysPrompt = getEvaluatorPrompt(currentQuestion);
    const messages = [new SystemMessage(sysPrompt), ...state.messages];

    try {
      const response = await model.invoke(messages);
      const content = response.content.trim().toUpperCase();

      const isComplete = content.includes("YES");

      return {
        isComplete: isComplete
      };
    } catch (error) {
      console.error("Evaluator error:", error);
      return { isComplete: false };
    }
  };

  const tutorNode = async (state) => {
    const sysPrompt = getTutorPrompt(currentQuestion);

    const messages = [new SystemMessage(sysPrompt), ...state.messages];

    try {
      const response = await model.invoke(messages);
      return { messages: [new AIMessage(response.content)], isComplete: false };
    } catch (error) {
      if (error.message.includes("429")) {
        return { messages: [new AIMessage("Whoa there! We've hit a rate limit, the AI is tired. Take a mental health break, move around, and try again in a bit.")] };
      }
      return { messages: [new AIMessage("I encountered an error connecting to my brain (API). Please check your key or try again later.")] };
    }
  };

  const doneNode = async (state) => {
    const sysPrompt = getDonePrompt(currentQuestion);

    const messages = [new SystemMessage(sysPrompt), ...state.messages];

    try {
      const response = await model.invoke(messages);
      return { messages: [new AIMessage(response.content)], isComplete: true };
    } catch (error) {
      return { messages: [new AIMessage("Great job! You've completed the assignment.")], isComplete: true };
    }
  };

  const shouldContinue = (state) => {
    if (state.isComplete) {
      return "done";
    }
    return "tutor";
  };

  // Build Graph
  const workflow = new StateGraph({ channels: graphState })
    .addNode("evaluator", evaluatorNode)
    .addNode("tutor", tutorNode)
    .addNode("done", doneNode)
    .addEdge(START, "evaluator")
    .addConditionalEdges("evaluator", shouldContinue, {
      done: "done",
      tutor: "tutor"
    })
    .addEdge("tutor", END)
    .addEdge("done", END);

  const memory = new IndexedDBMemorySaver();
  graphApp = workflow.compile({ checkpointer: memory });

  // Load history if any from graph state
  const config = { configurable: { thread_id: threadId } };
  const currentState = await graphApp.getState(config);

  let isThreadComplete = false;
  if (currentState && currentState.values && currentState.values.isComplete) {
    isThreadComplete = true;
  }

  if (isThreadComplete) {
    chatInput.disabled = true;
    sendBtn.disabled = true;
    chatInput.placeholder = "Session completed.";
    downloadReceiptBtn.disabled = false;
    localStorage.setItem(`socratic_complete_${currentQuestion.question_id}`, 'true');
    markQuestionAsCompleteInUI(currentQuestion.question_id);
  } else {
    chatInput.disabled = false;
    sendBtn.disabled = false;
    chatInput.placeholder = "Type your response or question here...";
    downloadReceiptBtn.disabled = true;
  }

  if (currentState && currentState.values && currentState.values.messages && currentState.values.messages.length > 0) {
    for (const msg of currentState.values.messages) {
      if (msg._getType() === 'system') continue;
      const role = msg._getType() === 'human' ? 'user' : 'tutor';

      // Attempt to extract image url for re-render if it was sent as an array
      let content = msg.content;
      let imageBase64 = null;
      if (Array.isArray(content)) {
        const textPart = content.find(p => p.type === 'text');
        const imgPart = content.find(p => p.type === 'image_url');
        content = textPart ? textPart.text : '';
        if (imgPart && imgPart.image_url) {
          imageBase64 = typeof imgPart.image_url === 'string' ? imgPart.image_url : imgPart.image_url.url;
        }
      }
      appendMessage(role, content, imageBase64);
    }
  }
}

// --- Interaction ---
sendBtn.addEventListener('click', async () => {
  const text = chatInput.value.trim();
  if (!text && !currentImageBase64) return;

  chatInput.value = '';
  chatInput.disabled = true;
  sendBtn.disabled = true;

  appendMessage('user', text, currentImageBase64);

  // Construct message format for multimodal
  let msgContent = text;
  if (currentImageBase64) {
    msgContent = [
      { type: "text", text: text || "Here is my submission." },
      { type: "image_url", image_url: currentImageBase64 }
    ];
  }

  const humanMsg = new HumanMessage({ content: msgContent });

  // Clear image upload state
  currentImageBase64 = null;
  fileUpload.value = '';
  const fileUploadText = document.getElementById('file-upload-text');
  if (fileUploadText) fileUploadText.textContent = `📎 Attach Image`;
  fileUploadLabel.style.color = '';

  // Show typing
  const typingDiv = document.createElement('div');
  typingDiv.className = 'message system typing-indicator-container';
  typingDiv.innerHTML = `<div class="avatar">🤖</div><div class="content"><div class="typing-indicator"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div></div>`;
  chatMessages.appendChild(typingDiv);
  chatMessages.scrollTop = chatMessages.scrollHeight;

  try {
    const config = { configurable: { thread_id: threadId } };
    const result = await graphApp.invoke({ messages: [humanMsg] }, config);

    // Remove typing
    chatMessages.removeChild(typingDiv);

    const latestMsg = result.messages[result.messages.length - 1];
    appendMessage('system', latestMsg.content);

    // Persist basic chat for receipt
    let chatHistory = JSON.parse(localStorage.getItem('socratic_chat_history') || '[]');
    chatHistory.push({ role: 'user', content: text, timestamp: new Date().toISOString() });
    chatHistory.push({ role: 'tutor', content: latestMsg.content, timestamp: new Date().toISOString() });
    localStorage.setItem('socratic_chat_history', JSON.stringify(chatHistory));

    if (result.isComplete) {
      downloadReceiptBtn.disabled = false;
      appendMessage('system', '**Assignment Complete!** You may now download your submission receipt.');
      chatInput.placeholder = "Session completed.";
      localStorage.setItem(`socratic_complete_${currentQuestion.question_id}`, 'true');
      markQuestionAsCompleteInUI(currentQuestion.question_id);
    } else {
      chatInput.disabled = false;
      sendBtn.disabled = false;
      chatInput.focus();
    }
  } catch (error) {
    chatMessages.removeChild(typingDiv);
    appendMessage('system', 'Error processing your message.');
    chatInput.disabled = false;
    sendBtn.disabled = false;
  }
});

downloadReceiptBtn.addEventListener('click', () => {
  const history = JSON.parse(localStorage.getItem('socratic_chat_history') || '[]');
  const receipt = {
    student_id: "local_browser_session",
    question_id: currentQuestion.question_id,
    completion_time: new Date().toISOString(),
    transcript: history
  };

  const blob = new Blob([JSON.stringify(receipt, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `submission_${currentQuestion.question_id}.ada`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
});

// Initialize the application
initApp();
