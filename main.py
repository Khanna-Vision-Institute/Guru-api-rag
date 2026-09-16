from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any, Tuple
import json
import os
import re
import httpx
import traceback
from datetime import datetime
from collections import defaultdict
import time
from utils import search_opensearch, index_document, embed_text
from llm_providers import (
    generate_answer_with_fallback,
    generate_answer_for_agent,
    generate_answer,
    PRICING_FACTS,
    DR_KHANNA_FACTS,
    CLINIC_POLICIES_FACTS,
    sanitize_guru_answer,
)
from booking_normalize import normalize_booking_payload
from agent_ecosystem import eco_turn, init_eco_session


def boost_rag_hits(query: str, hits: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Prepend official policy text so OpenSearch noise does not override biography/age rules."""
    ql = (query or "").lower()
    pre: List[Dict[str, Any]] = []
    if ("khanna" in ql or "dr." in ql or "doctor" in ql) and any(
        x in ql for x in ("practicing", "practice", "how long", "years", "experience", "career", "ophthalmology")
    ):
        pre.append({"text": DR_KHANNA_FACTS})
    if any(x in ql for x in ("lasik", "smile", "refractive", "vision correction", "appointment", "book", "consult", "schedule")) and any(
        x in ql
        for x in (
            "5 year",
            "five year",
            "five-year",
            "4 year",
            "6 year",
            "7 year",
            "child",
            "kid",
            "under 13",
            "under thirteen",
            "toddler",
            "baby",
            "preschool",
        )
    ):
        pre.append({"text": CLINIC_POLICIES_FACTS})
    return pre + (hits or [])

# Sanitize Guru responses: never return wrong/outdated phone numbers
def sanitize_phone_in_response(text: str) -> str:
    """Replace wrong phone numbers with correct office number."""
    if not text or not isinstance(text, str):
        return text
    # (310) 997-4490 and variants -> (805) 230-2126
    for wrong in ['(310) 997-4490', '310-997-4490', '310.997.4490', '3109974490']:
        text = text.replace(wrong, '(805) 230-2126')
    return text
from ingest import scrape, split
from dotenv import load_dotenv

load_dotenv()
import lacs_consumer  # LACS approved-Q&A consumer (remediation 2026-09-15)

app = FastAPI(title="Guru AI RAG API", version="1.0.0", description="Medical AI assistant with RAG capabilities")

# Deduplication: Track processed tool calls to prevent duplicate bookings
processed_tool_calls = set()

# ── CORS: only allow requests from our own site ──────────────────────────────
ALLOWED_ORIGINS = [
    "https://khannainstitute.com",
    "https://www.khannainstitute.com",
    "http://localhost:3000",
    "http://localhost:8080",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8080",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# ---- Guru access control (remediation 2026-09-15) ----
import hmac as _hmac
from fastapi.responses import JSONResponse as _ACJSONResponse

_GURU_ADMIN_KEY = os.getenv("GURU_ADMIN_KEY", "").strip()
_GURU_READONLY_KEYS = [k.strip() for k in os.getenv("GURU_READONLY_KEYS", "").split(",") if k.strip()]
_GURU_WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
_GURU_ENFORCE_TOOL_SECRET = os.getenv("GURU_ENFORCE_TOOL_SECRET", "false").lower() == "true"
_AC_ADMIN_PATHS = ("/logs", "/docs", "/openapi.json", "/redoc", "/ingest-website", "/embed")
_AC_READ_PATHS = ("/ask", "/search")


def _ac_key_ok(given: str, allowed) -> bool:
    given = given or ""
    return any(k and _hmac.compare_digest(given, k) for k in allowed)


@app.middleware("http")
async def guru_access_control(request: Request, call_next):
    path = request.url.path
    key = request.headers.get("x-guru-key", "")
    if path.startswith(_AC_ADMIN_PATHS):
        if not _ac_key_ok(key, [_GURU_ADMIN_KEY]):
            return _ACJSONResponse({"detail": "Not Found"}, status_code=404)
    elif path.startswith(_AC_READ_PATHS):
        if not _ac_key_ok(key, _GURU_READONLY_KEYS + [_GURU_ADMIN_KEY]):
            return _ACJSONResponse({"detail": "Unauthorized"}, status_code=401)
    elif path.startswith("/vapi/tool/") and not path.endswith("/health"):
        secret = request.headers.get("x-vapi-secret", "")
        if not (_GURU_WEBHOOK_SECRET and _hmac.compare_digest(secret, _GURU_WEBHOOK_SECRET)):
            print(f"[ACCESS] tool call without valid x-vapi-secret on {path} (enforce={_GURU_ENFORCE_TOOL_SECRET})")
            if _GURU_ENFORCE_TOOL_SECRET:
                return _ACJSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)
# ---- end Guru access control ----


# ── Rate limiting (in-memory, per IP) ───────────────────────────────────────
# Allows 20 messages/minute and 200 messages/hour per IP
_rate_buckets: Dict[str, Dict] = defaultdict(lambda: {
    "min_count": 0, "min_ts": 0.0,
    "hour_count": 0, "hour_ts": 0.0,
})
RATE_LIMIT_PER_MINUTE = 20
RATE_LIMIT_PER_HOUR   = 200

def check_rate_limit(ip: str) -> Optional[str]:
    now = time.time()
    b = _rate_buckets[ip]
    # reset minute bucket
    if now - b["min_ts"] > 60:
        b["min_count"] = 0
        b["min_ts"] = now
    # reset hour bucket
    if now - b["hour_ts"] > 3600:
        b["hour_count"] = 0
        b["hour_ts"] = now
    b["min_count"]  += 1
    b["hour_count"] += 1
    if b["min_count"] > RATE_LIMIT_PER_MINUTE:
        return "Too many messages — please wait a moment before sending again."
    if b["hour_count"] > RATE_LIMIT_PER_HOUR:
        return "Hourly message limit reached. Please call us at (805) 230-2126."
    return None

# ── Input sanitisation ───────────────────────────────────────────────────────
MAX_MESSAGE_LENGTH = 800  # characters

# Common prompt-injection patterns to detect and neutralise
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions",
    r"forget\s+(all\s+)?(previous|prior|above|earlier)",
    r"you\s+are\s+now\s+(a\s+)?(?!brandi|guru|max|lucy|rose|kate|sage|buffett|barbie)",
    r"act\s+as\s+(a\s+)?(?!brandi|guru|max|lucy|rose|kate|sage|buffett|barbie)",
    r"system\s*:\s*you",
    r"<\s*system\s*>",
    r"\[system\]",
    r"jailbreak",
    r"do\s+anything\s+now",
    r"dan\s+mode",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

def sanitize_input(text: str) -> tuple[str, bool]:
    """Returns (cleaned_text, was_injected). Strips injection attempts."""
    if not text:
        return "", False
    text = text.strip()[:MAX_MESSAGE_LENGTH]
    if _INJECTION_RE.search(text):
        return "Tell me about your vision correction options.", True
    return text, False

# ---------- Request/Response Models ----------
class AskRequest(BaseModel):
    query: str
    top_k: Optional[int] = 5

class EmbedRequest(BaseModel):
    text: str

class SearchRequest(BaseModel):
    query: str
    top_k: Optional[int] = 5
    model: Optional[str] = "openai"  # For legacy compatibility

class AskResponse(BaseModel):
    answer: str
    model_used: str
    hits: List[Dict[str, Any]]
    timestamp: str

class SearchResponse(BaseModel):
    hits: List[Dict[str, Any]]
    timestamp: str

class EmbedResponse(BaseModel):
    success: bool
    document_id: Optional[str]
    timestamp: str

class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str

# Vapi-specific models
class VapiChatRequest(BaseModel):
    session_id: str
    query: str

class VapiChatResponse(BaseModel):
    answer: str

# ---------- Booking State Management ----------
# In-memory storage for booking sessions (in production, use Redis or database)
booking_sessions: Dict[str, Dict[str, Any]] = {}

# Multi-agent (Brandi + specialists) session state keyed by Vapi/web call id
ecosystem_sessions: Dict[str, Dict[str, Any]] = {}

def detect_booking_intent(message: str) -> bool:
    """Detect if user wants to book an appointment — requires explicit booking action words."""
    t = message.lower()
    # Must contain a clear booking action word
    booking_actions = [
        'book an appointment', 'book a consultation', 'book me', 'book now',
        'schedule an appointment', 'schedule a consultation', 'schedule me',
        'make an appointment', 'set up an appointment', 'set up a consultation',
        'want to book', 'need to book', 'want to schedule', 'need to schedule',
        'can i book', 'can i schedule', 'i want to book', 'i need to book',
        'i want to schedule', 'i need to schedule',
        'i need an appointment', 'i need a consultation',
        'available dates', 'available times', 'when can i come in',
        'reserve a spot', 'reserve an appointment',
    ]
    return any(kw in t for kw in booking_actions)

_NAME_SKIP_WORDS = {
    'please', 'book', 'an', 'appointment', 'schedule', 'consultation',
    'help', 'want', 'need', 'i', 'would', 'like', 'to', 'a', 'the',
    'can', 'you', 'with', 'me', 'my', 'for', 'how', 'what', 'when',
    'yes', 'no', 'okay', 'ok', 'sure', 'thanks', 'hello', 'hi', 'hey',
}

def extract_name(message: str) -> Optional[str]:
    """Extract patient name — rejects booking-intent phrases."""
    # Explicit name patterns
    patterns = [
        r"(?:my name is|i'?m called|call me|this is|i am|i'?m)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message.strip(), re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            words = name.lower().split()
            if not any(w in _NAME_SKIP_WORDS for w in words):
                return name.title()

    # Bare 1–2 word reply that looks like a proper name (no skip words)
    words = message.strip().split()
    if 1 <= len(words) <= 2 and all(re.match(r'^[A-Za-z]+$', w) for w in words):
        if not any(w.lower() in _NAME_SKIP_WORDS for w in words):
            return message.strip().title()

    return None

def extract_email(message: str) -> Optional[str]:
    """Extract email from message"""
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    match = re.search(email_pattern, message)
    return match.group(0) if match else None

def extract_phone(message: str) -> Optional[str]:
    """Extract phone number from message"""
    # Remove common words and extract digits
    cleaned = re.sub(r'[^\d\s\-\(\)\+]', '', message)
    # Look for 10+ digit phone numbers
    phone_pattern = r'[\d\s\-\(\)\+]{10,}'
    match = re.search(phone_pattern, cleaned)
    if match:
        # Clean up the phone number
        phone = re.sub(r'[^\d]', '', match.group(0))
        if len(phone) >= 10:
            return phone
    return None

def extract_age(message: str) -> Optional[int]:
    """Extract age from message"""
    patterns = [
        r"(?:i'?m|i am|age|aged)\s+(\d{1,3})\s*(?:years?|y\/o|yo|old)?",
        r"\b(\d{1,3})\s*(?:years?\s*old)\b",
        r"^\s*(\d{1,3})\s*$",   # bare number on its own line — treat as age
    ]
    for pattern in patterns:
        match = re.search(pattern, message.strip(), re.IGNORECASE)
        if match:
            age = int(match.group(1))
            if 1 <= age <= 120:
                return age
    return None

def extract_location(message: str) -> Optional[str]:
    """Extract location preference"""
    message_lower = message.lower()
    if 'beverly' in message_lower or 'beverly hills' in message_lower:
        return 'beverly'
    elif 'westlake' in message_lower or 'westlake village' in message_lower:
        return 'westlake'
    return None

def extract_date_time(message: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract date and time from message, returning actual matched text."""
    date = None
    time = None

    date_patterns = [
        r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}',
        # "13th May 2026", "May 13", "May 13th 2026"
        r'\d{1,2}(?:st|nd|rd|th)?\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)(?:\s*,?\s*\d{4})?',
        r'(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?(?:\s*,?\s*\d{4})?',
        r'(?:on|for|this|next)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)',
        r'(?:tomorrow|today)',
    ]

    for pattern in date_patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            date = match.group(0).strip()
            break

    # Use anchored time pattern: match full "HH:MM AM/PM" before trying bare "H AM/PM"
    # The negative lookbehind prevents matching "00 AM" inside "10:00 AM"
    time_patterns = [
        r'\b(1[0-2]|0?[1-9]):[0-5]\d\s*(?:am|pm)\b',   # 10:00 AM
        r'(?<!\d)(1[0-2]|0?[1-9])\s*(?:am|pm)\b',       # 10 AM  (not preceded by digit)
    ]

    for pattern in time_patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            time = match.group(0).strip()
            break

    return date, time

