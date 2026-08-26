from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any, Tuple
import json
import os
import re
import httpx
import traceback
from datetime import datetime
from utils import search_opensearch, index_document, embed_text
from llm_providers import generate_answer_with_fallback, generate_answer
from ingest import scrape, split
from faq_matcher import match_faq
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Guru AI RAG API", version="1.0.0", description="Medical AI assistant with RAG capabilities")

# Deduplication: Track processed tool calls to prevent duplicate bookings
processed_tool_calls = set()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
cancel_sessions: Dict[str, Dict[str, Any]] = {}

def detect_cancel_intent(message: str) -> bool:
    """Detect if user wants to cancel an appointment"""
    cancel_keywords = [
        'cancel', 'cancellation', 'cancel my appointment', 'cancel appointment',
        'i need to cancel', 'want to cancel', 'would like to cancel',
        'cancel my booking', 'cancel my consultation', 'cancel my visit'
    ]
    message_lower = message.lower()
    return any(keyword in message_lower for keyword in cancel_keywords)

def extract_procedure(message: str) -> Optional[str]:
    """Extract procedure type from message"""
    procedures = ['LASIK', 'SMILE', 'EVO ICL', 'PIE', 'Cataract', 'Consultation', 'Exam', 'New Patient Exam']
    message_lower = message.lower()
    for proc in procedures:
        if proc.lower() in message_lower:
            return proc
    return None

def detect_booking_intent(message: str) -> bool:
    """Detect if user wants to book an appointment"""
    booking_keywords = [
        'book', 'booking', 'schedule', 'appointment', 'consultation',
        'make an appointment', 'set up', 'reserve', 'available times',
        'when can i come', 'i want to see', 'i need an appointment',
        'i would like to', 'can i schedule', 'want to book', 'need to book',
        'set up appointment', 'make appointment', 'book me', 'schedule me',
        'when available', 'available dates', 'book consultation', 'schedule consultation'
    ]
    message_lower = message.lower()
    return any(keyword in message_lower for keyword in booking_keywords)

def extract_name(message: str) -> Optional[str]:
    """Extract name from message"""
    # Look for patterns like "my name is X", "I'm X", "call me X"
    patterns = [
        r"(?:my name is|i'm|i am|call me|this is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
        r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)$",  # Just a name
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1).strip()
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
    # Look for "I'm X years old", "age X", "X years", or just "35"
    patterns = [
        r"(?:i'm|i am|age|aged)\s+(\d+)\s*(?:years?|old)?",
        r"(\d+)\s*(?:years?|old)",
        r"^\s*(\d{1,3})\s*$",  # Standalone number e.g. "35"
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            age = int(match.group(1))
            if 1 <= age <= 120:  # Reasonable age range
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
    """Extract date and time from message"""
    # This is a simplified version - in production, use a proper date parser
    # For now, we'll guide the user to provide specific dates
    date = None
    time = None
    
    # Look for common date patterns
    date_patterns = [
        r'(?:on|for|this|next)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)',
        r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}',
        r'(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}',
    ]
    
    for pattern in date_patterns:
        if re.search(pattern, message, re.IGNORECASE):
            date = "extracted"  # Placeholder - would need proper parsing
    
    # Look for time patterns
    time_patterns = [
        r'\d{1,2}:\d{2}\s*(?:am|pm)',
        r'\d{1,2}\s*(?:am|pm)',
    ]
    
    for pattern in time_patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            time = match.group(0)
    
    return date, time

async def submit_booking(booking_data: Dict[str, Any]) -> Dict[str, Any]:
    """Submit booking to the booking API"""
    try:
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

def get_next_booking_question(session_data: Dict[str, Any]) -> str:
    """Determine what question to ask next for booking"""
    if not session_data.get('fullName'):
        return "Great! I'd be happy to help you schedule a consultation. To get started, may I have your full name?"
    
    if not session_data.get('age'):
        return f"Thank you, {session_data.get('fullName', 'there')}. What is your age?"
    
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

def get_next_cancel_question(session_data: Dict[str, Any]) -> str:
    """Determine what question to ask next for cancellation"""
    if not session_data.get('fullName'):
        return "I'd be happy to help you cancel your appointment. May I have your full name?"
    if not session_data.get('age'):
        return f"Thank you, {session_data.get('fullName', 'there')}. What is your age?"
    if not session_data.get('procedure'):
        return "Which procedure or appointment type would you like to cancel? For example: LASIK, SMILE, EVO ICL, PIE, Consultation, or Exam."
    if not session_data.get('email'):
        return "What is your email address?"
    return None

