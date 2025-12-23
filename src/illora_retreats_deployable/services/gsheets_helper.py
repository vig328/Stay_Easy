import os
import logging
from typing import Dict, Any, List, Optional
from config import Config
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

try:
    import gspread
except Exception:
    gspread = None


def _get_creds_path():
    return (
         getattr(Config, "SERVICE_ACCOUNT_FILE", None)
    )

def _get_spreadsheet_id_or_name():
    return (
         getattr(Config, "GSHEET_ID", None)
    )



def _open_worksheet(sheet_name: str):
    """Return a gspread Worksheet instance for the configured spreadsheet and sheet name."""
    if gspread is None:
        raise RuntimeError("gspread not available")

    creds_path = _get_creds_path()
    if not creds_path or not os.path.exists(creds_path):
        raise RuntimeError("Google service account JSON not found. Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS to a valid path.")

    client = gspread.service_account(filename=creds_path)

    ss_id = _get_spreadsheet_id_or_name()
    if not ss_id:
        raise RuntimeError("Spreadsheet identifier not configured. Set SPREADSHEET_ID or SPREADSHEET_NAME environment variable.")

    try:
        # If id looks like an ID (contains : or long), try open_by_key
        ws = None
        try:
            sh = client.open_by_key(ss_id)
        except Exception:
            sh = client.open(ss_id)
        ws = sh.worksheet(sheet_name)
        return ws
    except Exception as e:
        logger.exception("_open_worksheet failed for sheet=%s: %s", sheet_name, e)
        raise


def append_row_to_sheet(sheet_name: str, row_data: Dict[str, Any]) -> Dict[str, Any]:
    """Append a row to the sheet using the header order present in the sheet.

    Returns a dict similar to previous webapp responses: {"success": True} or {"success": False, "message": ...}
    """
    try:
        ws = _open_worksheet(sheet_name)
        headers = ws.row_values(1)
        if not headers:
            # If no headers, write them in the order of row_data keys
            headers = list(row_data.keys())
            ws.append_row(headers, value_input_option="RAW")
        ordered = [row_data.get(h, "") for h in headers]
        ws.append_row(ordered, value_input_option="RAW")
        return {"success": True}
    except Exception as e:
        logger.exception("append_row_to_sheet failed for %s", sheet_name)
        return {"success": False, "message": str(e)}


def find_row_by_email(sheet_name: str, email: str) -> Optional[Dict[str, Any]]:
    """Return the first row dict where a header normalised to 'email' or 'username' matches the email."""
    try:
        ws = _open_worksheet(sheet_name)
        records = ws.get_all_records()
        target = (email or "").strip().lower()
        for r in records:
            for k, v in r.items():
                if k and ''.join(ch.lower() for ch in k if ch.isalnum()) in ("email", "username"):
                    if str(v or "").strip().lower() == target:
                        return r
        return None
    except Exception as e:
        logger.exception("find_row_by_email failed: %s", e)
        return None


def get_all_records(sheet_name: str) -> List[Dict[str, Any]]:
    """Return all rows as list of dicts using headers as keys."""
    try:
        ws = _open_worksheet(sheet_name)
        return ws.get_all_records()
    except Exception as e:
        logger.exception("get_all_records failed: %s", e)
        return []


def update_row_by_email(sheet_name: str, email: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Find the row by email and update the provided columns. Returns {success: True/False, userData?: {...}}"""
    try:
        ws = _open_worksheet(sheet_name)
        headers = ws.row_values(1)
        records = ws.get_all_records()
        target = (email or "").strip().lower()
        for idx, r in enumerate(records, start=2):
            for k, v in r.items():
                if k and ''.join(ch.lower() for ch in k if ch.isalnum()) in ("email", "username"):
                    if str(v or "").strip().lower() == target:
                        # build updated row values
                        padded = [r.get(h, "") for h in headers]
                        for uk, uv in updates.items():
                            if uk in headers:
                                pos = headers.index(uk)
                                padded[pos] = uv
                            else:
                                headers.append(uk)
                                padded.append(uv)
                        # If headers extended, update header row
                        if len(headers) > len(ws.row_values(1)):
                            ws.update("A1", [headers])
                        ws.update(f"A{idx}", [padded])
                        return {"success": True, "userData": r}
        return {"success": False, "message": "Email not found"}
    except Exception as e:
        logger.exception("update_row_by_email failed: %s", e)
        return {"success": False, "message": str(e)}