async def submit_booking(booking_data: Dict[str, Any], skip_emails: bool = False) -> Dict[str, Any]:
    """Submit booking to the booking API"""
    try:
        default_page = booking_data.get('pageUrl') or 'Web Voice Call (KVI Chat)'
        booking_data = normalize_booking_payload(
            booking_data,
            page_url=default_page,
        )
        if not booking_data.get('surgeryExamType'):
            booking_data = {**booking_data, 'surgeryExamType': 'General Consultation'}
        if not booking_data.get('pageUrl'):
            booking_data = {**booking_data, 'pageUrl': default_page}

        # The booking API endpoint (configurable via environment variable)
        booking_url = os.getenv("BOOKING_API_URL", "https://khannainstitute.com/api/booking/submit")
        
        print(f"[SUBMIT_BOOKING] Calling: {booking_url}")
        print(f"[SUBMIT_BOOKING] Data: {json.dumps(booking_data, indent=2)}")
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                booking_url,
                json=booking_data,
                headers={"Content-Type": "application/json"}
            )
            print(f"[SUBMIT_BOOKING] Response status: {response.status_code}")
            print(f"[SUBMIT_BOOKING] Response body: {response.text[:500]}")
            
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException as e:
        print(f"[SUBMIT_BOOKING] Timeout error: {e}")
        raise
    except httpx.HTTPStatusError as e:
        print(f"[SUBMIT_BOOKING] HTTP error: {e.response.status_code} - {e.response.text}")
        raise
    except Exception as e:
        print(f"[SUBMIT_BOOKING] Error: {e}")
        print(f"[SUBMIT_BOOKING] Traceback: {traceback.format_exc()}")
        raise

def get_next_booking_question(session_data: Dict[str, Any]) -> Optional[str]:
    """Determine what question to ask next for booking"""
    name = session_data.get('fullName')

    if not name:
        return "I'd be happy to help you schedule a consultation! To get started, may I have your full name?"

    if not session_data.get('age'):
        return f"Thank you, {name}! What is your age?"

    if not session_data.get('email'):
        return "What is your email address?"

    if not session_data.get('phone'):
        return "What is your phone number?"

    if not session_data.get('location'):
        return "Which location would you prefer? We have offices in Beverly Hills and Westlake Village."

    if not session_data.get('date'):
        return "What date would you like to schedule your consultation? Please provide a specific date."

    if not session_data.get('time'):
        return "What time would work best for you?"

    return None  # All information collected

# ---------- Logging ----------
LOG_FILE = "guru_logs.json"

def log_interaction(query: str, response: str, model_used: str, hits_count: int):
    """Log all interactions to JSON file"""
    try:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "query": query,
            "response": response,
            "model_used": model_used,
            "hits_count": hits_count
        }

        # Read existing logs
        logs = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                try:
                    logs = json.load(f)
                except:
                    logs = []

        # Add new log
        logs.append(log_entry)

        # Keep only last 1000 logs
        if len(logs) > 1000:
            logs = logs[-1000:]

        # Write back
        with open(LOG_FILE, 'w') as f:
            json.dump(logs, f, indent=2)

    except Exception as e:
        print(f"Logging error: {e}")

# ---------- API Endpoints ----------