async def submit_cancel(cancel_data: Dict[str, Any]) -> Dict[str, Any]:
    """Submit cancellation to the API"""
    try:
        cancel_url = os.getenv("CANCEL_API_URL", "https://khannainstitute.com/api/booking/cancel")
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(cancel_url, json=cancel_data, headers={"Content-Type": "application/json"})
            response.raise_for_status()
            return response.json()
    except Exception as e:
        print(f"[SUBMIT_CANCEL] Error: {e}")
        raise

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

@app.get("/health", response_model=HealthResponse)
def health_check():
    """Health check endpoint"""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now().isoformat(),
        version="1.0.0"
    )

@app.post("/ask", response_model=AskResponse)
def ask_guru(request: AskRequest):
    """
    Main RAG endpoint - takes a query, searches vector DB, and generates answer with fallback
    """
    try:
        # Search for relevant documents
        hits = search_opensearch(request.query, request.top_k)

        if not hits:
            # Even with no hits, provide helpful response about Khanna Institute
            response = "I don't have enough specific information about that in my knowledge base. However, I can tell you that Khanna Vision Institute, led by Dr. Rajesh Khanna, offers comprehensive vision correction services including LASIK, SMILE laser, EVO ICL, and other advanced procedures. Please contact us directly at (805) 327-5758 or visit our website for more detailed information."
            model_used = "none"
        else:
            # Generate answer with fallback logic
            response, model_used = generate_answer_with_fallback(request.query, hits)

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
                error_msg = "I encountered an issue while processing your booking. Please try again or contact us directly at (805) 327-5758."
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
            error_msg = "The booking system is taking longer than expected. Please try again in a moment or contact us directly at (805) 327-5758."
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
            error_msg = f"I encountered an issue while booking your appointment. Please try again or contact us directly at (805) 327-5758."
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
            error_msg = "I encountered an issue while booking your appointment. Please try again or contact us directly at (805) 327-5758."
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
        error_msg = "An unexpected error occurred. Please try again or contact us at (805) 327-5758."
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
        # Search for relevant documents
        hits = search_opensearch(request.query, top_k=5)

        if not hits:
            # Even with no hits, provide helpful response about Khanna Institute
            response = "I don't have enough specific information about that in my knowledge base. However, I can tell you that Khanna Vision Institute, led by Dr. Rajesh Khanna, offers comprehensive vision correction services including LASIK, SMILE laser, EVO ICL, and other advanced procedures. Please contact us directly at (805) 327-5758 or visit our website for more detailed information."
            model_used = "none"
        else:
            # Generate answer with fallback logic
            response, model_used = generate_answer_with_fallback(request.query, hits)

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
        # Get request body as JSON
        body = await request.json()
        
        # DEBUG: Log the full request body to understand Vapi's format
        # This is CRITICAL for debugging - log everything Vapi sends
        print(f"\n{'='*80}")
        print(f"[VAPI WEBHOOK] Received request at {datetime.now().isoformat()}")
        print(f"[VAPI WEBHOOK] Full request body: {json.dumps(body, indent=2)}")
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

        # Initialize or get cancel session
        if session_id not in cancel_sessions:
            cancel_sessions[session_id] = {
                'in_cancel_flow': False,
                'fullName': None,
                'age': None,
                'procedure': None,
                'email': None
            }
        cancel_session_data = cancel_sessions[session_id]
        
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
                'time': None
            }
        
        session_data = booking_sessions[session_id]
        
        # Check cancel intent FIRST (before booking)
        is_cancel_intent = detect_cancel_intent(message) or cancel_session_data['in_cancel_flow']
        
        # Check if user wants to book or is already in booking flow (but NOT if they want to cancel)
        is_booking_intent = (not is_cancel_intent) and (detect_booking_intent(message) or session_data['in_booking_flow'])
        
        # Debug logging
        print(f"[Vapi Webhook] Session: {session_id}, Message: {message}, Cancel: {is_cancel_intent}, Booking: {is_booking_intent}")
        
        if is_cancel_intent:
            # Cancel appointment flow
            cancel_session_data['in_cancel_flow'] = True
            if not cancel_session_data.get('fullName'):
                extracted_name = extract_name(message)
                if extracted_name:
                    cancel_session_data['fullName'] = extracted_name
            if not cancel_session_data.get('age'):
                extracted_age = extract_age(message)
                if extracted_age:
                    cancel_session_data['age'] = extracted_age
            if not cancel_session_data.get('procedure'):
                extracted_proc = extract_procedure(message)
                if extracted_proc:
                    cancel_session_data['procedure'] = extracted_proc
            if not cancel_session_data.get('email'):
                extracted_email = extract_email(message)
                if extracted_email:
                    cancel_session_data['email'] = extracted_email
            required_cancel = ['fullName', 'age', 'procedure', 'email']
            missing_cancel = [f for f in required_cancel if not cancel_session_data.get(f)]
            if not missing_cancel:
                try:
                    await submit_cancel({
                        'fullName': cancel_session_data['fullName'],
                        'age': cancel_session_data['age'],
                        'procedure': cancel_session_data['procedure'],
                        'email': cancel_session_data['email']
                    })
                    cancel_sessions[session_id] = {'in_cancel_flow': False, 'fullName': None, 'age': None, 'procedure': None, 'email': None}
                    answer = "I've submitted your appointment cancellation request. Our team will process it shortly and you'll receive a confirmation. Is there anything else I can help you with?"
                    model_used = "cancel_submitted"
                except Exception as e:
                    answer = f"I encountered an issue submitting your cancellation. Please call us directly at (310) 482-1240 or (805) 230-2126. Error: {str(e)}"
                    model_used = "cancel_error"
            else:
                next_q = get_next_cancel_question(cancel_session_data)
                answer = next_q or "I need a bit more information to process your cancellation."
                model_used = "cancel_collection"
            log_interaction(f"[Vapi Cancel Session: {session_id}] {message}", answer, model_used, 0)
        elif is_booking_intent:
            # Enter or continue booking flow
            session_data['in_booking_flow'] = True
            
            # Extract information from current message
            if not session_data.get('fullName'):
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
                    booking_result = await submit_booking({
                        'fullName': session_data['fullName'],
                        'age': session_data['age'],
                        'email': session_data['email'],
                        'phone': session_data['phone'],
                        'location': session_data['location'],
                        'date': session_data['date'],
                        'time': session_data['time']
                    })
                    
                    # Clear booking session
                    booking_sessions[session_id] = {
                        'in_booking_flow': False,
                        'fullName': None,
                        'age': None,
                        'email': None,
                        'phone': None,
                        'location': None,
                        'date': None,
                        'time': None
                    }
                    
                    answer = f"Perfect! I've scheduled your consultation for {session_data['date']} at {session_data['time']} at our {session_data['location']} location. You'll receive a confirmation email at {session_data['email']} shortly. Is there anything else I can help you with?"
                    model_used = "booking_submitted"
                    
                except Exception as e:
                    answer = f"I encountered an issue submitting your booking. Please try again or call us directly at (805) 327-5758. Error: {str(e)}"
                    model_used = "booking_error"
            else:
                # Ask for next missing piece of information
                next_question = get_next_booking_question(session_data)
                answer = next_question or "I need a bit more information to complete your booking."
                model_used = "booking_collection"
            
            # Log booking interaction
            log_interaction(f"[Vapi Booking Session: {session_id}] {message}", answer, model_used, 0)
            
        else:
            # Regular Q&A flow
            # First, try to match against FAQ knowledge base
            faq_result = match_faq(message, threshold=0.5)
            
            if faq_result:
                answer, confidence = faq_result
                model_used = f"faq_match_{confidence:.2f}"
                print(f"[FAQ MATCH] Confidence: {confidence:.2f} - Using FAQ answer")
                hits = []
            else:
                # Process through RAG pipeline
                hits = search_opensearch(message, top_k=5)

                if not hits:
                    # Even with no hits, provide helpful response about Khanna Institute
                    answer = "I don't have enough specific information about that in my knowledge base. However, I can tell you that Khanna Vision Institute, led by Dr. Rajesh Khanna, offers comprehensive vision correction services including LASIK, SMILE laser, EVO ICL, and other advanced procedures. Please contact us directly at (805) 327-5758 or visit our website for more detailed information."
                    model_used = "none"
                else:
                    answer, model_used = generate_answer_with_fallback(message, hits)

            # Log the interaction
            log_interaction(f"[Vapi Session: {session_id}] {message}", answer, model_used, len(hits))

        # Return in Vapi-expected format
        # For assistant-request events, Vapi expects this format
        response = {
            "messages": [
                {
                    "type": "text",
                    "text": answer
                }
            ]
        }
        
        # If this is an assistant-request event, include toolCallId if present
        if event_type == 'assistant-request' and 'toolCallId' in body:
            response['toolCallId'] = body['toolCallId']
        
        # DEBUG: Log response
        print(f"[VAPI WEBHOOK] Sending response: {json.dumps(response, indent=2)}")
        
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
