import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

load_dotenv()

print("\n--- Testing OpenAI ---")
try:
    openai_llm = ChatOpenAI(model="gpt-4o-mini")
    print(openai_llm.invoke("Hello from OpenAI").content)
except Exception as e:
    print("OpenAI Error:", e)

print("\n--- Testing Claude ---")
try:
    claude_llm = ChatAnthropic(model="claude-3-haiku-20240307")
    print(claude_llm.invoke("Hello from Claude").content)
except Exception as e:
    print("Claude Error:", e)

print("\n--- Gemini Test Skipped ---")
print("Gemini disabled temporarily because google-ai-generativelanguage 0.4.0 is incompatible with langchain-google-genai 3.0.3.")