@app.post("/tts")
async def text_to_speech(request: Request):
    """Convert text to speech using OpenAI TTS — used by the chat widget."""
    try:
        # Rate limit TTS same as chat
        client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
        client_ip = client_ip.split(",")[0].strip()
        rate_msg = check_rate_limit(client_ip)
        if rate_msg:
            return JSONResponse(status_code=429, content={"error": rate_msg})

        body = await request.json()
        text = (body.get("text") or "").strip()
        voice = body.get("voice", "nova")   # nova, alloy, echo, fable, onyx, shimmer

        if not text:
            return JSONResponse(status_code=400, content={"error": "No text provided"})

        # Strip markdown so TTS reads cleanly
        import re as _re
        clean = _re.sub(r'\*\*(.*?)\*\*', r'\1', text)
        clean = _re.sub(r'\*(.*?)\*',     r'\1', clean)
        clean = _re.sub(r'#+\s',          '',    clean)
        clean = _re.sub(r'https?://\S+',  'visit our website', clean)
        clean = clean[:4096]  # OpenAI TTS limit

        import openai
        client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        response = await client.audio.speech.create(
            model="tts-1",
            voice=voice,
            input=clean,
        )

        from fastapi.responses import Response as FastResponse
        return FastResponse(
            content=response.content,
            media_type="audio/mpeg",
            headers={"Cache-Control": "no-store"},
        )

    except Exception as e:
        print(f"[TTS] Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/health", response_model=HealthResponse)
def health_check():
    """Health check endpoint"""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now().isoformat(),
        version="1.0.0"
    )

@app.post("/ask", response_model=AskResponse)
def ask_guru(request: AskRequest, http_request: Request):
    """
    Main RAG endpoint - takes a query, searches vector DB, and generates answer with fallback
    """
    try:
        # ---- LACS consumer hook (/ask) ----
        _lacs = lacs_consumer.consult(request.query, "ask", privileged=lacs_consumer.is_admin(http_request))
        if _lacs is not None:
            return AskResponse(answer=_lacs["text"], model_used="lacs-approved-qa", hits=[], timestamp=datetime.now().isoformat())
        # Search for relevant documents
        hits = search_opensearch(request.query, request.top_k)
        hits = boost_rag_hits(request.query, hits)

        # For pricing/cost questions, prepend official pricing + contact facts
        pricing_keywords = ['cost', 'price', 'pricing', 'how much', 'fee', 'charge', 'exam cost', 'consultation cost']
        contact_keywords = ['phone number', 'phone', 'contact number', 'call us', 'our number', 'address', 'westlake', 'location', 'reach us']
        if any(kw in request.query.lower() for kw in pricing_keywords) or any(kw in request.query.lower() for kw in contact_keywords):
            hits = [{"text": PRICING_FACTS}] + (hits or [])

        if not hits:
            # Even with no hits, provide helpful response about Khanna Institute
            response = "I don't have enough specific information about that in my knowledge base. However, I can tell you that Khanna Vision Institute, led by Dr. Rajesh Khanna, offers comprehensive vision correction services including LASIK, SMILE laser, EVO ICL, and other advanced procedures. Please contact us directly at (805) 230-2126 or visit https://khannainstitute.com/contact/schedule-consultation/ for more information."
            model_used = "none"
        else:
            # Generate answer with fallback logic
            response, model_used = generate_answer_with_fallback(request.query, hits)

        # Sanitize: enforce Dr. Khanna years; never return wrong phone numbers
        response = sanitize_guru_answer(response, request.query)
        response = sanitize_phone_in_response(response)

        # Log the interaction
        log_interaction(request.query, response, model_used, len(hits))

        return AskResponse(
            answer=response,
            model_used=model_used,
            hits=hits,
            timestamp=datetime.now().isoformat()
        )

    except Exception as e:
        error_msg = f"An error occurred while processing your request: {str(e)}"
        log_interaction(request.query, error_msg, "error", 0)
        raise HTTPException(status_code=500, detail=error_msg)

