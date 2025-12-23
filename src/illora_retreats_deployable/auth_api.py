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
from services.gsheets_helper import append_row_to_sheet, find_row_by_email, update_row_by_email

# ------------------------- Logging setup -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------- FastAPI app -------------------------
app = FastAPI(title="Illora Auth API", version="1.0.0")

# ------------------------- CORS -------------------------
FRONTEND_ORIGINS = [
    "http://localhost:8080",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ------------------------- Models -------------------------
class SignupReq(BaseModel):
    name: str = Field(..., min_length=2, description="User's full name")
    username: str = Field(..., min_length=3, description="User's email address")
    password: str = Field(..., min_length=6, description="User's password")
    phoneNo: str = Field(default="", description="User's phone number")
    
class LoginReq(BaseModel):
    username: str = Field(..., description="User's username")
    password: str = Field(..., description="User's password")
    remember: bool = Field(default=True, description="Remember me flag")

class UpdateWorkflowReq(BaseModel):
    username: str = Field(..., description="User's username/email")
    stage: str = Field(..., description="New workflow stage")
    booking_id: Optional[str] = Field(None, description="Booking ID if available")
    id_proof_link: Optional[str] = Field(None, description="ID proof link if available")

# ------------------------- Google Sheets Integration -------------------------
def push_row_to_sheet(sheet_name: str, row_data: Dict[str, Any]) -> Dict[str, Any]:
    """Append a row to the specified sheet using gspread via services.gsheets_helper.

    Keeps the old return shape: {'success': True} or {'success': False, 'message': ...}
    """
    logger.info("Pushing data to sheet %s: %s", sheet_name, {k: (v if str(k).lower() != 'password' else '****') for k, v in row_data.items()})
    try:
        return append_row_to_sheet(sheet_name, row_data)
    except Exception as e:
        logger.exception("push_row_to_sheet failed: %s", e)
        return {"success": False, "message": str(e)}

# ------------------------- Endpoints -------------------------
@app.post("/auth/login", tags=["authentication"])
async def login(req: LoginReq):
    """Verify user credentials against the Google Sheet"""
    logger.info(f"Login attempt for username: {req.username}")
    
    try:
        # Use direct Sheets lookup instead of Apps Script webapp
        row = find_row_by_email("Client_workflow", req.username)
        if not row:
            logger.warning("User %s not found in Client_workflow", req.username)
            raise HTTPException(
                status_code=403,
                detail={"message": "User not registered. Please sign up first.", "needsSignup": True},
            )

        pw = row.get("Password") or row.get("password") or row.get("Password Hash") or row.get("password_hash")
        if not pw or str(pw) != str(req.password):
            logger.warning("Invalid password for user %s", req.username)
            raise HTTPException(status_code=401, detail={"message": "Invalid credentials"})

        token = uuid.uuid4().hex if req.remember else None
        logger.info("User %s logged in successfully (sheets)", req.username)
        return {"username": req.username, "remember_token": token, "userData": row}
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/auth/signup", tags=["authentication"])
async def signup(req: SignupReq = Body(...)):
    """Register a new user and add them to the Client_workflow sheet"""
    logger.info(f"Received signup request for username: {req.username}")
    
    try:
        # Generate unique Client Id
        client_id = f"ILR-{datetime.utcnow().year}-{random.randint(1000,9999)}"
        workflow_stage = "Registered"
        
        # Prepare row data for Client_workflow sheet
        row_data = {
            "Client Id": client_id,
            "Name": req.name,
            "Email": req.username,  # Map username to Email column
            "Password": req.password,
            "Booking Id": "",
            "Workflow Stage": workflow_stage,
            "Room Alloted": "",
            "CheckIn": "",
            "Check Out": "",
            "Id Link": "",
        }
        
        # Add user to Google Sheet
        resp = push_row_to_sheet("Client_workflow", row_data)
        
        if resp.get("success") or resp.get("ok") or resp.get("status_code") == 200:
            logger.info(f"User {req.username} registered successfully with client ID {client_id}")
            return {
                "success": True,
                "workflowStage": workflow_stage,
                "clientId": client_id,
                "message": "Registration successful"
            }
        else:
            error_msg = resp.get("message", "Unknown error during registration")
            logger.error(f"Failed to register user {req.username}: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
            
    except Exception as e:
        logger.error(f"Error in signup endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))     

@app.post("/auth/update-workflow", tags=["authentication"])
async def update_workflow(req: UpdateWorkflowReq):
    """Update a user's workflow stage in the Client_workflow sheet"""
    logger.info(f"Updating workflow stage for user {req.username} to {req.stage}")
    
    try:
        updates = {"Workflow Stage": req.stage}
        if req.booking_id:
            updates["Booking Id"] = req.booking_id
        if req.id_proof_link:
            updates["Id Link"] = req.id_proof_link

        result = update_row_by_email("Client_workflow", req.username, updates)
        if not result.get("success"):
            logger.error("Failed to update workflow for %s: %s", req.username, result.get("message"))
            raise HTTPException(status_code=400, detail=result.get("message", "Failed to update"))

        logger.info("Successfully updated workflow stage for user %s", req.username)
        return {"success": True, "message": f"Workflow stage updated to {req.stage}", "userData": result.get("userData")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error updating workflow: %s", str(e))
        raise HTTPException(status_code=500, detail=str(e))