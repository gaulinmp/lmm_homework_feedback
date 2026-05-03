from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

llm = ChatOpenAI(
    model="qwen3",           # llama-server ignores this, but it must be non-empty
    base_url="http://127.0.0.1:8080/v1",
    api_key="not-needed",    # llama-server doesn't require auth
    temperature=0.0,
)

messages = [
    SystemMessage("You are a helpful homework tutor. Be concise."),
    HumanMessage("What is the quadratic formula, and when do you use it?"),
]

print("Sending request to local LLM...\n")
response = llm.invoke(messages)
print(response.content)
