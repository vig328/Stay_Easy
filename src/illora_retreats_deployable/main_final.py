# main_final.py
## importing essential libraries

import os
import uuid
import json
import random
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
import httpx
import requests
import re
from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import Config

# Project imports (kept)
import web_ui_final as web
from services.intent_classifier import classify_intent
from logger import log_chat
from services.qa_agent import ConciergeBot
from services.payment_gateway import (
    create_checkout_session,
    create_addon_checkout_session,
    create_pending_checkout_session,
)

# Illora checkin app / models
from illora.checkin_app.models import Room, Booking, BookingStatus
from illora.checkin_app.pricing import calculate_price_for_room as calculate_price
from illora.checkin_app.database import Base, engine, SessionLocal
from illora.checkin_app.booking_flow import create_booking_record
from illora.checkin_app.chat_models import ChatMessage

from sqlalchemy import func
from sqlalchemy.orm import Session

#########################################################################

# ------------------------- Logging setup -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------- FastAPI app -------------------------
app = FastAPI(title="AI Chieftain API", version="1.0.0")

# ------------------------- Constants -------------------------
CLIENT_WORKFLOW_SHEET = "Client_workflow"

# ------------------------- CORS -------------------------

FRONTEND_ORIGINS = [
    "http://localhost:8080",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173","https://ilora-demo-799523984969.us-central1.run.app/"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ------------------------- Static files -------------------------
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ------------------------- Concierge bot -------------------------
bot = ConciergeBot()

# In-memory user session store
USER_SESSIONS: Dict[str, Dict[str, Any]] = {}

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
SERVICE_ACCOUNT_FILE = "E:\\ilora_case_study-main\\src\\illora_retreats_deployable\\service_account.json"

creds = Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
gc = gspread.authorize(creds)

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from config import Config

# Create client
def get_gsheet_client():
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(Config.SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    return client

# Open a worksheet
def get_worksheet(sheet_name: str):
    client = get_gsheet_client()
    sh = client.open_by_key(Config.GSHEET_ID)
    ws = sh.worksheet(sheet_name)
    return ws

# Fetch all rows
def fetch_sheet_data(sheet_name: str):
    ws = get_worksheet(sheet_name)
    return ws.get_all_records()  # list of dicts

# Fetch a row by email
def fetch_client_row_from_sheet_by_email(email: str):
    rows = fetch_sheet_data("Client_workflow")
    target = email.strip().lower()
    for row in rows:
        if str(row.get("Email", "")).strip().lower() == target:
            return row
    return None

# Append a row
def add_row_to_sheet(sheet_name: str, row_data: dict):
    ws = get_worksheet(sheet_name)
    ws.append_row(list(row_data.values()))
    return {"success": True}

# Update user row (replace update_user_in_sheet)
def update_user_in_sheet(email, name=None, room_no=None, orders=None, pending_balance=None, workflow_stage=None):
    ws = get_worksheet("Client_workflow")
    all_rows = ws.get_all_records()
    for i, row in enumerate(all_rows, start=2):  # skip header, gspread index starts at 1
        if str(row.get("Email", "")).strip().lower() == email.strip().lower():
            if name is not None:
                ws.update_cell(i, row.keys().index("Name")+1, name)
            if room_no is not None:
                ws.update_cell(i, row.keys().index("Room Alloted")+1, room_no)
            if orders is not None:
                ws.update_cell(i, row.keys().index("Orders")+1, orders)
            if pending_balance is not None:
                ws.update_cell(i, row.keys().index("Pending Balance")+1, pending_balance)
            if workflow_stage is not None:
                ws.update_cell(i, row.keys().index("Workflow Stage")+1, workflow_stage)
            return {"success": True}
    return {"success": False, "error": "User not found"}

def ticket_exists(room_no: str, message: str):
    rows = fetch_sheet_data(TICKET_SHEET_NAME) or []
    msg = message.strip().lower()

    for r in rows[-20:]:
        if (
            r.get("Room No") == room_no and
            r.get("Request/Query", "").strip().lower() == msg and
            r.get("Status") in ["Open", "In Progress"]
        ):
            return r.get("Ticket ID")
    return None



###########################################################################
# ------------------------- Models -------------------------
class SignupReq(BaseModel):
    name: str = Field(..., min_length=2)
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)
    phoneNo: str = Field(default="")

class LoginReq(BaseModel):
    username: str = Field(..., description="User's username/email")
    password: str = Field(..., description="User's password")
    remember: bool = Field(default=True)

class UpdateWorkflowReq(BaseModel):
    username: str
    stage: str
    booking_id: Optional[str] = None
    id_proof_link: Optional[str] = None

class MeReq(BaseModel):
    username: Optional[str] = None
    remember_token: Optional[str] = None

###########################################################################
# ------------------------- Helpers / debug utils -------------------------
def _normalize_key(k: Any) -> str:
    return "".join(ch.lower() for ch in str(k) if ch.isalnum())

def _parse_float(val: Any) -> float:
    if val is None or str(val).strip() == "":
        return 0.0
    try:
        s = str(val).strip().replace("$", "").replace(",", "")
        return float(s)
    except Exception:
        return 0.0

def _short(s: str, n: int = 400) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + " ...[truncated]"

def get_first_value(d: Dict[str, Any], candidates: List[str], default: Any = "") -> Any:
    if not d:
        return default
    for k in candidates:
        if k in d and d[k] not in (None, ""):
            return d[k]
    lowered = {str(k).lower(): v for k, v in d.items() if v not in (None, "")}
    for k in candidates:
        if k.lower() in lowered:
            return lowered[k.lower()]
    return default

def map_sheet_row_to_user_details(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map exact columns to the frontend-friendly object used by HotelSidebar."""
    if not row:
        return {}
    # Keep exact keys as in sheet when useful
    uid = row.get("Client Id", "") or row.get("ClientId", "") or row.get("client_id", "")
    booking_status = row.get("Workfow Stage", "") or row.get("Workflow Stage", "") or row.get("Booking Status", "") or "Not Booked"
    # id proof may be a status or a link
    id_proof = row.get("Id Link", "") or row.get("IdLink", "") or row.get("ID Proof", "") or ""
    pending_balance = _parse_float(row.get("Pending Balance", 0) or row.get("Balance", 0) or 0)
    status = row.get("Status", "") or booking_status or "Still"
    room_number = row.get("Room Alloted", "") or row.get("Room Number", "") or ""
    check_in = row.get("CheckIn", "") or row.get("Check In", "")
    check_out = row.get("Check Out", "") or row.get("CheckOut", "")

    return {
        "uid": uid,
        "bookingStatus": booking_status,
        "bookingId": row.get("Booking Id", "") or row.get("BookingId", "") or "",
        "idProof": id_proof,
        "pendingBalance": pending_balance,
        "status": status,
        "roomNumber": room_number,
        "checkIn": check_in,
        "checkOut": check_out,
        "_raw_row": {k: (v if _normalize_key(k) != "password" else "****") for k, v in (row or {}).items()}
    }

def normalize_raw_user_data(raw_user_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalized snake_case mapping (useful for other endpoints)."""
    return {
        "client_id": raw_user_data.get("Client Id", "") or raw_user_data.get("ClientId", ""),
        "name": raw_user_data.get("Name", "") or raw_user_data.get("Full Name", ""),
        "email": raw_user_data.get("Email", ""),
        "booking_id": raw_user_data.get("Booking Id", ""),
        "workflow_stage": raw_user_data.get("Workfow Stage", "") or raw_user_data.get("Workflow Stage", ""),
        "room_alloted": raw_user_data.get("Room Alloted", ""),
        "check_in": raw_user_data.get("CheckIn", "") or raw_user_data.get("Check In", ""),
        "check_out": raw_user_data.get("Check Out", "") or raw_user_data.get("CheckOut", ""),
        "id_link": raw_user_data.get("Id Link", ""),
        "pending_balance": _parse_float(raw_user_data.get("Pending Balance", 0)),
        "status": raw_user_data.get("Status", "") or raw_user_data.get("Workfow Stage", "")
    }

def _update_session_from_raw(username: str, raw_user_data: Dict[str, Any], remember_token: Optional[str] = None):
    normalized = normalize_raw_user_data(raw_user_data)
    frontend_view = map_sheet_row_to_user_details(raw_user_data)
    USER_SESSIONS[username] = {
        "normalized": normalized,
        "raw": raw_user_data,
        "frontend": frontend_view,
        "last_login": datetime.utcnow().isoformat() + "Z",
        "remember_token": remember_token or USER_SESSIONS.get(username, {}).get("remember_token"),
    }
    logger.debug("Session for %s updated/saved (sanitized): %s", username, json.dumps({
        "normalized": normalized,
        "frontend": frontend_view,
        "last_login": USER_SESSIONS[username]["last_login"],
        "remember_token": USER_SESSIONS[username]["remember_token"]
    }, default=str))

###########################################################################
# ------------------------- Sheets helpers (with debug) -------------------------
def fetch_client_row_from_sheet_by_email(email: str) -> Optional[Dict[str, Any]]:
    try:
        sh = gc.open_by_key(Config.GSHEET_ID)
        worksheet = sh.worksheet(CLIENT_WORKFLOW_SHEET)
        records = worksheet.get_all_records()
        for row in records:
            if str(row.get("Email", "")).strip().lower() == email.strip().lower():
                return row
        return None
    except Exception as e:
        logger.exception("fetch_client_row_from_sheet_by_email failed")
        return None


import requests
import logging
import requests
import logging

import requests
import json

import requests
import json

def update_user_in_sheet(email, name, room_no, orders, pending_balance):
    try:
        sh = gc.open_by_key(Config.GSHEET_ID)
        worksheet = sh.worksheet(CLIENT_WORKFLOW_SHEET)
        records = worksheet.get_all_records()
        headers = worksheet.row_values(1)
        for i, row in enumerate(records, start=2):  # start=2 because first row is headers
            if str(row.get("Email", "")).strip().lower() == email.strip().lower():
                update_values = {
                    "Name": name,
                    "Room No": room_no,
                    "Orders": orders,
                    "Pending Balance": pending_balance
                }
                for col_name, val in update_values.items():
                    if col_name in headers:
                        col_index = headers.index(col_name) + 1
                        worksheet.update_cell(i, col_index, val)
                return {"success": True}
        return {"success": False, "error": "Email not found"}
    except Exception as e:
        logger.exception("update_user_in_sheet failed")
        return {"success": False, "error": str(e)}




###########################################################################
# ------------------------- Endpoints (login/signup/update + debug) -------------------------

@app.post("/auth/login", tags=["authentication"])
async def login(req: LoginReq = Body(...)):
    raw_user_data = fetch_client_row_from_sheet_by_email(req.username)
    if not raw_user_data:
        raise HTTPException(status_code=401, detail="Invalid credentials or user not found")

    # session creation continues as before
    frontend_view = map_sheet_row_to_user_details(raw_user_data)
    normalized = normalize_raw_user_data(raw_user_data)
    token = uuid.uuid4().hex if req.remember else None
    _update_session_from_raw(req.username, raw_user_data, remember_token=token)

    return {
        "username": req.username,
        "remember_token": token,
        "userData": {
            "raw": USER_SESSIONS[req.username]["raw"],
            "normalized": USER_SESSIONS[req.username]["normalized"],
            "frontend": USER_SESSIONS[req.username]["frontend"],
        }
    }



@app.post("/auth/signup", tags=["authentication"])
async def signup(req: SignupReq = Body(...)):
    client_id = f"ILR-{datetime.utcnow().year}-{random.randint(1000,9999)}"
    workflow_stage = "Not Booked"
    row_data = {
        "Client Id": client_id,
        "Name": req.name,
        "Email": req.username,
        "Password": req.password,
        "Booking Id": "",
        "Workflow Stage": workflow_stage,
        "Room Alloted": "",
        "CheckIn": "",
        "Check Out": "",
        "Id Link": "",
        "Orders": "",
        "Pending Balance": 0
    }
    add_row_to_sheet("Client_workflow", row_data)
    return {"success": True, "workflowStage": workflow_stage, "clientId": client_id, "message": "Registration successful"}



@app.post("/auth/update-workflow", tags=["authentication"])
async def update_workflow(req: UpdateWorkflowReq = Body(...)):
    existing_row = fetch_client_row_from_sheet_by_email(req.username)
    if not existing_row:
        raise HTTPException(status_code=404, detail="User not found")

    update_user_in_sheet(
        email=req.username,
        name=existing_row.get("Name"),
        room_no=existing_row.get("Room Alloted"),
        orders=existing_row.get("Orders", ""),
        pending_balance=existing_row.get("Pending Balance", 0),
        workflow_stage=req.stage
    )

    return {"success": True, "message": f"Workflow stage updated to {req.stage}"}



@app.get("/auth/session/{username}", tags=["authentication"])
def get_session(username: str):
    logger.debug("get_session called for %s", username)
    session = USER_SESSIONS.get(username)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # return a sanitized session for frontend
    return {"username": username, "session": {"normalized": session["normalized"], "frontend": session["frontend"], "last_login": session["last_login"]}}


@app.post("/auth/logout", tags=["authentication"])
def logout(username: str = Body(..., embed=True)):
    logger.info("logout called for %s", username)
    if username in USER_SESSIONS:
        del USER_SESSIONS[username]
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Session not found")


@app.post("/auth/me", tags=["authentication"])
def me_post(body: MeReq = Body(...)):
    logger.debug("me_post called with body=%s", body.dict())
    if body.remember_token:
        for uname, sess in USER_SESSIONS.items():
            if sess.get("remember_token") == body.remember_token:
                logger.debug("me_post: found session by token for %s", uname)
                return {"username": uname, "session": sess}
        raise HTTPException(status_code=404, detail="Session not found for provided token")
    if body.username:
        sess = USER_SESSIONS.get(body.username)
        if not sess:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"username": body.username, "session": sess}
    raise HTTPException(status_code=400, detail="Provide either username or remember_token")

@app.get("/auth/me", tags=["authentication"])
def get_current_user(request: Any):
    session_token = request.cookies.get("session_token")
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    print()
    print(USER_SESSIONS)
    print()

    # Find the user from sessions
    for username, sess in USER_SESSIONS.items():
        if sess.get("session_token") == session_token:
            return {
                "username": username,
                "frontend": sess.get("frontend", {}),
                "normalized": sess.get("normalized", {}),
                "last_login": sess.get("last_login"),
            }

    raise HTTPException(status_code=404, detail="User not found")


# ------------------------- New endpoint: return all sessions (sanitized) -------------------------
@app.get("/auth/sessions", tags=["authentication"])
def get_all_sessions():
    """
    Return all active user sessions in-memory.
    Raw rows are returned but any 'password' key is masked.
    Useful for frontend admin display or session debugging.
    """
    print()
    print(USER_SESSIONS)
    print()

    logger.debug("get_all_sessions called - returning %d sessions", len(USER_SESSIONS))
    safe_sessions: Dict[str, Dict[str, Any]] = {}
    for username, s in USER_SESSIONS.items():
        raw_s = s.get("raw", {}) or {}
        # mask any password-like keys
        raw_s_sanitized = {k: ("****" if _normalize_key(k) == "password" else v) for k, v in raw_s.items()}
        safe_sessions[username] = {
            "raw": raw_s_sanitized,
            "normalized": s.get("normalized", {}),
            "frontend": s.get("frontend", {}),
            "last_login": s.get("last_login"),
            "remember_token": s.get("remember_token"),
        }
    return {"sessions": safe_sessions}


###########################################################################

logger.info("Initial USER_SESSIONS snapshot (sanitized): %s", {k: {"last_login": v.get("last_login"), "client_id": v.get("normalized", {}).get("client_id")} for k, v in USER_SESSIONS.items()})

################### Now Lets Move to the main part (responses) #####################


DEMO_ROOM_TYPES = ["Luxury Tent"]
sample_bookings: List[Dict[str, Any]] = []

TICKET_SHEET_NAME = getattr(Config, "GSHEET_TICKET_SHEET", "ticket_management")
GUEST_LOG_SHEET_NAME = getattr(Config, "GSHEET_GUEST_LOG_SHEET", "guest_interaction_log")
MENU_SHEET_NAME = getattr(Config, "GSHEET_MENU_SHEET", "menu_manager")
menu_rows: List[Dict[str, Any]] = []

# ------------- Generic helper to push a row to any sheet via your Apps Script Web App -------------
def push_row_to_sheet(sheet_name: str, row_data: Dict[str, Any]) -> Dict[str, Any]:
    sh = gc.open_by_key(Config.GSHEET_ID)
    worksheet = sh.worksheet(sheet_name)
    headers = worksheet.row_values(1)
    row = [row_data.get(h, "") for h in headers]
    worksheet.append_row(row)
    return {"success": True}



# ------------- Guest interaction logging helper -------------
def _naive_sentiment(message: str) -> str:
    """Very small sentiment heuristic (optional). Returns 'positive' / 'negative' / ''."""
    if not message:
        return ""
    m = message.lower()
    negative_words = ["not", "no", "never", "bad", "disappointed", "angry", "hate", "worst", "problem", "issue", "delay"]
    positive_words = ["good", "great", "awesome", "excellent", "happy", "love", "enjoy"]
    if any(w in m for w in negative_words) and not any(w in m for w in positive_words):
        return "negative"
    if any(w in m for w in positive_words) and not any(w in m for w in negative_words):
        return "positive"
    return ""

def create_guest_log_row(req_session_id: Optional[str], email: Optional[str], user_input: str, bot_response: str,
                         intent: str, is_guest_flag: bool, ref_ticket_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Build a row matching headers:
    Log ID | Timestamp | Source | Session ID | Guest Email | Guest Name | User Input | Bot Response | Intent | Guest Type | Sentiment | Reference Ticket ID | Conversation URL
    """
    log_id = f"LOG-{random.randint(1000,999999)}"
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    source = "web"
    session_id = req_session_id or ""
    guest_email = email or ""
    guest_name = "Guest"
    user_input_val = user_input or ""
    bot_response_val = bot_response or ""
    intent_val = intent or ""
    guest_type = "guest" if bool(is_guest_flag) else "non-guest"
    sentiment = _naive_sentiment(user_input)
    reference_ticket_id = ref_ticket_id or ""
    conversation_url = ""  # optional — left blank or build if you have a UI link

    return {
        "Log ID": log_id,
        "Timestamp": timestamp,
        "Source": source,
        "Session ID": session_id,
        "Guest Email": guest_email,
        "Guest Name": guest_name,
        "User Input": user_input_val,
        "Bot Response": bot_response_val,
        "Intent": intent_val,
        "Guest Type": guest_type,
        "Sentiment": sentiment,
        "Reference Ticket ID": reference_ticket_id,
        "Conversation URL": conversation_url,
    }

WIFI_PROBLEM_WORDS = [
    "not working",
    "not connecting",
    "no internet",
    "slow",
    "issue",
    "problem",
    "disconnected",
    "down",
    "unstable",
    "poor signal"
]

QNA_INTENTS = {
    "wifi_password",
    "faq",
    "general_query",
    "checkin_time",
    "checkout_time",
    "amenities_info",
    "greeting"
}

def is_wifi_issue(message: str) -> bool:
    msg = message.lower()
    if "wifi" not in msg and "internet" not in msg:
        return False
    return any(word in msg for word in WIFI_PROBLEM_WORDS)


def is_ticket_request(message: str, intent: str, addon_matches: list = None) -> bool:
    msg = message.lower()

    # 🚫 HARD BLOCK — QnA can NEVER create tickets
    if intent in QNA_INTENTS:
        return False

    # ✅ WiFi problems only
    if is_wifi_issue(msg):
        return True

    # Other engineering issues
    engineering_keywords = [
        "water leak",
        "ac not working",
        "tv not working",
        "power cut",
        "light not working",
        "geyser not working"
    ]
    if any(k in msg for k in engineering_keywords):
        return True

    # Service / booking requests
    service_keywords = [
        "book",
        "order",
        "room service",
        "housekeeping",
        "spa",
        "laundry",
        "wake up call"
    ]
    if any(k in msg for k in service_keywords):
        return True

    return False


def classify_ticket_category(message: str) -> str:
    """Map message content to a ticket category."""
    m = message.lower()
    if any(w in m for w in ["coffee", "tea", "drink", "food", "meal", "snack", "beverage", "breakfast", "lunch", "dinner"]):
        return "Food"
    if any(w in m for w in ["towel", "clean", "housekeeping", "room service", "bed", "makeup", "turn down", "linen"]):
        return "Room Service"
    if any(w in m for w in ["ac", "wifi", "tv", "light", "repair", "engineer", "fix", "leak", "broken", "toilet", "plumb", "electr"]):
        return "Engineering"
    return "General"

def assign_staff_for_category(category: str) -> str:
    return {
        "Food": "Food Staff",
        "Room Service": "Room Service",
        "Engineering": "Engineering",
        "General": "Front Desk"
    }.get(category, "Front Desk")

def create_ticket_row_payload(message: str, email: str = None) -> Dict[str, str]:
    """
    Build the exact rowData dict matching your sheet's headers:
    Ticket ID | Guest Name | Room No | Request/Query | Category | Assigned To | Status | Created At | Resolved At | Notes
    
    Note: This function now tries to get the actual room number from Client_workflow sheet if possible
    """

    # get the latest session (key + object)
    session_key, session_obj = get_latest_session(USER_SESSIONS)
    # Get actual room number from Client_workflow sheet if possible

    print("\nSelected session_key:", session_key)
    print("Selected session_obj (sanitized):")
    if session_obj:
        # mask password in raw if present for logging
        raw_preview = {k: ("****" if re.sub(r"[^a-zA-Z0-9]", "", str(k)).lower() == "password" else v)
                       for k, v in (session_obj.get("raw", {}) or {}).items()}
        print("normalized:", session_obj.get("normalized"))
        print("frontend:", session_obj.get("frontend"))
        print("raw (masked):", raw_preview)
    else:
        print("No session selected (session_obj is None)")
    print()



    normalized = session_obj.get("normalized") if session_obj else {}

    guest_email = normalized.get("email") if isinstance(normalized, dict) else None
    room_no = normalized.get("room_alloted") if isinstance(normalized, dict) else None

    
    ticket_id = f"TCK-{random.randint(1000, 99999)}"
    guest_name = guest_email 
    category = classify_ticket_category(message)
    assigned_to = assign_staff_for_category(category)
    status = "In Progress"
    created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    resolved_at = ""  # empty initially
    notes = message

    # Use exact header names present in the spreadsheet (case + spaces matter for the Apps Script mapping)
    # Return the ticket data with the room number
    return {
        "Ticket ID": ticket_id,
        "Guest Name": guest_email,
        "Room No": room_no,  # Now using actual room number from Client_workflow sheet
        "Request/Query": message,
        "Category": category,
        "Assigned To": assigned_to,
        "Status": status,
        "Created At": created_at,
        "Resolved At": resolved_at,
        "Notes": notes
    }


import os
import uuid
import json
import random
import logging
import asyncio
import requests
from typing import List, Optional, Dict, Any, Generator
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Query, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from config import Config

# ------------------------- SSE Broker -------------------------
class EventBroker:
    def __init__(self):
        self.connections: List[asyncio.Queue] = []

    async def connect(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.connections.append(q)
        return q

    async def disconnect(self, q: asyncio.Queue):
        if q in self.connections:
            try:
                self.connections.remove(q)
            except Exception:
                pass

    async def broadcast(self, event: str, data: Dict[str, Any]):
        msg = json.dumps({"event": event, "data": data}, default=str)
        for q in list(self.connections):
            try:
                await q.put(msg)
            except Exception:
                try:
                    self.connections.remove(q)
                except Exception:
                    pass

broker = EventBroker()

@app.get("/events")
async def sse_events(request: Request):
    async def event_generator(q: asyncio.Queue):
        try:
            await q.put(json.dumps({"event": "connected", "data": {}}))
            while True:
                if await request.is_disconnected():
                    break
                msg = await q.get()
                yield f"data: {msg}\n\n"
        finally:
            await broker.disconnect(q)

    q = await broker.connect()
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(event_generator(q), headers=headers, media_type="text/event-stream")



# ------------------------- Pydantic Models -------------------------
class ChatReq(BaseModel):
    message: str
    is_guest: Optional[bool] = False
    session_id: Optional[str] = None
    email: Optional[str] = None

class ChatActions(BaseModel):
    show_booking_form: bool = False
    addons: List[str] = Field(default_factory=list)
    payment_link: Optional[str] = None
    pending_balance: Optional[Dict[str, Any]] = None

class ChatResp(BaseModel):
    reply: str
    reply_parts: Optional[List[str]] = None
    intent: Optional[str] = None
    actions: ChatActions = Field(default_factory=ChatActions)
    pending_balance: Optional[float] = 0.0   # <-- ✅ Add this line



########## helper functions ##########

def _fetch_sheet_data(self, sheet_name: str) -> List[Dict[str, Any]]:
    """
    Calls the deployed Apps Script web app and returns the list of row objects for the sheet.
    The web app must implement ?action=getSheetData&sheet=<sheetName> (matching your provided Apps Script).
    """
    if not self.sheet_api:
        raise RuntimeError("GSHEET_WEBAPP_URL is not configured in Config.")

    params = {"action": "getSheetData", "sheet": sheet_name}
    try:
        resp = requests.get(self.sheet_api, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Data should be a list of objects (one per row). If the webapp returns {error:...}, raise.
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Sheets webapp returned error: {data.get('error')}")
        if not isinstance(data, list):
            # sometimes the webapp might wrap results; be permissive
            raise RuntimeError("Unexpected sheet response format (expected list of row objects).")
        return data
    except Exception as e:
        logger.error(f"Error fetching sheet '{sheet_name}' from {self.sheet_api}: {e}")
        raise

'''
# -------------------- Updated /chat endpoint (drop-in replacement) --------------------
menu = _fetch_sheet_data(MENU_SHEET_NAME) or []
menu_text = ""
if menu:
    menu_text = "\n\n\n📣 **Menu / Item / Price / Description:**\n"
    for c in menu[:]:
        # try to pick a title/description
        type = c.get("Type") or c.get("Type") or c.get("Type") or c.get("Type") or ""
        item = c.get("Item") or c.get("Item") or c.get("Item") or ""
        price = c.get("Price") or "0"
        desc = c.get("Descripton") or ""
        if type or desc:
            menu_text += f"- {type} {('- ' + desc) if desc else ''} {('- ' + item) if item else ''} {('- ' + price) if price else ''} {('- ' + desc) if desc else ''}\n"
'''

menu = []

from datetime import datetime
from typing import Tuple, Optional, Dict, Any

# --- Helper: pick latest session by last_login ---
def get_latest_session(user_sessions: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Return (session_key, session_obj) for the session with the newest last_login.
    If none found, returns (None, None).
    """
    if not user_sessions:
        return None, None

    latest_key = None
    latest_ts = None

    for key, sess in user_sessions.items():
        last_login_str = sess.get("last_login")
        if last_login_str:
            try:
                # handle trailing Z
                ts = datetime.fromisoformat(last_login_str.replace("Z", "+00:00"))
            except Exception:
                ts = None
        else:
            ts = None

        # treat missing timestamps as very old so they won't override real ones
        if ts is None:
            continue

        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
            latest_key = key

    # If we didn't find any with parseable timestamps but there are sessions, pick an arbitrary one
    if latest_key is None and len(user_sessions) > 0:
        # pick last inserted key (stable for dicts in Python 3.7+)
        try:
            latest_key = next(reversed(user_sessions))
        except Exception:
            latest_key = next(iter(user_sessions))

    if latest_key:
        return latest_key, user_sessions.get(latest_key)
    return None, None


# --- Corrected /chat endpoint ---
import requests
import logging
import time

logger = logging.getLogger(__name__)
from config import Config
WEBAPPURL = getattr(Config, "GSHEET_WEBAPP_URL", "")

def add_item_to_ticket(session_obj, item_name, item_price):
    """
    Add an item to the guest's ticket and update the pending balance in Google Sheet.
    """
    logger.info("Adding item %s for $%s to ticket", item_name, item_price)

    try:
        if not session_obj or not isinstance(session_obj.get("normalized"), dict):
            logger.warning("Invalid session object provided")
            return

        # Get current pending balance safely
        current_pending = session_obj["normalized"].get("pending_balance", 0.0) or 0.0
        new_pending = current_pending + item_price

        # Update normalized + frontend balances
        session_obj["normalized"]["pending_balance"] = new_pending
        session_obj["frontend"]["pendingBalance"] = new_pending

        # Handle ticket creation/update
        ticket = session_obj.get("ticket")
        if ticket:
            ticket["total_amount"] = ticket.get("total_amount", 0) + item_price
        else:
            ticket_id = f"TCK-{int(time.time())}"
            session_obj["ticket"] = {
                "ticket_id": ticket_id,
                "guest_name": session_obj["normalized"].get("name", ""),
                "room_no": session_obj["normalized"].get("room_alloted", ""),
                "total_amount": item_price
            }

        logger.info("Updated pending balance in session: %s", new_pending)

        # Prepare payload for Google Sheet
        ticket_data = session_obj.get("ticket")
        if ticket_data and ticket_data.get("ticket_id"):
            update_payload = {
                "ticket_id": ticket_data["ticket_id"],
                "pending_balance": session_obj["normalized"]["pending_balance"],
                "guest_name": ticket_data.get("guest_name", ""),
                "room_no": ticket_data.get("room_no", ""),
                "total_amount": ticket_data.get("total_amount", 0)
            }

            resp = requests.post(
                WEBAPPURL,
                json={"action": "update_pending_balance", "data": update_payload},
                timeout=10
            )

            if resp.ok:
                logger.info("Pending balance updated in Google Sheet for ticket %s", ticket_data["ticket_id"])
            else:
                logger.warning("Failed to update Google Sheet: %s", resp.text)

        # Debug prints
        print(">>> After add_item_to_ticket:")
        print("Pending balance (normalized):", session_obj["normalized"].get("pending_balance"))
        print("Pending balance (frontend):", session_obj["frontend"].get("pendingBalance"))

    except Exception as e:
        logger.exception("Error updating ticket pending balance: %s", str(e))

from typing import Optional
import logging

@app.post("/chat", response_model=ChatResp)
async def chat(req: ChatReq):
    user_input = req.message or ""
    print("[CHAT] User input:", user_input)

    # Determine guest flag
    is_guest = bool(getattr(req, "is_guest", False))

    # Get latest session (key + object)
    session_key, session_obj = get_latest_session(USER_SESSIONS)
    print("[CHAT] Session object:", session_obj)

    # Get bot reply
    bot_reply_text = bot.ask(
        query=user_input,
        user_type="guest",
        user_session=session_obj,
        session_key=session_key
    )

    # Classify intent
    intent = classify_intent(user_input)

    # MENU AND BALANCE HANDLING
    AVAILABLE_EXTRAS = {}
    EXTRAS_PRICE_BY_KEY = {}

    for c in menu[:]:
        if c.get("Type") == "Complimentary":
            continue
        item = (c.get("Item") or "").strip().lower()
        try:
            price = float(c.get("Price") or 0)
        except Exception:
            price = 0.0
        if item:
            AVAILABLE_EXTRAS[item] = item
            EXTRAS_PRICE_BY_KEY[item] = price

    message_lower = user_input.lower()
    addon_matches = [k for k in AVAILABLE_EXTRAS if k in message_lower]

    # Initialize session fields safely
    if session_obj is None:
        session_obj = {}
    session_obj.setdefault("pending_balance", 0.0)
    session_obj.setdefault("orders", {})
    session_obj.setdefault("guest_name", getattr(req, "name", ""))
    session_obj.setdefault("room_no", getattr(req, "room_no", ""))

    # Track pending balance
    pending_balance = 0.0

    # Add new order items
    for item_name in addon_matches:
        item_price = EXTRAS_PRICE_BY_KEY.get(item_name, 0.0)
        print(f"[DEBUG] Adding recognized item: {item_name} | Price: {item_price}")
        add_item_to_ticket(session_obj, item_name, item_price)
        pending_balance += item_price
        session_obj["pending_balance"] += item_price

    print(f"[DEBUG] This order: ₹{pending_balance}")
    print(f"[DEBUG] Total pending balance: ₹{session_obj['pending_balance']}")

    # ✅ Prepare data for Google Sheet update
    guest_name = (
    session_obj.get("guest_name")
    or session_obj.get("normalized", {}).get("name", "Unknown")
)
    room_no = (
    session_obj.get("room_no")
    or session_obj.get("normalized", {}).get("room_alloted", "N/A")
)

    orders_str = ", ".join([f"{item} ({qty})" for item, qty in session_obj.get("orders", {}).items()])
    pending_balance_val = session_obj.get("pending_balance", 0.0)
    print(f"[DEBUG] Guest: {guest_name}, Room: {room_no}")

    # ✅ Update Google Sheet
    if req.email:
        print(f"[SHEET] Updating Google Sheet for {req.email}")
        update_user_in_sheet(
            email=req.email,
            name=guest_name,
            room_no=room_no,
            orders=orders_str,
            pending_balance=pending_balance_val
        )

    # Ticket creation
    created_ticket_id: Optional[str] = None
    try:
        existing_ticket = ticket_exists(room_no, user_input)

        if existing_ticket:
            created_ticket_id = existing_ticket
        elif is_ticket_request(user_input, intent, addon_matches):
            ticket_row = create_ticket_row_payload(user_input, req.email)
            push_row_to_sheet(TICKET_SHEET_NAME, ticket_row)
            created_ticket_id = ticket_row.get("Ticket ID")
    except Exception as e:
        logger.warning("Ticket error: %s", e)

    # Log chat
    log_chat("web", req.session_id or "", user_input, bot_reply_text, intent, is_guest)

    # Format reply parts
    reply_parts = bot_reply_text.split("\n\n") if isinstance(bot_reply_text, str) else [str(bot_reply_text)]

    # Actions (for pending balance display)
    actions = ChatActions()
    pending_balance_amount = session_obj.get("pending_balance", 0.0)
    if pending_balance_amount > 0:
        actions.pending_balance = {
            "amount": pending_balance_amount,
            "items": [{"description": "Room balance payment"}]
        }

    print("[CHAT] Returning response:", {
        "reply": bot_reply_text,
        "reply_parts": reply_parts,
        "intent": intent,
        "actions": actions.__dict__,
    })

    # Return structured chat response
    return ChatResp(
        reply=bot_reply_text,
        reply_parts=reply_parts,
        intent=intent,
        actions=actions
    )




# ------------------------- Run locally -------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_final:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=True)