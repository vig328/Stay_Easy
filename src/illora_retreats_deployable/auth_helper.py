import logging
from typing import Optional, Tuple, Dict, Any
from config import Config
from services.gsheets_helper import find_row_by_email

CLIENT_WORKFLOW_SHEET = "Client_workflow"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def verify_user_credentials(username: str, password: str) -> Tuple[bool, bool, Optional[Dict[str, Any]], str]:
    """
    Verify user credentials against the Google Sheet
    Returns: (found: bool, verified: bool, user_data: Optional[Dict], message: str)
    """
    try:
        logger.info("Attempting login with username: %s", username)
        row = find_row_by_email(CLIENT_WORKFLOW_SHEET, username)
        if not row:
            logger.warning("verify_user_credentials: user not found: %s", username)
            return False, False, None, "User not found"

        pw = row.get("Password") or row.get("password") or row.get("Password Hash") or row.get("password_hash")
        verified = False
        if pw and str(pw) == str(password):
            verified = True

        message = "Verified" if verified else "Invalid credentials"
        logger.info("Authentication result - Found: True, Verified: %s", verified)
        return True, verified, row if verified else None, message
    except Exception as e:
        error_msg = f"Unexpected error during verification: {str(e)}"
        logger.exception(error_msg)
        return False, False, None, error_msg