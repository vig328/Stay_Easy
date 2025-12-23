# qa_agent.py (final, updated)
from typing import Optional, List, Dict, Any, Tuple
import google.generativeai as genai
from vector_store import create_vector_store
from config import Config
from services.gsheets_helper import get_all_records
from logger import setup_logger
import os
import json
import requests
import difflib
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import re
from collections import defaultdict
logger = setup_logger("QAAgent")
QNA_MIN_SCORE = 0.55
INTENTS = {
    "ORDER",
    "MENU",
    "TICKET",
    "QNA",
    "UNKNOWN",
    "SERVICE_REQUEST"
}


TICKET_KEYWORDS = [
    "leak", "leakage", "tap broken",
    "ac not working", "ac broken",
    "electricity", "power issue",
    "water issue",
    "damage",
    "not working",
    "complaint"
]



def _normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


class ConciergeBot:
    """
    Concierge QA Agent with:
      - Sheet + vector store retrieval (with timeouts + fallbacks)
      - Safe chat history persistence (background threads)
      - Prompt building from sheets, campaigns, dos/donts, menu, user session
      - Debug logging for tracing
    """

    def classify_intent_with_llm(self, user_message: str) -> dict:
        """
        Uses LLM to classify user intent and extract entities.
        Returns JSON with intent and optional entities.
        """
        prompt = f"""
You are an intent classification engine for a hotel assistant.

Classify the user intent ONLY. Do not answer.

Allowed intents:
- MENU
- ORDER
- SERVICE_REQUEST
- TICKET
- QNA
- SMALL_TALK
- UNKNOWN

Rules:
- Asking about food or drinks → MENU
- Ordering food or drinks → ORDER
- Requesting hotel services (spa, wake-up call, housekeeping, room service, towels, booking) → SERVICE_REQUEST
- Reporting problems or damages (leak, AC not working, power issue) → TICKET
- Asking factual hotel questions → QNA
- Greetings → SMALL_TALK
- Otherwise → UNKNOWN

Return JSON only:
{{
  "intent": "<INTENT>"
}}

User message:
\"\"\"{user_message}\"\"\"
"""


        try:
            model = genai.GenerativeModel(self.llm_model)
            response = model.generate_content(prompt)
            text = response.text.strip()
        # Parse JSON safely
            data = json.loads(text)
            return data
        except Exception as e:
            print(f"[WARN] LLM intent classification failed: {e}")
            return {"intent": "UNKNOWN"}



    def __init__(self):
        print("[DEBUG] Initializing ConciergeBot...")
        start_init = time.time()
        try:
            # Google Sheets webapp URL (not used) — prefer direct gspread helper
            

            self.qna_sheet = getattr(Config, "GSHEET_QNA_SHEET", "QnA_Manager")
            self.dos_sheet = getattr(Config, "GSHEET_DOS_SHEET", "Dos and Donts")
            self.campaign_sheet = getattr(Config, "GSHEET_CAMPAIGN_SHEET", "Campaigns_Manager")
            self.menu_sheet = getattr(Config, "GSHEET_MENU_SHEET", "Menu_Manager")
            self.orders_sheet = getattr(Config, "GSHEET_ORDERS_SHEET", "Room_Service_Orders")


            self.retriever_k = int(getattr(Config, "RETRIEVER_K", 5))
            self.sheet_refresh_interval = int(getattr(Config, "SHEET_REFRESH_INTERVAL", 300))
            self.sheet_last_refresh = 0

            self.sheet_fetch_timeout = float(getattr(Config, "SHEET_FETCH_TIMEOUT", 7.0))
            self.retrieve_timeout = float(getattr(Config, "RETRIEVER_TIMEOUT", 2.0))
            self.llm_timeout = float(getattr(Config, "LLM_TIMEOUT", 8.0))

            # Try to use Google Sheets via the gsheets_helper by default;
            # _refresh_sheets will flip to vector fallback on failure.
            self.use_sheet = True
            self.http = requests.Session()

            # Configure Gemini API
            genai.configure(api_key=os.getenv("GEMINI_API_KEY", getattr(Config, "GEMINI_API_KEY", None)))
            self.llm_model = os.getenv("GEMINI_MODEL", getattr(Config, "GEMINI_MODEL", "gemini-2.5-flash"))

            self._executor = ThreadPoolExecutor(max_workers=4)

            self.qna_rows: List[Dict[str, Any]] = []
            self.dos_donts: List[Dict[str, str]] = []
            self.campaigns: List[Dict[str, Any]] = []
            self.menu_rows: List[Dict[str, Any]] = []

            self.chat_histories: Dict[str, List[Dict[str, Any]]] = {}
            self.chat_lock = threading.Lock()
            self.chat_history_limit = int(getattr(Config, "CHAT_HISTORY_LIMIT", 10))
            self.chat_history_persist = bool(getattr(Config, "CHAT_HISTORY_PERSIST", True))
            self.chat_history_dir = getattr(Config, "CHAT_HISTORY_DIR", os.path.join("data", "chat_histories"))
            if self.chat_history_persist:
                os.makedirs(self.chat_history_dir, exist_ok=True)
            self.chat_save_every = int(getattr(Config, "CHAT_SAVE_EVERY", 5))

            if self.use_sheet:
                try:
                    self._refresh_sheets(force=True)
                    logger.info("Loaded Sheets data on init.")
                except Exception as e:
                    logger.warning(f"Sheets load failed: {e}, using vector fallback.")
                    self.use_sheet = False

            if not self.use_sheet:
                try:
                    self.vector_store = create_vector_store()
                    fetch_k = int(getattr(Config, "RETRIEVER_FETCH_K", 20))
                    self.retriever = self.vector_store.as_retriever(
                        search_type="mmr", search_kwargs={"k": self.retriever_k, "fetch_k": fetch_k}
                    )
                    logger.info("FAISS retriever loaded.")
                except Exception as e:
                    logger.error(f"Vector store init failed: {e}")
                    self.retriever = None

            if not self.dos_donts:
                self.dos_donts_path = os.path.join("data", "dos_donts.json")
                self.dos_donts = self._load_dos_donts_from_file()

            logger.info("ILORA RETREATS ConciergeBot ready.")
            print(f"[DEBUG] Init complete in {time.time() - start_init:.2f}s")
        except Exception as e:
            logger.error(f"Init error: {e}")
            raise

    # ---------------- Formatting helpers ----------------
    def _format_user_session_summary(self, session_obj: dict) -> str:
        """Summarize normalized user session into text for system prompt."""
        if not isinstance(session_obj, dict):
            return ""
        norm = session_obj.get("normalized") or session_obj
        parts = []
        if "client_id" in norm:
            parts.append(f"Client ID: {norm['client_id']}")
        if "name" in norm:
            parts.append(f"Name: {norm['name']}")
        if "email" in norm:
            parts.append(f"Email: {norm['email']}")
        if "booking_id" in norm:
            parts.append(f"Booking ID: {norm['booking_id']}")
        if "workflow_stage" in norm:
            parts.append(f"Workflow Stage: {norm['workflow_stage']}")
        if "room_alloted" in norm:
            parts.append(f"Room: {norm['room_alloted']}")
        if "check_in" in norm or "check_out" in norm:
            parts.append(f"Stay: {norm.get('check_in','')} → {norm.get('check_out','')}")
        if "id_link" in norm:
            parts.append(f"ID Proof: {norm['id_link']}")
        if "pending" in norm:
            parts.append(f"Pending: {norm['pending']}")
        return "\n".join(parts)

    def _format_conversation_for_prompt(self, history: list) -> str:
        """Format chat history into text for prompt injection."""
        if not history:
            return ""
        lines = []
        for msg in history[-self.chat_history_limit:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            lines.append(f"{role.title()}: {content}")
        return "\n".join(lines)

    def _raise_ticket(self, guest, request_text, category):
        from services.gsheets_helper import append_row_to_sheet, get_all_records
        import uuid

        room_no = guest.get("room_alloted", "N/A")
        request_norm = request_text.strip().lower()

    # 🚫 DUPLICATE PROTECTION (same room + same request)
        try:
            existing_tickets = get_all_records("ticket_management")
            for row in existing_tickets[-15:]:  # check recent tickets only
                if (
                    row.get("Room No") == room_no
                    and row.get("Request", "").strip().lower() == request_norm
                    and row.get("Status") in ["Open", "In Progress"]
            ):
                # ✅ Return existing ticket instead of creating new one
                   return row.get("Ticket ID")
        except Exception:
            pass  # fail-safe: never block ticket creation

    # ✅ Create new ticket only if no duplicate found
        ticket_id = f"TCK-{uuid.uuid4().hex[:5].upper()}"

        append_row_to_sheet("ticket_management", {
        "Ticket ID": ticket_id,
        "Email": guest.get("email") or guest.get("Email") or "N/A",
        "Name": guest.get("name") or guest.get("Name") or "Guest",
        "Room No": room_no,
        "Request": request_text,
        "Category": category,
        "Assigned To": category,
        "Status": "Open",
        "Created At": datetime.utcnow().isoformat(),
        "Resolved At": "",
        "Notes": ""
    })

        return ticket_id


    def get_recent_history(self, session_key: str) -> list:
        """Get last N messages for session."""
        with self.chat_lock:
            return self.chat_histories.get(session_key, [])[-self.chat_history_limit:]

    def add_chat_message(self, session_key: str, role: str, content: str, meta: dict = None):
        """Add a chat message to history and persist if needed."""
        with self.chat_lock:
            if session_key not in self.chat_histories:
                self.chat_histories[session_key] = []
            self.chat_histories[session_key].append(
                {"role": role, "content": content, "meta": meta or {}}
            )
    def _format_menu_for_llm(self, query: str) -> str:
        if not self.menu_rows:
            return "Menu information is currently unavailable."

        q = query.lower()
        rows = []

        for r in self.menu_rows:
            item = str(r.get("Item", "")).lower()
            typ = str(r.get("Type", "")).lower()

            if "menu" in q or item in q or typ in q:
                rows.append(r)

        if not rows:
            return "That item is not available in our menu."

        lines = []
        lines.append("📜 ILORA RETREATS – MENU")
        lines.append("")
        lines.append(
            f"{'Item ID':<8} | {'Type':<10} | {'Item':<20} | {'Price':<8} | Description"
        )
        lines.append("-" * 70)

        for r in rows:
            lines.append(
                f"{str(r.get('Item ID','')):<8} | "
                f"{str(r.get('Type','')):<10} | "
                f"{str(r.get('Item','')):<20} | "
                f"{str(r.get('Price','')):<8} | "
                f"{str(r.get('Description',''))}"
            )
        return "\n".join(lines)
 
    def _process_order(self, query: str):
        """
         Extract ordered items from query with quantities and calculate total amount safely.
         Returns:
             items_summary: str (e.g., "2 Coffee, 3 Sandwich")
             total_amount: float
        """
        ordered_items = defaultdict(int)
        total_amount = 0.0
        q = query.lower()

        for r in self.menu_rows:
            item_name = str(r.get("Item", "")).lower()
            if not item_name:
                continue

        # Regex to detect quantity, e.g., "2 coffee" or "coffee x3"
            patterns = [
                rf"(\d+)\s+{re.escape(item_name)}",
                rf"{re.escape(item_name)}\s*x\s*(\d+)"
            ]
            quantity = 0
            for pat in patterns:
                match = re.search(pat, q)
                if match:
                    quantity = int(match.group(1))
                    break
        # If no explicit quantity, check if item is mentioned once
            if quantity == 0 and item_name in q:
                quantity = 1

            if quantity > 0:
                ordered_items[r.get("Item")] += quantity

            # Safe price parsing
                raw_price = str(r.get("Price", "")).replace("₹", "").replace(",", "").strip()
                try:
                    price = float(raw_price)
                except Exception:
                    price = 0.0

                total_amount += price * quantity

        if not ordered_items:
            return "", 0.0

        items_summary = ", ".join(f"{v} {k}" if v > 1 else k for k, v in ordered_items.items())
        return items_summary, total_amount

    # ---------------- Timeout wrapper ----------------
    def _run_with_timeout(self, fn, args: tuple = (), timeout: float = 5.0):
        future = self._executor.submit(fn, *args)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            try:
                future.cancel()
            except Exception:
                pass
            raise TimeoutError(f"Operation timed out after {timeout}s")
        except Exception as e:
            raise

    # ---------------- Sheet fetch ----------------
    def _fetch_sheet_data(self, sheet_name: str) -> List[Dict[str, Any]]:
        try:
            return get_all_records(sheet_name)
        except Exception as e:
            logger.exception("_fetch_sheet_data failed: %s", e)
            raise

    def _refresh_sheets(self, force=False):
        now = time.time()
        if not force and now - self.sheet_last_refresh < self.sheet_refresh_interval:
            return

        self.qna_rows = get_all_records(self.qna_sheet) or []
        # ✅ Normalize QnA_Manager rows for retrieval
        normalized_qna = []

        for row in self.qna_rows:
            if str(row.get("Status", "")).strip().lower() != "active":
                continue

            question = str(row.get("Question", "")).strip()
            answer = str(row.get("Answer", "")).strip()

            if not question or not answer:
                continue

            row["page_content"] = f"Q: {question}\nA: {answer}"
            row["question_norm"] = _normalize_text(question)


            normalized_qna.append(row)

        self.qna_rows = normalized_qna

        logger.info("QnA loaded: %d active questions", len(self.qna_rows))

        self.menu_rows = get_all_records(self.menu_sheet) or []
        self.dos_donts = get_all_records(self.dos_sheet) or []
        self.campaigns = get_all_records(self.campaign_sheet) or []
        print("🔥 DEBUG MENU ROWS:", len(self.menu_rows))
        logger.info("Sheets refreshed | Menu items: %d", len(self.menu_rows))
        self.sheet_last_refresh = now
        
    def _save_room_service_order(
    self,
    email: str,
    name: str,
    room_no: str,
    orders: str,
    pending_balance: float
):
        """
        Save room service order to Google Sheet: Room_Service_Orders
        Headers:
        [Email | Name | Room No | Orders | Pending Balance]
        """
        try:
            from services.gsheets_helper import append_row_to_sheet

            row_data = {
                "Email": email,
                "Name": name,
                "Room No": room_no,
                "Orders": orders,
                "Pending Balance": pending_balance
            }

            result = append_row_to_sheet("Room_Service_Orders", row_data)

            if not result.get("success"):
                raise RuntimeError(result.get("message", "Unknown sheet error"))

            logger.info("Room service order saved for %s", email)

        except Exception as e:
            logger.error("Failed to save room service order: %s", e)
            raise


    def _load_dos_donts_from_file(self):
        path = getattr(self, "dos_donts_path", os.path.join("data", "dos_donts.json"))
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    def _is_menu_query(self, query: str) -> bool:
        q = query.lower()
        return any(k in q for k in [
            "menu", "food", "drink", "beverage",
            "breakfast", "lunch", "dinner",
            "price", "cost","what you have", "available",
            "show me", "list of", "dish", "item",
        ])    
    def _is_order_query(self, query: str) -> bool:
        q = query.lower()
        return any(k in q for k in [
        "order", "i want", "i need",
        "i would like", "i'll have", "ill have",
        "get me", "bring me", "can i have",
        "add to my order", "place an order",
        "deliver", "delivery", 
    ])
  
    def _is_ticket_request(self, query: str) -> bool:
        q = query.lower()
        return any(k in q for k in TICKET_KEYWORDS)

    def _is_service_request(self, query: str) -> bool:
        """
        Detect hotel service requests (NOT maintenance tickets).
        Examples: wake-up call, spa booking, housekeeping, towels.
        """
        if not query:
            return False

        q = query.lower()

        SERVICE_KEYWORDS = [
        "wake up", "wake-up", "wake me",
        "spa", "massage", "book a spa",
        "housekeeping", "clean room", "room cleaning",
        "extra towel", "extra towels", "towels",
        "blanket", "pillow",
        "luggage", "bag",
        "room service", "assist me",
        "call me at", "remind me"
        ]

        return any(k in q for k in SERVICE_KEYWORDS)


    # ---------------- Retrieval ----------------
    def _row_to_doc_text(self, row):
        q = row.get("question") or row.get("q") or ""
        a = row.get("answer") or row.get("a") or ""
        return f"Q: {q}\nA: {a}" if q or a else " | ".join(str(v) for v in row.values() if v)

    def _score_doc(self, doc_text_norm, query_norm):
        if not doc_text_norm or not query_norm:
            return 0.0

        q_tokens = set(query_norm.split())
        d_tokens = set(doc_text_norm.split())

        overlap = len(q_tokens & d_tokens) / max(1, min(len(q_tokens), len(d_tokens)))
        seq = difflib.SequenceMatcher(None, doc_text_norm, query_norm).ratio()

    # 🔥 Intent / semantic boost
        intent_boost = 0.15 if any(
            k in doc_text_norm
            for k in q_tokens
        ) else 0.0

        return min(1.0, 0.6 * overlap + 0.3 * seq + intent_boost)


    def _retrieve_from_sheets(self, query, k=None):
        k = k or self.retriever_k
        nq = _normalize_text(query)
        scored = [
            (self._score_doc(row["question_norm"], nq), row["page_content"], row)
            for row in self.qna_rows
        ]
        scored = [(s, t, r) for s, t, r in scored if s > 0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"page_content": t, "score": s, "metadata": r} for s, t, r in scored[:k]]
    
    def _increment_qna_usage(self, row):
        try:
            from services.gsheets_helper import update_row_by_id

            usage = int(row.get("Usage Count", 0)) + 1
            update_row_by_id(
                sheet_name=self.qna_sheet,
                row_id=row.get("QnA ID"),
                updates={
                    "Usage Count": usage,
                    "Last Updated": datetime.utcnow().isoformat()
                }
            )
        except Exception as e:
            logger.warning("Failed to update QnA usage: %s", e)

    # ---------------- Ask ----------------
    def _extract_session_object(self, user_session, session_key):
        if not user_session:
            return None, None
        if isinstance(user_session, dict) and ("frontend" in user_session or "normalized" in user_session):
            norm = user_session.get("normalized", {}) or {}
            key = norm.get("email") or norm.get("client_id") or session_key
            return key, user_session
        if isinstance(user_session, dict):
            if session_key and session_key in user_session:
                return session_key, user_session[session_key]
        return None, None
    def _build_prompt(self, hotel_data: str, query: str, user_profile_text: str = "", recent_conversation: str = "") -> str:
        agent_name = "AI Assistant"
        agents_file = os.path.join("data", "agents.json")
        try:
            if os.path.exists(agents_file):
                with open(agents_file, "r", encoding="utf-8") as f_new_1:
                    agents = json.load(f_new_1)
                for agent in agents:
                    if agent.get("Name") == "Front Desk":
                        agent_name = agent.get("agent_name", agent_name)
        except Exception:
            pass

        rules_text = ""
        if self.dos_donts:
            rules_text = "\n\n📋 **Important Communication Rules:**\n"
            for idx, entry in enumerate(self.dos_donts, start=1):
                do = str(entry.get("do", "")).strip()
                dont = str(entry.get("dont", "")).strip()
                if do:
                    rules_text += f"- ✅ Do: {do}\n"
                if dont:
                    rules_text += f"- ❌ Don't: {dont}\n"

        campaigns_text = ""
        if self.campaigns:
            campaigns_text = "\n\n📣 **Active Campaigns / Promos (summary):**\n"
            for c in self.campaigns[:5]:
                title = c.get("Name") or c.get("Title") or c.get("Campaign") or ""
                desc = c.get("Description") or c.get("Desc") or c.get("Details") or ""
                if title or desc:
                    campaigns_text += f"- {title} {('- ' + desc) if desc else ''}\n"

        

        recent_conv_text = f"\n\nRecent Conversation (most recent messages):\n{recent_conversation}" if recent_conversation else ""
        user_profile_block = f"\n\nGuest Profile (from session):\n{user_profile_text}" if user_profile_text else ""
    
        prompt = (
            f"You are an AI agent named {agent_name} for the user {user_profile_block}, a knowledgeable, polite, and concise concierge assistant at *ILORA RETREATS*, "
            f"ILORA RETREATS have only LUXURY TENTS in room types and have 14 rooms in total."
            f"The following is the earlier chat {recent_conv_text} you need to reply accordingly"
            f"a premium hotel known for elegant accommodations, gourmet dining, rejuvenating spa treatments, "
            f"Ilora Retreats is a luxury safari camp in Kenya’s Masai Mara, near Olkiombo Airstrip, offering 14 fully equipped tents with en‑suite bathrooms, private verandas, and accessible facilities. Guests can enjoy a pool, spa, gym, yoga, bush dinners, and stargazing, with activities like game drives, walking safaris, hot air balloon rides, and Maasai cultural experiences. Full-board rates start around USD 500–650 per night, with premium activities and beverages extra. The retreat emphasizes sustainability, blending nature with comfort, creating an immersive safari experience."
            f"a fully-equipped gym, pool access, 24x7 room service, meeting spaces, and personalized hospitality.\n\n"
            "Answer guest queries using the Hotel Data and the Menu given below.\n"
            "You MUST answer strictly using the Hotel Data provided.\n"
            "If the answer is not present, respond exactly with:\n"
            "\"I’m sorry, I don’t have that information at the moment.\"\n"
            "Do NOT use general knowledge.\n"
            "Do NOT invent facts.\n\n"
            "but remember **DO NOT MAKE FALSE FACTS**. If unsure, ask clarifying questions. DO NOT GIVE PHONE NUMBERS unless very necessary.\n\n"
            f"Agent Name: {agent_name}\n\n"
            f"Hotel Data (most relevant excerpts):\n{hotel_data}\n\n"
            f"{user_profile_block}\n\n"
            f"{recent_conv_text}\n\n"
            f"Guest Query: {query}\n"
            f"{rules_text}\n"
            f"{campaigns_text}\n"
            f"Rules:\n"
            f"1. Do NOT hallucinate or provide inaccurate information.\n"
            f"2. 2. If the answer is not available, respond exactly:\n"
            f"\"I’m sorry, I don’t have that information at the moment.\"\n"
            f"Do NOT raise tickets for QnA.\n"
            f"3. Authority boundaries must be respected: maintenance, billing, or managerial approvals are required for changes."
            f"\n\nProvide a helpful, accurate, and concise response based on the data and rules above"
        )
        # ✅ PASTE THIS HERE (EXACTLY HERE)
        prompt += (
    "\n\nSTRICT MENU RULES:\n"
    "- Answer menu questions ONLY using the Menu data provided.\n"
    "- If an item is not explicitly listed, say: 'That item is not available.'\n"
    "- Do NOT guess prices or items.\n"
    "- Do NOT suggest alternatives unless asked.\n"
)


        return prompt
    
    def ask(self, query: str, user_type=None, user_session=None, session_key=None) -> str:
        print(f"[DEBUG] >>> ask: {query}")

        sess_key, sess_obj = self._extract_session_object(user_session, session_key)

    # --- 1️⃣ Intent Detection (LLM) ---
        intent_data = self.classify_intent_with_llm(query)
        intent = intent_data.get("intent", "UNKNOWN")
        print(f"[DEBUG] LLM Intent detected: {intent}")


    # Fallback to keyword detection if UNKNOWN
        # 🔥 Ticket has highest priority
        # Fallback ONLY if LLM is unsure
        # Fallback ONLY if LLM is unsure
        if intent == "UNKNOWN":
            if self._is_ticket_request(query):
                intent = "TICKET"
            elif self._is_service_request(query):
                intent = "SERVICE_REQUEST"
            elif self._is_order_query(query):
                intent = "ORDER"
            elif self._is_menu_query(query):
                intent = "MENU"
            else:
                intent = "QNA"



    # --- 2️⃣ MENU Handling ---
        if intent == "MENU":
            try:
                self._refresh_sheets()
            except Exception as e:
                print(f"[WARN] Menu sheet refresh failed: {e}")
            return self._format_menu_for_llm(query)

        elif intent == "ORDER" and sess_obj:
            self._refresh_sheets()
            items = intent_data.get("items", [])
            if not items:
        
        # fallback if LLM did not extract items
                orders, total = self._process_order(query)
            else:
        # If LLM extracted items, convert to summary and total
                orders_list = []
                total = 0.0
                for it in items:
                    name = it.get("name")
                    qty = int(it.get("quantity", 1))
            # Find price from menu
                    menu_row = next((r for r in self.menu_rows if r.get("Item", "").lower() == name.lower()), None)
                    price = float(str(menu_row.get("Price", "0")).replace("₹", "").replace(",", "").strip()) if menu_row else 0
                    orders_list.append(f"{qty} {name}" if qty > 1 else name)
                    total += price * qty
                orders = ", ".join(orders_list)

            if not orders:
                return "This request needs staff assistance. I’ve raised a service ticket for you."

            guest = sess_obj.get("normalized", sess_obj)
            try:
                self._save_room_service_order(
                    email=guest.get("email", "N/A"),
                    name=guest.get("name", "Guest"),
                    room_no=guest.get("room_alloted", "N/A"),
                    orders=orders,
                    pending_balance=total
                )
            except Exception as e:
                print(f"[ERROR] Failed to save order: {e}")
                return "I couldn't save your order at the moment. Please try again shortly."

            return (
                f"✅ **Order Confirmed!**\n\n"
                f"🧾 Items: {orders}\n"
                f"💰 Total Amount: ₹{total}\n"
                f"📍 Room No: {guest.get('room_alloted', 'N/A')}\n\n"
                f"Your order will be delivered shortly. Thank you!"
            )
        
        elif intent in ["SERVICE_REQUEST", "TICKET"]:
            return (
        "✅ I’ve noted your request.\n"
        "Our team will assist you shortly."
            )



    # If intent changed to QNA, continue to QnA handling

        elif intent == "QNA":
    # --- 5️⃣ QnA / LLM Handling ---
            docs = []
            if self.use_sheet:
                try:
                    self._refresh_sheets()
                    docs = self._run_with_timeout(self._retrieve_from_sheets, (query,), timeout=self.retrieve_timeout)
                except Exception as e:
                    print(f"[WARN] Sheets retrieval failed: {e}")
                    docs = []

            if not docs and getattr(self, "retriever", None):
                try:
                    docs = self._run_with_timeout(lambda q: self.retriever.get_relevant_documents(q), (query,), timeout=self.retrieve_timeout)
                except Exception as e:
                    print(f"[WARN] Vector retriever failed: {e}")
                    docs = []

    # Get best match
            best_match = docs[0] if docs else None
            if not best_match:
                answer = "I’m sorry, I don’t have that information at the moment."

            elif best_match["score"] < QNA_MIN_SCORE:
    # 🔥 Soft fallback: let Gemini answer using closest QnA
                hotel_data = best_match["page_content"]

                prompt = self._build_prompt(
                    hotel_data,
                    query,
                    self._format_user_session_summary(sess_obj) if sess_obj else "",
                    self._format_conversation_for_prompt(self.get_recent_history(sess_key)) if sess_key else ""
                )

                try:
                    model = genai.GenerativeModel(self.llm_model)
                    response = model.generate_content(prompt)
                    answer = response.text
                except Exception:
                    answer = "I’m sorry, I don’t have that information at the moment."
            else:
                hotel_data = best_match["page_content"]
                matched_row = best_match["metadata"]
                self._increment_qna_usage(matched_row)

                user_profile_text = self._format_user_session_summary(sess_obj) if sess_obj else ""
                recent_conversation = (
                    self._format_conversation_for_prompt(self.get_recent_history(sess_key))
                    if sess_key else ""
                )

                prompt = self._build_prompt(
                    hotel_data,
                    query,
                    user_profile_text,
                    recent_conversation
                )

                try:
                    def gemini_call():
                        model = genai.GenerativeModel(self.llm_model)
                        response = model.generate_content(prompt)
                        return response.text if hasattr(response, "text") else str(response)
                    answer = self._run_with_timeout(gemini_call, timeout=self.llm_timeout)
                except Exception as e:
                    err = str(e).lower()
                    print(f"[ERROR] Gemini call failed: {e}")
                    if "quota" in err or "429" in err:
                        answer = (
                            "I'm currently handling a lot of requests and have temporarily reached my response limit. "
                            "Please try again in a minute"
                        )
                    else:
                        answer = "I'm sorry, I couldn't process that right now."

    # --- 6️⃣ Chat History Persistence ---
            if sess_key:
                self.add_chat_message(sess_key, "user", query, meta={"ts": datetime.utcnow().isoformat() + "Z"})
                self.add_chat_message(sess_key, "assistant", answer, meta={"ts": datetime.utcnow().isoformat() + "Z"})

        # Persist chat every N messages
                if self.chat_history_persist and len(self.chat_histories[sess_key]) % self.chat_save_every == 0:
                    try:
                        path = os.path.join(self.chat_history_dir, f"{sess_key}.json")
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(self.chat_histories[sess_key], f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        print(f"[WARN] Failed to persist chat history: {e}")

            return answer