@app.post("/embed", response_model=EmbedResponse)
def embed_document(request: EmbedRequest):
    """
    Embed and store a document in the vector database
    """
    try:
        # Index the document
        response = index_document(request.text)

        return EmbedResponse(
            success=True,
            document_id=response.get('_id'),
            timestamp=datetime.now().isoformat()
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to embed document: {str(e)}")

@app.post("/search", response_model=SearchResponse)
def search_documents(request: SearchRequest):
    """
    Search documents in vector database (for testing/debugging)
    """
    try:
        hits = search_opensearch(request.query, request.top_k)

        return SearchResponse(
            hits=hits,
            timestamp=datetime.now().isoformat()
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.post("/ingest-website")
def ingest_website():
    """
    Ingest content from Khan Institute website
    """
    try:
        print("Starting website ingestion...")

        # Scrape website
        text = scrape()
        print(f"Scraped {len(text)} characters")

        # Split into chunks
        chunks = split(text)
        print(f"Split into {len(chunks)} chunks")

        # Embed and store each chunk
        stored_count = 0
        for i, chunk in enumerate(chunks):
            try:
                index_document(chunk)
                stored_count += 1
                if (i + 1) % 10 == 0:
                    print(f"Stored {i + 1}/{len(chunks)} chunks")
            except Exception as e:
                print(f"Error storing chunk {i}: {e}")

        return {
            "success": True,
            "chunks_processed": len(chunks),
            "chunks_stored": stored_count,
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ingest failed: {str(e)}")

@app.get("/vapi/tool/bookAppointment/health")
async def vapi_tool_health():
    """Health check for Vapi tool endpoint"""
    return {
        "status": "healthy",
        "endpoint": "/vapi/tool/bookAppointment",
        "timestamp": datetime.now().isoformat()
    }

@app.post("/vapi/tool/bookAppointment")
async def vapi_booking_tool(request: Request):
    """
    Vapi Tool endpoint for booking appointments
    This endpoint receives tool calls from Vapi and submits to booking API
    
    Vapi expects response format:
    {
      "results": [
        {
          "toolCallId": "...",
          "result": "success message or error"
        }
      ]
    }
    """
    try:
        # Get raw body and all request info
        raw_body_bytes = await request.body()
        raw_body_str = raw_body_bytes.decode('utf-8') if raw_body_bytes else ''
        
        # Log for debugging
        print(f"\n{'='*60}")
        print(f"[VAPI TOOL] bookAppointment called at {datetime.now().isoformat()}")
        print(f"[VAPI TOOL] Raw body length: {len(raw_body_str)}")
        print(f"[VAPI TOOL] Raw body: {repr(raw_body_str[:500])}")
        print(f"[VAPI TOOL] Request method: {request.method}")
        print(f"[VAPI TOOL] Request URL: {request.url}")
        print(f"[VAPI TOOL] Query params: {dict(request.query_params)}")
        print(f"[VAPI TOOL] Headers: {dict(request.headers)}")
        print(f"{'='*60}")
        
        # Try to parse JSON body
        body = {}
        if raw_body_str:
            try:
                body = json.loads(raw_body_str)
                print(f"[VAPI TOOL] Parsed body: {json.dumps(body, indent=2)}")
            except json.JSONDecodeError as e:
                print(f"[VAPI TOOL] ❌ Failed to parse JSON: {e}")
        else:
            print(f"[VAPI TOOL] ⚠️ Empty request body!")
            # Check query params
            if request.query_params:
                body = dict(request.query_params)
                print(f"[VAPI TOOL] Using query params as body: {body}")
            else:
                # If body is empty, VAPI might be sending data differently
                # Try to get from headers or check if this is a GET request
                print(f"[VAPI TOOL] No query params either. This might be a VAPI configuration issue.")
                print(f"[VAPI TOOL] VAPI should send arguments in the request body, but it's empty.")
                print(f"[VAPI TOOL] The tool call data is likely in the webhook message instead.")
                # NOTE: The webhook intercepts tool calls and processes them.
                # If the tool endpoint is called with empty body, it means VAPI is calling it separately.
                # Return a message indicating the booking will be processed via webhook.
                # The webhook will handle the actual booking and return success.
                return {
                    "results": [
                        {
                            "toolCallId": "unknown",
                            "result": "Booking is being processed. Please wait for confirmation."
                        }
                    ]
                }
        
        # Check if arguments is in the body
        if 'arguments' not in body:
            print(f"[VAPI TOOL] ⚠️ 'arguments' key not found in body!")
            print(f"[VAPI TOOL] Available keys: {list(body.keys())}")
            # Try alternative keys
            if 'parameters' in body:
                print(f"[VAPI TOOL] Found 'parameters' key instead")
                body['arguments'] = body['parameters']
            elif 'data' in body:
                print(f"[VAPI TOOL] Found 'data' key instead")
                body['arguments'] = body['data']
            elif 'function' in body:
                print(f"[VAPI TOOL] Found 'function' key - this might be the tool call format")
                if isinstance(body.get('function'), dict) and 'arguments' in body['function']:
                    body['arguments'] = body['function']['arguments']
                    print(f"[VAPI TOOL] ✅ Extracted arguments from function key")
            else:
                # No arguments found - this is the problem
                print(f"[VAPI TOOL] ❌ No arguments found in any format!")
                print(f"[VAPI TOOL] VAPI is calling the tool endpoint but not sending the data.")
                print(f"[VAPI TOOL] The tool call data should be intercepted in the webhook instead.")
                # NOTE: The webhook intercepts and processes tool calls.
                # If we reach here, the webhook should have already processed the booking.
                # Return a message indicating processing is happening via webhook.
                return {
                    "results": [
                        {
                            "toolCallId": body.get('toolCallId', 'unknown'),
                            "result": "Booking is being processed via webhook. Please wait for confirmation."
                        }
                    ]
                }
        
        # Extract toolCallId (required for response)
        tool_call_id = body.get('toolCallId') or body.get('id') or 'unknown'
        print(f"[VAPI TOOL] Tool call ID: {tool_call_id}")
        
        # Extract parameters from Vapi tool call
        # Vapi can send parameters in different formats:
        # 1. body.parameters = {...}
        # 2. body = {...} (direct)
        # 3. body.arguments = {...} (can be dict or JSON string)
        params = {}
        
        # Try different possible parameter locations
        if 'parameters' in body and isinstance(body['parameters'], dict):
            params = body['parameters']
        elif 'arguments' in body:
            # Arguments can be a dict or a JSON string
            if isinstance(body['arguments'], dict):
                params = body['arguments']
            elif isinstance(body['arguments'], str):
                # Parse JSON string (may contain literal \n characters from VAPI)
                print(f"[VAPI TOOL] Arguments is a string, length: {len(body['arguments'])}")
                print(f"[VAPI TOOL] First 200 chars: {repr(body['arguments'][:200])}")
                
                # VAPI sends literal \n (backslash-n) characters, not actual newlines
                # First, try parsing as-is
                try:
                    params = json.loads(body['arguments'])
                    print(f"[VAPI TOOL] ✅ Successfully parsed arguments JSON string: {json.dumps(params, indent=2)}")
                except json.JSONDecodeError as e1:
                    print(f"[VAPI TOOL] First parse attempt failed: {e1}")
                    # Try removing literal \n and extra spaces
                    try:
                        cleaned_args = body['arguments'].replace('\\n', '').replace('\\', '').strip()
                        params = json.loads(cleaned_args)
                        print(f"[VAPI TOOL] ✅ Parsed arguments after cleaning: {json.dumps(params, indent=2)}")
                    except json.JSONDecodeError as e2:
                        print(f"[VAPI TOOL] Second parse attempt failed: {e2}")
                        # Last resort: extract fields using regex
                        import re
                        params = {}
                        # Extract string fields
                        for field in ['fullName', 'email', 'phone', 'location', 'date', 'time']:
                            match = re.search(rf'"{field}"\s*:\s*"([^"]+)"', body['arguments'])
                            if match:
                                params[field] = match.group(1).strip()
                        # Extract age (number)
                        age_match = re.search(r'"age"\s*:\s*(\d+)', body['arguments'])
                        if age_match:
                            params['age'] = int(age_match.group(1))
                        
                        if params and len(params) >= 7:
                            print(f"[VAPI TOOL] ⚠️ Extracted params using regex fallback: {json.dumps(params, indent=2)}")
                        else:
                            print(f"[VAPI TOOL] ❌ Failed to extract params. Raw: {repr(body['arguments'])}")
                            params = {}
        elif isinstance(body, dict):
            # Check if data is directly in body (excluding metadata fields)
            exclude_fields = ['toolCallId', 'id', 'callId', 'assistantId', 'type', 'name']
            params = {k: v for k, v in body.items() if k not in exclude_fields}
        
        print(f"[VAPI TOOL] Extracted params: {json.dumps(params, indent=2)}")
        print(f"[VAPI TOOL] Params type: {type(params)}, Keys: {list(params.keys()) if isinstance(params, dict) else 'Not a dict'}")
        
        # Extract booking data from various possible formats
        # Try multiple field name variations
        def get_field_value(field_variations):
            for field in field_variations:
                # Check params first (from arguments/parameters)
                if isinstance(params, dict) and field in params:
                    value = params[field]
                    if value:
                        return str(value).strip() if isinstance(value, str) else value
                # Fallback to body
                if isinstance(body, dict) and field in body:
                    value = body[field]
                if value:
                    return str(value).strip() if isinstance(value, str) else value
            return None
        
        booking_data = {
            'fullName': get_field_value(['fullName', 'name', 'full_name', 'patientName', 'patient_name']) or '',
            'age': get_field_value(['age', 'patientAge', 'patient_age']),
            'email': get_field_value(['email', 'emailAddress', 'email_address', 'patientEmail', 'patient_email']) or '',
            'phone': get_field_value(['phone', 'phoneNumber', 'phone_number', 'patientPhone', 'patient_phone', 'tel']) or '',
            'location': get_field_value(['location', 'preferredLocation', 'preferred_location', 'clinicLocation', 'clinic_location']) or '',
            'date': get_field_value(['date', 'appointmentDate', 'appointment_date', 'preferredDate', 'preferred_date']) or '',
            'time': get_field_value(['time', 'appointmentTime', 'appointment_time', 'preferredTime', 'preferred_time']) or ''
        }
        
        # Clean up empty strings
        booking_data = {k: (v if v else None) for k, v in booking_data.items()}
        
        print(f"[VAPI TOOL] Booking data extracted: {json.dumps(booking_data, indent=2)}")
        print(f"[VAPI TOOL] Checking each field:")
        for field in ['fullName', 'age', 'email', 'phone', 'location', 'date', 'time']:
            value = booking_data.get(field)
            print(f"  {field}: {repr(value)} (type: {type(value)}, truthy: {bool(value)})")
        
        # Normalize location
        if booking_data['location']:
            location_lower = booking_data['location'].lower()
            if 'beverly' in location_lower:
                booking_data['location'] = 'beverly'
            elif 'westlake' in location_lower:
                booking_data['location'] = 'westlake'
        
        # Validate required fields - check for None, empty string, or missing
        required_fields = ['fullName', 'age', 'email', 'phone', 'location', 'date', 'time']
        missing_fields = []
        for field in required_fields:
            value = booking_data.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing_fields.append(field)
        
        if missing_fields:
            error_msg = f"Missing required information: {', '.join(missing_fields)}. Please provide all details to complete the booking."
            print(f"[VAPI TOOL] ❌ Validation error: {error_msg}")
            print(f"[VAPI TOOL] Full booking data: {json.dumps(booking_data, indent=2)}")
            print(f"[VAPI TOOL] Params dict: {json.dumps(params, indent=2) if isinstance(params, dict) else 'Not a dict'}")
            print(f"[VAPI TOOL] Body dict keys: {list(body.keys()) if isinstance(body, dict) else 'Not a dict'}")
            # Return Vapi-expected format with error
            return {
                "results": [
                    {
                        "toolCallId": tool_call_id,
                        "result": error_msg
                    }
                ]
            }
        
        # Convert age to number if it's a string
        if isinstance(booking_data['age'], str):
            try:
                booking_data['age'] = int(booking_data['age'])
            except ValueError:
                error_msg = f"Invalid age format: {booking_data['age']}"
                print(f"[VAPI TOOL] Age conversion error: {error_msg}")
                return {
                    "results": [
                        {
                            "toolCallId": tool_call_id,
                            "result": error_msg
                        }
                    ]
                }
        
        # Submit to booking API
        print(f"[VAPI TOOL] Submitting booking to API: {json.dumps(booking_data, indent=2)}")
        
        # Skip emails for test addresses
        skip_emails = booking_data.get('email', '').endswith('@example.com') or 'test' in booking_data.get('email', '').lower()
        if skip_emails:
            print(f"[VAPI TOOL] ⚠️ Skipping emails for test address: {booking_data.get('email')}")
        
        try:
            # Call booking API with timeout
            result = await submit_booking(booking_data, skip_emails=skip_emails)
            print(f"[VAPI TOOL] ✅ Booking successful!")
            print(f"[VAPI TOOL] Result: {json.dumps(result, indent=2)}")
            
            # Verify booking was successful
            if result.get('success') or result.get('data'):
                # Create success message for Vapi to speak
                location_display = "Beverly Hills" if booking_data['location'] == 'beverly' else "Westlake Village"
                success_message = (
                    f"Perfect! I've successfully booked your appointment. "
                    f"Your consultation is scheduled for {booking_data['date']} at {booking_data['time']} "
                    f"at our {location_display} location. "
                    f"You'll receive a confirmation email at {booking_data['email']} shortly. "
                    f"Is there anything else I can help you with?"
                )
                
                # Return Vapi-expected format with success
                return {
                    "results": [
                        {
                            "toolCallId": tool_call_id,
                            "result": success_message
                        }
                    ]
                }
            else:
                # Booking API returned but indicated failure
                error_msg = "I encountered an issue while processing your booking. Please try again or contact us directly at (805) 230-2126."
                print(f"[VAPI TOOL] ⚠️ Booking API returned failure: {json.dumps(result)}")
                return {
                    "results": [
                        {
                            "toolCallId": tool_call_id,
                            "result": error_msg
                        }
                    ]
                }
            
        except httpx.TimeoutException as e:
            error_msg = "The booking system is taking longer than expected. Please try again in a moment or contact us directly at (805) 230-2126."
            print(f"[VAPI TOOL] ❌ Booking timeout: {str(e)}")
            print(f"[VAPI TOOL] Traceback: {traceback.format_exc()}")
            return {
                "results": [
                    {
                        "toolCallId": tool_call_id,
                        "result": error_msg
                    }
                ]
            }
        except httpx.HTTPStatusError as e:
            error_msg = f"I encountered an issue while booking your appointment. Please try again or contact us directly at (805) 230-2126."
            print(f"[VAPI TOOL] ❌ HTTP error: {e.response.status_code} - {e.response.text}")
            print(f"[VAPI TOOL] Traceback: {traceback.format_exc()}")
            return {
                "results": [
                    {
                        "toolCallId": tool_call_id,
                        "result": error_msg
                    }
                ]
            }
        except Exception as e:
            error_msg = "I encountered an issue while booking your appointment. Please try again or contact us directly at (805) 230-2126."
            print(f"[VAPI TOOL] ❌ Booking failed: {str(e)}")
            print(f"[VAPI TOOL] Error type: {type(e).__name__}")
            print(f"[VAPI TOOL] Traceback: {traceback.format_exc()}")
            
            # Return Vapi-expected format with error
            return {
                "results": [
                    {
                        "toolCallId": tool_call_id,
                        "result": error_msg
                    }
                ]
            }
            
    except json.JSONDecodeError as e:
        error_msg = "Invalid request format"
        print(f"[VAPI TOOL] JSON decode error: {str(e)}")
        tool_call_id = 'unknown'
        return {
            "results": [
                {
                    "toolCallId": tool_call_id,
                    "result": error_msg
                }
            ]
        }
        
    except Exception as e:
        error_msg = "An unexpected error occurred. Please try again or contact us at (805) 230-2126."
        print(f"[VAPI TOOL] ❌ Unexpected error: {str(e)}")
        print(f"[VAPI TOOL] Traceback: {traceback.format_exc()}")
        tool_call_id = body.get('toolCallId', 'unknown') if 'body' in locals() else 'unknown'
        # Always return HTTP 200 with error in response body (Vapi requirement)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=200,
            content={
                "results": [
                    {
                        "toolCallId": tool_call_id,
                        "result": error_msg
                    }
                ]
            }
        )

@app.get("/logs")
def get_logs(limit: int = 50):
    """
    Get recent interaction logs (for debugging)
    """
    try:
        if not os.path.exists(LOG_FILE):
            return {"logs": []}

        with open(LOG_FILE, 'r') as f:
            logs = json.load(f)

        # Return most recent logs
        recent_logs = logs[-limit:] if len(logs) > limit else logs

        return {"logs": recent_logs}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read logs: {str(e)}")

@app.post("/guru/chat", response_model=VapiChatResponse)
def guru_chat(request: VapiChatRequest):
    """
    Vapi-compatible chat endpoint
    Receives messages from Vapi and returns RAG responses
    """
    try:
        # ---- LACS consumer hook (/guru/chat) ----
        _lacs = lacs_consumer.consult(request.query, "chat")
        if _lacs is not None:
            return VapiChatResponse(answer=_lacs["text"])
        # Search for relevant documents
        hits = search_opensearch(request.query, top_k=5)
        hits = boost_rag_hits(request.query, hits)

        if not hits:
            # Even with no hits, provide helpful response about Khanna Institute
            response = "I don't have enough specific information about that in my knowledge base. However, I can tell you that Khanna Vision Institute, led by Dr. Rajesh Khanna, offers comprehensive vision correction services including LASIK, SMILE laser, EVO ICL, and other advanced procedures. Please contact us directly at (805) 230-2126 or visit https://khannainstitute.com/contact/schedule-consultation/ for more information."
            model_used = "none"
        else:
            # Generate answer with fallback logic
            response, model_used = generate_answer_with_fallback(request.query, hits)

        response = sanitize_guru_answer(response, request.query)
        response = sanitize_phone_in_response(response)

        # Log the interaction with session ID
        log_interaction(f"[Session: {request.session_id}] {request.query}", response, model_used, len(hits))

        return VapiChatResponse(answer=response)

    except Exception as e:
        error_msg = f"An error occurred while processing your request: {str(e)}"
        log_interaction(f"[Session: {request.session_id}] {request.query}", error_msg, "error", 0)
        # Return error message that will trigger Vapi fallback
        raise HTTPException(status_code=500, detail=error_msg)

@app.post("/vapi/webhook")
async def vapi_webhook(request: Request):
    """
    Vapi webhook handler
    Receives webhook calls from Vapi and processes them
    Handles both general Q&A and appointment booking
    """
    try:
        # ── Security: rate limit by IP ────────────────────────────────────
        client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
        client_ip = client_ip.split(",")[0].strip()
        rate_msg = check_rate_limit(client_ip)
        if rate_msg:
            print(f"[SECURITY] Rate limit hit for IP {client_ip}")
            return JSONResponse(
                status_code=429,
                content={"messages": [{"type": "text", "text": rate_msg}]}
            )

        # ── Security: enforce request size ───────────────────────────────
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 32_000:
            return JSONResponse(
                status_code=413,
                content={"messages": [{"type": "text", "text": "Message too large."}]}
            )

        # Get request body as JSON
        body = await request.json()
        
        # DEBUG: Log the full request body to understand Vapi's format
        # This is CRITICAL for debugging - log everything Vapi sends
        print(f"\n{'='*80}")
        print(f"[VAPI WEBHOOK] Received request at {datetime.now().isoformat()}")
        print("[VAPI WEBHOOK] request body redacted (remediation 2026-09-15)")
        print(f"[VAPI WEBHOOK] Body type: {type(body)}")
        print(f"[VAPI WEBHOOK] Body keys: {list(body.keys()) if isinstance(body, dict) else 'N/A'}")
        # Check for tool calls in various locations
        if isinstance(body, dict):
            if 'message' in body:
                msg = body['message']
                print(f"[VAPI WEBHOOK] message type: {type(msg)}")
                if isinstance(msg, dict):
                    print(f"[VAPI WEBHOOK] message keys: {list(msg.keys())}")
                    if 'artifact' in msg:
                        print(f"[VAPI WEBHOOK] Found artifact in message!")
                        artifact = msg['artifact']
                        if isinstance(artifact, dict) and 'messages' in artifact:
                            print(f"[VAPI WEBHOOK] artifact.messages: {len(artifact['messages'])} messages")
            if 'messages' in body:
                print(f"[VAPI WEBHOOK] Found messages array: {len(body['messages'])} messages")
        print(f"{'='*80}\n")
        
        # Vapi sends events - check for 'type' field
        # Common types: 'model-output', 'assistant-request', 'function-call', 'tool-calls', etc.
        event_type = body.get('type') if isinstance(body, dict) else None
        print(f"[VAPI WEBHOOK] Event type: {event_type}")
        
        # Also check for tool calls at the top level (some VAPI versions send them here)
        if isinstance(body, dict):
            if 'toolCalls' in body or 'tool_calls' in body:
                tool_calls = body.get('toolCalls') or body.get('tool_calls') or []
                print(f"[VAPI WEBHOOK] Found tool calls at top level: {len(tool_calls)}")
                for tool_call in tool_calls:
                    if isinstance(tool_call, dict):
                        func_data = tool_call.get('function', {})
                        if func_data.get('name') == 'bookAppointment':
                            print(f"[VAPI WEBHOOK] 🔧 Intercepted bookAppointment at top level!")
                            arguments_str = func_data.get('arguments', '')
                            tool_call_id = tool_call.get('id', 'unknown')
                            
                            # Deduplication: Check if we've already processed this tool call
                            if tool_call_id in processed_tool_calls:
                                print(f"[VAPI WEBHOOK] ⚠️ Tool call {tool_call_id} already processed - skipping duplicate")
                                try:
                                    if isinstance(arguments_str, str):
                                        try:
                                            arguments = json.loads(arguments_str)
                                        except json.JSONDecodeError:
                                            cleaned = arguments_str.replace('\\n', '').replace(' ', '')
                                            arguments = json.loads(cleaned)
                                    else:
                                        arguments = arguments_str
                                    
                                    location_display = "Beverly Hills" if arguments.get('location') == 'beverly' else "Westlake Village"
                                    success_msg = (
                                        f"Perfect! I've successfully booked your appointment. "
                                        f"Your consultation is scheduled for {arguments.get('date')} at {arguments.get('time')} "
                                        f"at our {location_display} location. "
                                        f"You'll receive a confirmation email at {arguments.get('email')} shortly. "
                                        f"Is there anything else I can help you with?"
                                    )
                                    return {
                                        "messages": [
                                            {
                                                "role": "assistant",
                                                "content": success_msg
                                            }
                                        ]
                                    }
                                except:
                                    return {
                                        "messages": [
                                            {
                                                "role": "assistant",
                                                "content": "Your appointment has been booked successfully. You'll receive a confirmation email shortly."
                                            }
                                        ]
                                    }
                            
                            # Mark this tool call as processed
                            processed_tool_calls.add(tool_call_id)
                            print(f"[VAPI WEBHOOK] ✅ Marked tool call {tool_call_id} as processed (top level)")
                            
                            try:
                                if isinstance(arguments_str, str):
                                    try:
                                        arguments = json.loads(arguments_str)
                                    except json.JSONDecodeError:
                                        cleaned = arguments_str.replace('\\n', '').replace(' ', '')
                                        arguments = json.loads(cleaned)
                                else:
                                    arguments = arguments_str
                                
                                print(f"[VAPI WEBHOOK] ✅ Parsed arguments: {json.dumps(arguments, indent=2)}")
                                booking_result = await submit_booking(arguments)
                                print(f"[VAPI WEBHOOK] ✅ Booking result: {json.dumps(booking_result, indent=2)}")
                                
                                location_display = "Beverly Hills" if arguments.get('location') == 'beverly' else "Westlake Village"
                                success_msg = (
                                    f"Perfect! I've successfully booked your appointment. "
                                    f"Your consultation is scheduled for {arguments.get('date')} at {arguments.get('time')} "
                                    f"at our {location_display} location. "
                                    f"You'll receive a confirmation email at {arguments.get('email')} shortly. "
                                    f"Is there anything else I can help you with?"
                                )
                                
                                print(f"[VAPI WEBHOOK] ✅ Booking successful from top-level tool call!")
                                return {
                                    "messages": [
                                        {
                                            "role": "assistant",
                                            "content": success_msg
                                        }
                                    ]
                                }
                            except Exception as e:
                                print(f"[VAPI WEBHOOK] ❌ Error handling top-level tool call: {e}")
                                import traceback
                                print(traceback.format_exc())
        
        # Extract message from Vapi webhook payload
        # Vapi sends different payload structures, handle both
        message = None
        session_id = None

        if isinstance(body, dict):
            # ALWAYS check for tool calls in message.artifact.messages (regardless of event type)
            # This is where VAPI sends tool calls in webhook payloads
            artifact = body.get('message', {}).get('artifact', {}) if isinstance(body.get('message'), dict) else {}
            if artifact and 'messages' in artifact:
                messages = artifact.get('messages', [])
                print(f"[VAPI WEBHOOK] Checking artifact.messages for tool calls ({len(messages)} messages)")
                for msg in messages:
                    if isinstance(msg, dict):
                        # Check for tool_calls role
                        if msg.get('role') == 'tool_calls':
                            tool_calls = msg.get('toolCalls') or []
                            print(f"[VAPI WEBHOOK] Found tool_calls message with {len(tool_calls)} tool calls")
                            for tool_call in tool_calls:
                                if isinstance(tool_call, dict) and tool_call.get('type') == 'function':
                                    func_data = tool_call.get('function', {})
                                    if func_data.get('name') == 'bookAppointment':
                                        print(f"[VAPI WEBHOOK] 🔧 Intercepted bookAppointment tool call from artifact!")
                                        arguments_str = func_data.get('arguments', '')
                                        tool_call_id = tool_call.get('id', 'unknown')
                                        print(f"[VAPI WEBHOOK] Tool call ID: {tool_call_id}")
                                        print(f"[VAPI WEBHOOK] Arguments (first 200 chars): {repr(arguments_str[:200])}")
                                        
                                        # Deduplication: Check if we've already processed this tool call
                                        if tool_call_id in processed_tool_calls:
                                            print(f"[VAPI WEBHOOK] ⚠️ Tool call {tool_call_id} already processed - skipping duplicate")
                                            try:
                                                # Parse arguments just for the response message
                                                if isinstance(arguments_str, str):
                                                    try:
                                                        arguments = json.loads(arguments_str)
                                                    except json.JSONDecodeError:
                                                        cleaned = arguments_str.replace('\\n', '').replace(' ', '')
                                                        arguments = json.loads(cleaned)
                                                else:
                                                    arguments = arguments_str
                                                
                                                location_display = "Beverly Hills" if arguments.get('location') == 'beverly' else "Westlake Village"
                                                success_msg = (
                                                    f"Perfect! I've successfully booked your appointment. "
                                                    f"Your consultation is scheduled for {arguments.get('date')} at {arguments.get('time')} "
                                                    f"at our {location_display} location. "
                                                    f"You'll receive a confirmation email at {arguments.get('email')} shortly. "
                                                    f"Is there anything else I can help you with?"
                                                )
                                                return {
                                                    "messages": [
                                                        {
                                                            "role": "assistant",
                                                            "content": success_msg
                                                        }
                                                    ]
                                                }
                                            except:
                                                # If parsing fails, return generic success
                                                return {
                                                    "messages": [
                                                        {
                                                            "role": "assistant",
                                                            "content": "Your appointment has been booked successfully. You'll receive a confirmation email shortly."
                                                        }
                                                    ]
                                                }
                                        
                                        # Mark this tool call as processed BEFORE processing
                                        processed_tool_calls.add(tool_call_id)
                                        print(f"[VAPI WEBHOOK] ✅ Marked tool call {tool_call_id} as processed (artifact)")
                                        
                                        # Parse arguments and handle booking
                                        try:
                                            if isinstance(arguments_str, str):
                                                # Handle \n characters - try parsing as-is first
                                                try:
                                                    arguments = json.loads(arguments_str)
                                                except json.JSONDecodeError:
                                                    # If that fails, clean it
                                                    cleaned = arguments_str.replace('\\n', '').replace(' ', '')
                                                    arguments = json.loads(cleaned)
                                            else:
                                                arguments = arguments_str
                                            
                                            print(f"[VAPI WEBHOOK] ✅ Parsed arguments: {json.dumps(arguments, indent=2)}")
                                            
                                            # Submit booking directly from webhook
                                            booking_result = await submit_booking(arguments)
                                            
                                            print(f"[VAPI WEBHOOK] ✅ Booking result: {json.dumps(booking_result, indent=2)}")
                                            
                                            # Return success response
                                            location_display = "Beverly Hills" if arguments.get('location') == 'beverly' else "Westlake Village"
                                            success_msg = (
                                                f"Perfect! I've successfully booked your appointment. "
                                                f"Your consultation is scheduled for {arguments.get('date')} at {arguments.get('time')} "
                                                f"at our {location_display} location. "
                                                f"You'll receive a confirmation email at {arguments.get('email')} shortly. "
                                                f"Is there anything else I can help you with?"
                                            )
                                            
                                            print(f"[VAPI WEBHOOK] ✅ Booking successful from webhook!")
                                            return {
                                                "messages": [
                                                    {
                                                        "role": "assistant",
                                                        "content": success_msg
                                                    }
                                                ]
                                            }
                                        except Exception as e:
                                            print(f"[VAPI WEBHOOK] ❌ Error handling tool call: {e}")
                                            import traceback
                                            print(traceback.format_exc())
            
            # Handle Vapi event format
            if event_type == 'model-output':
                
                # Extract message from various possible locations
                message = (
                    body.get('message', {}).get('content', '') if isinstance(body.get('message'), dict) else
                    str(body.get('message', '')) if body.get('message') else
                    body.get('transcript', {}).get('text', '') if isinstance(body.get('transcript'), dict) else
                    str(body.get('transcript', '')) if body.get('transcript') else
                    body.get('text', '') or
                    body.get('query', '') or
                    body.get('input', '') or
                    (body.get('messages', [{}])[-1].get('content', '') if body.get('messages') and len(body.get('messages', [])) > 0 else '')
                )
                session_id = (
                    body.get('call', {}).get('id') if isinstance(body.get('call'), dict) else
                    body.get('callId') or
                    body.get('sessionId') or
                    body.get('conversationId') or
                    'unknown'
                )
                print(f"[VAPI WEBHOOK] model-output event - Message: '{message}', Session: {session_id}")
            elif event_type == 'transcript':
                # Vapi transcript event - user's speech
                message = (
                    body.get('transcript', {}).get('text', '') if isinstance(body.get('transcript'), dict) else
                    str(body.get('transcript', '')) if body.get('transcript') else
                    body.get('text', '') or
                    body.get('message', {}).get('content', '') if isinstance(body.get('message'), dict) else ''
                )
                session_id = (
                    body.get('call', {}).get('id') if isinstance(body.get('call'), dict) else
                    body.get('callId') or
                    'unknown'
                )
                print(f"[VAPI WEBHOOK] transcript event - Message: '{message}', Session: {session_id}")
            elif event_type == 'assistant-request':
                # Vapi assistant-request event format (legacy)
                message = body.get('message', {}).get('content', '') or body.get('transcript', '')
                session_id = body.get('call', {}).get('id') or body.get('callId') or 'unknown'
            elif 'message' in body:
                message = body['message'].get('content', '') if isinstance(body['message'], dict) else str(body['message'])
            elif 'transcript' in body:
                message = body['transcript']
            elif 'query' in body:
                message = body['query']
            elif 'text' in body:
                message = body['text']
            elif 'messages' in body and len(body.get('messages', [])) > 0:
                # Vapi might send messages array - check all messages for tool calls
                for msg in body['messages']:
                    if isinstance(msg, dict) and msg.get('role') == 'tool_calls':
                        tool_calls = msg.get('toolCalls') or []
                        print(f"[VAPI WEBHOOK] Found tool_calls message with {len(tool_calls)} tool calls")
                        for tool_call in tool_calls:
                            if isinstance(tool_call, dict) and tool_call.get('type') == 'function':
                                func_data = tool_call.get('function', {})
                                if func_data.get('name') == 'bookAppointment':
                                    print(f"[VAPI WEBHOOK] 🔧 Intercepted bookAppointment tool call!")
                                    arguments_str = func_data.get('arguments', '')
                                    tool_call_id = tool_call.get('id', 'unknown')
                                    print(f"[VAPI WEBHOOK] Tool call ID: {tool_call_id}")
                                    print(f"[VAPI WEBHOOK] Arguments (first 200 chars): {repr(arguments_str[:200])}")
                                    
                                    # Parse arguments and handle booking
                                    try:
                                        if isinstance(arguments_str, str):
                                            # Handle \n characters - try parsing as-is first
                                            try:
                                                arguments = json.loads(arguments_str)
                                            except json.JSONDecodeError:
                                                # If that fails, clean it
                                                cleaned = arguments_str.replace('\\n', '').replace(' ', '')
                                                arguments = json.loads(cleaned)
                                        else:
                                            arguments = arguments_str
                                        
                                        print(f"[VAPI WEBHOOK] ✅ Parsed arguments: {json.dumps(arguments, indent=2)}")
                                        
                                        # Submit booking directly from webhook
                                        booking_result = await submit_booking(arguments)
                                        
                                        print(f"[VAPI WEBHOOK] ✅ Booking result: {json.dumps(booking_result, indent=2)}")
                                        
                                        # Return success response
                                        location_display = "Beverly Hills" if arguments.get('location') == 'beverly' else "Westlake Village"
                                        success_msg = (
                                            f"Perfect! I've successfully booked your appointment. "
                                            f"Your consultation is scheduled for {arguments.get('date')} at {arguments.get('time')} "
                                            f"at our {location_display} location. "
                                            f"You'll receive a confirmation email at {arguments.get('email')} shortly. "
                                            f"Is there anything else I can help you with?"
                                        )
                                        
                                        print(f"[VAPI WEBHOOK] ✅ Booking successful from webhook!")
                                        return {
                                            "messages": [
                                                {
                                                    "role": "assistant",
                                                    "content": success_msg
                                                }
                                            ]
                                        }
                                    except Exception as e:
                                        print(f"[VAPI WEBHOOK] ❌ Error handling tool call: {e}")
                                        import traceback
                                        print(traceback.format_exc())
                
                # Get message from last entry if no tool calls found
                last_msg = body['messages'][-1]
                message = last_msg.get('content') or last_msg.get('text') or str(last_msg)
            
            session_id = body.get('call', {}).get('id') or body.get('session_id') or body.get('conversationId') or body.get('callId') or 'unknown'
            
            # DEBUG: Log extracted values
            print(f"[VAPI WEBHOOK] Event type: {event_type}, Message: {message}, Session: {session_id}")

        if not message:
            return {"error": "No message found in webhook payload", "received": body}

        # ── Security: sanitize and length-limit message ───────────────────
        message, was_injected = sanitize_input(message)
        # ---- LACS consumer hook (/vapi/webhook: website text + voice) ----
        try:
            import asyncio as _asyncio
            _lacs = await _asyncio.get_running_loop().run_in_executor(None, lacs_consumer.consult, message, "webhook")
        except Exception:
            _lacs = None
        if _lacs is not None:
            return {"messages": [{"type": "text", "text": _lacs["text"]}], "active_agent": "lacs",
                    "lacs": {"documentId": _lacs["documentId"], "version": _lacs["version"], "requiresHumanReview": True}}
        if was_injected:
            print(f"[SECURITY] Prompt injection attempt blocked from session {session_id}")

        # Initialize or get booking session
        if session_id not in booking_sessions:
            booking_sessions[session_id] = {
                'in_booking_flow': False,
                'fullName': None,
                'age': None,
                'email': None,
                'phone': None,
                'location': None,
                'date': None,
                'time': None,
                'booking_agent': None,
                'just_entered': False,
                'bookings_submitted': 0,  # prevent email spam per session
            }

        # Block sessions that already submitted a booking (max 2 per session)
        if booking_sessions[session_id].get('bookings_submitted', 0) >= 2:
            answer = "You've already submitted a booking request. Our team will contact you shortly. For urgent help call (805) 230-2126."
            return {"messages": [{"type": "text", "text": answer}], "active_agent": "brandi"}

        session_data = booking_sessions[session_id]

        # Check if user wants to book or is already in booking flow
        fresh_booking_intent = detect_booking_intent(message) and not session_data['in_booking_flow']
        is_booking_intent = fresh_booking_intent or session_data['in_booking_flow']

        # Debug logging
        print(f"[Vapi Webhook] Session: {session_id}, Message: {message}, Booking Intent: {is_booking_intent}, In Flow: {session_data['in_booking_flow']}")

        if is_booking_intent:
            # Enter or continue booking flow
            if fresh_booking_intent:
                session_data['in_booking_flow'] = True
                session_data['just_entered'] = True
                # Capture which agent is currently active so we can include it in the email
                if session_id in ecosystem_sessions:
                    active = ecosystem_sessions[session_id].get("active_agent", "brandi")
                    _agent_labels = {
                        "brandi": "Brandi", "guru": "Guru", "max": "Max",
                        "lucy": "Lucy", "rose": "Rose", "kate": "Kate",
                        "sage": "Sage", "buffet": "Buffett", "barbie": "Barbie", "jill": "Jill",
                    }
                    session_data['booking_agent'] = _agent_labels.get(active, "Guru AI")
                else:
                    session_data['booking_agent'] = "Guru AI"
            else:
                session_data['just_entered'] = False

            # Extract information from current message
            # Skip name extraction on the very first trigger message (e.g. "please book an appointment")
            # so we ask for the name fresh rather than accidentally capturing the intent phrase
            if not session_data.get('fullName') and not session_data.get('just_entered'):
                extracted_name = extract_name(message)
                if extracted_name:
                    session_data['fullName'] = extracted_name
            
            if not session_data.get('age'):
                extracted_age = extract_age(message)
                if extracted_age:
                    session_data['age'] = extracted_age
            
            if not session_data.get('email'):
                extracted_email = extract_email(message)
                if extracted_email:
                    session_data['email'] = extracted_email
            
            if not session_data.get('phone'):
                extracted_phone = extract_phone(message)
                if extracted_phone:
                    session_data['phone'] = extracted_phone
            
            if not session_data.get('location'):
                extracted_location = extract_location(message)
                if extracted_location:
                    session_data['location'] = extracted_location
            
            if not session_data.get('date'):
                extracted_date, _ = extract_date_time(message)
                if extracted_date:
                    session_data['date'] = extracted_date
            
            if not session_data.get('time'):
                _, extracted_time = extract_date_time(message)
                if extracted_time:
                    session_data['time'] = extracted_time
            
            # Check if we have all required information
            required_fields = ['fullName', 'age', 'email', 'phone', 'location', 'date', 'time']
            missing_fields = [field for field in required_fields if not session_data.get(field)]
            
            if not missing_fields:
                # All information collected - submit booking
                try:
                    booking_agent_label = session_data.get('booking_agent') or 'Guru AI'
                    booking_result = await submit_booking({
                        'fullName': session_data['fullName'],
                        'age': session_data['age'],
                        'email': session_data['email'],
                        'phone': session_data['phone'],
                        'location': session_data['location'],
                        'date': session_data['date'],
                        'time': session_data['time'],
                        'pageUrl': f'Chat Widget — {booking_agent_label} Agent',
                    })
                    
                    # Clear booking session but keep submission count
                    submitted_count = booking_sessions[session_id].get('bookings_submitted', 0) + 1
                    booking_sessions[session_id] = {
                        'in_booking_flow': False,
                        'fullName': None,
                        'age': None,
                        'email': None,
                        'phone': None,
                        'location': None,
                        'date': None,
                        'time': None,
                        'booking_agent': None,
                        'just_entered': False,
                        'bookings_submitted': submitted_count,
                    }
                    
                    loc_display = "Beverly Hills" if session_data['location'] == 'beverly' else "Westlake Village"
                    answer = (
                        f"Perfect! I've scheduled your consultation for {session_data['date']} at "
                        f"{session_data['time']} at our {loc_display} location. "
                        f"You'll receive a confirmation email at {session_data['email']} shortly. "
                        f"Is there anything else I can help you with?"
                    )
                    model_used = "booking_submitted"
                    
                except Exception as e:
                    answer = f"I encountered an issue submitting your booking. Please try again or call us directly at (805) 230-2126. Error: {str(e)}"
                    model_used = "booking_error"
            else:
                # Ask for next missing piece of information
                next_question = get_next_booking_question(session_data)
                answer = next_question or "I need a bit more information to complete your booking."
                model_used = "booking_collection"
            
            # Log booking interaction
            log_interaction(f"[Vapi Booking Session: {session_id}] {message}", answer, model_used, 0)
            
        else:
            # Regular Q&A flow — single Guru (legacy) or KVI multi-agent ecosystem
            mv = (os.getenv("KVI_MULTI_AGENT") or "1").strip().lower()
            use_multi = mv not in ("0", "false", "no", "off")

            if use_multi:
                if session_id not in ecosystem_sessions:
                    ecosystem_sessions[session_id] = init_eco_session()
                eco = ecosystem_sessions[session_id]
                early_reply, active_agent = eco_turn(eco, message)
                print(f"[VAPI WEBHOOK] multi-agent active_agent={active_agent} early_reply={'yes' if early_reply else 'no'}")

                if early_reply:
                    answer = early_reply
                    model_used = active_agent if active_agent in ("brandi", "jill") else "routing"
                    hits_for_log: List[Dict[str, Any]] = []
                else:
                    hits = search_opensearch(message, top_k=5)
                    hits = boost_rag_hits(message, hits)

                    pricing_keywords = ['cost', 'price', 'pricing', 'how much', 'fee', 'charge', 'exam cost', 'consultation cost']
                    contact_keywords = ['phone number', 'phone', 'contact number', 'call us', 'our number', 'address', 'westlake', 'location', 'reach us']
                    if any(kw in message.lower() for kw in pricing_keywords) or any(kw in message.lower() for kw in contact_keywords):
                        hits = [{"text": PRICING_FACTS}] + (hits or [])

                    if not hits:
                        answer = (
                            "I don't have enough specific information about that in my knowledge base. However, I can tell you that "
                            "Khanna Vision Institute, led by Dr. Rajesh Khanna, offers comprehensive vision correction services including LASIK, "
                            "SMILE laser, EVO ICL, and other advanced procedures. Please contact us directly at (805) 230-2126 or visit "
                            "https://khannainstitute.com/contact/schedule-consultation/ for more information."
                        )
                        model_used = "none"
                    else:
                        answer, model_used = generate_answer_for_agent(active_agent, message, hits)

                    hits_for_log = hits
            else:
                hits = search_opensearch(message, top_k=5)
                hits = boost_rag_hits(message, hits)

                pricing_keywords = ['cost', 'price', 'pricing', 'how much', 'fee', 'charge', 'exam cost', 'consultation cost']
                contact_keywords = ['phone number', 'phone', 'contact number', 'call us', 'our number', 'address', 'westlake', 'location', 'reach us']
                if any(kw in message.lower() for kw in pricing_keywords) or any(kw in message.lower() for kw in contact_keywords):
                    hits = [{"text": PRICING_FACTS}] + (hits or [])

                if not hits:
                    answer = (
                        "I don't have enough specific information about that in my knowledge base. However, I can tell you that "
                        "Khanna Vision Institute, led by Dr. Rajesh Khanna, offers comprehensive vision correction services including LASIK, "
                        "SMILE laser, EVO ICL, and other advanced procedures. Please contact us directly at (805) 230-2126 or visit "
                        "https://khannainstitute.com/contact/schedule-consultation/ for more information."
                    )
                    model_used = "none"
                else:
                    answer, model_used = generate_answer_with_fallback(message, hits)

                hits_for_log = hits

            # Sanitize: enforce Dr. Khanna years; never return wrong phone numbers
            answer = sanitize_guru_answer(answer, message)
            answer = sanitize_phone_in_response(answer)

            log_interaction(
                f"[Vapi Session: {session_id}] {message}",
                answer,
                model_used,
                len(hits_for_log),
            )

        # Return in Vapi-expected format
        # Determine which agent is currently active (for widget UI switching)
        if session_id in ecosystem_sessions:
            current_agent = ecosystem_sessions[session_id].get("active_agent", "brandi")
        else:
            current_agent = "guru"

        response = {
            "messages": [
                {
                    "type": "text",
                    "text": answer
                }
            ],
            "active_agent": current_agent
        }
        
        # If this is an assistant-request event, include toolCallId if present
        if event_type == 'assistant-request' and 'toolCallId' in body:
            response['toolCallId'] = body['toolCallId']
        
        # DEBUG: Log response
        print(f"[VAPI WEBHOOK] active_agent={current_agent} | Sending response: {json.dumps(response, indent=2)}")
        
        return response

    except Exception as e:
        error_msg = f"Error processing webhook: {str(e)}"
        print(f"Webhook error: {error_msg}")
        # Return error that will trigger Vapi fallback
        return {
            "error": error_msg,
            "messages": [
                {
                    "type": "text",
                    "text": "I'm having trouble processing that request. Please try again or contact Khanna Institute directly."
                }
            ]
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
