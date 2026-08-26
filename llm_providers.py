import os
import openai
import google.generativeai as genai
import anthropic
from dotenv import load_dotenv

load_dotenv()

# Load keys from env
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Model names from env
OPENAI_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp")

# Set API keys
openai.api_key = OPENAI_API_KEY
genai.configure(api_key=GOOGLE_API_KEY)

# ------------ Claude Client ------------
_claude_client = None

def get_claude():
    global _claude_client
    if _claude_client is None:
        if not ANTHROPIC_API_KEY:
            raise ValueError("Missing ANTHROPIC_API_KEY in environment")
        _claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _claude_client


# ------------ OpenAI call ------------
def call_openai(prompt, temperature=0.2):
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature
        )
        return resp.choices[0].message.content
    except Exception as e:
        print(f"OpenAI error: {e}")
        raise e


# ------------ Gemini call ------------
def call_gemini(prompt, temperature=0.2):
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        resp = model.generate_content(prompt)
        return resp.text
    except Exception as e:
        print(f"Gemini error: {e}")
        raise e


# ------------ Claude call ------------
def call_claude(prompt, temperature=0.2):
    try:
        client = get_claude()
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            temperature=temperature,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        return resp.content[0].text
    except Exception as e:
        print(f"Claude error: {e}")
        raise e


# ---------------------------------------------------------
# MAIN ENTRYPOINT: generate the final answer with fallback
# ---------------------------------------------------------
def generate_answer_with_fallback(query, hits):
    """
    Generate answer with fallback logic: OpenAI -> Claude -> Gemini
    Returns tuple: (answer, model_used)
    """
    context = "\n\n".join([h["text"] for h in hits])

    prompt = f"""You are Guru, a medical AI assistant for Khan Institute. Answer questions based on the provided context about LASIK surgery and eye care services.

Guidelines:
- Answer based on the context provided
- If the context doesn't contain enough information, say "I don't have enough information about that in my knowledge base"
- Be helpful, professional, and medically accurate
- Keep answers concise but comprehensive
- For medical advice, always recommend consulting with a qualified physician

Question: {query}

Context:
{context}

Answer:"""

    # Try OpenAI first
    try:
        answer = call_openai(prompt)
        return answer, "openai"
    except Exception as e:
        print(f"OpenAI failed, trying Claude: {e}")

    # Fallback to Claude
    try:
        answer = call_claude(prompt)
        return answer, "claude"
    except Exception as e:
        print(f"Claude failed, trying Gemini: {e}")

    # Final fallback to Gemini
    try:
        answer = call_gemini(prompt)
        return answer, "gemini"
    except Exception as e:
        print(f"All LLM calls failed: {e}")
        return "I'm sorry, I'm currently unable to generate a response. Please try again later or contact Khanna Institute directly at (805) 327-5744.", "error"


# ---------------------------------------------------------
# LEGACY FUNCTION for backward compatibility
# ---------------------------------------------------------
def generate_answer(model_name, query, hits):
    context = "\n\n".join([h["text"] for h in hits])

    prompt = f"""You are Guru, the AI medical assistant for Khanna Vision Institute (also known as Khanna Institute). 

IMPORTANT FACTS ABOUT KHANNA INSTITUTE:
- The clinic is called "Khanna Vision Institute" or "Khanna Institute"
- Dr. Rajesh Khanna is the board-certified ophthalmologist and founder
- Khanna Institute DOES perform LASIK surgery - it's one of our primary procedures
- We also offer: SuperLASIK, SMILE laser, EVO ICL, Presbyopic Implants (PIE), Cataract Surgery, and other vision correction procedures
- We have two locations: Beverly Hills and Westlake Village, California
- Dr. Khanna has over 20 years of experience in refractive and cataract surgery

Guidelines:
- Answer based on the context provided
- If the context doesn't contain enough information, say "I don't have enough information about that in my knowledge base. Please contact Khanna Institute directly for more details."
- ALWAYS confirm that Khanna Institute performs the procedures mentioned (LASIK, SMILE, EVO ICL, etc.)
- If asked about Dr. Khanna, confirm he is the founder and lead surgeon at Khanna Vision Institute
- Be helpful, professional, and medically accurate
- Keep answers concise but comprehensive
- For medical advice, always recommend consulting with Dr. Khanna or a qualified physician
- NEVER say that Khanna Institute doesn't perform procedures we actually offer

Question: {query}

Context:
{context}

Answer:"""

    try:
        if model_name == "openai":
            return call_openai(prompt)
        elif model_name == "gemini":
            return call_gemini(prompt)
        elif model_name == "claude":
            return call_claude(prompt)
        else:
            return call_openai(prompt)
    except Exception as e:
        return f"Error generating response: {str(e)}"
