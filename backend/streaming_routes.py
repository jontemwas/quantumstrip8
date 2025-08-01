from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
import uuid
import logging
import os

from auth import get_current_user
from database import get_database
from models import User, UserRole, ModelProfile, PrivateShow, Transaction, TransactionType, TransactionStatus

logger = logging.getLogger(__name__)

router = APIRouter()

# Configuration
PRIVATE_SHOW_RATE = int(os.getenv("PRIVATE_SHOW_RATE", 20))

# Request/Response Models
class StreamingSessionRequest(BaseModel):
    model_id: str = Field(..., description="ID of the model to watch")
    session_type: str = Field(..., description="public or private")

class PrivateShowRequest(BaseModel):
    model_id: str = Field(..., description="ID of the model for private show")
    duration_minutes: Optional[int] = Field(None, description="Requested duration in minutes")

class StreamingSessionResponse(BaseModel):
    session_id: str
    model_id: str
    viewer_id: str
    session_type: str
    status: str
    created_at: datetime
    webrtc_config: Optional[Dict[str, Any]] = None

class PrivateShowResponse(BaseModel):
    show_id: str
    model_id: str
    viewer_id: str
    rate_per_minute: int
    status: str
    created_at: datetime
    estimated_cost: Optional[int] = None

class ModelStreamingStatus(BaseModel):
    model_id: str
    is_live: bool
    is_available: bool
    current_viewers: int
    show_rate: int
    last_online: Optional[datetime] = None

class WebRTCSignalRequest(BaseModel):
    session_id: str
    signal_type: str = Field(..., description="offer, answer, ice-candidate")
    signal_data: Dict[str, Any]
    target_user_id: str

# Basic WebRTC configuration
WEBRTC_CONFIG = {
    "iceServers": [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
        # Add TURN servers here for production
    ]
}

# Streaming Session Routes
@router.post("/session", response_model=StreamingSessionResponse)
async def create_streaming_session(
    request: StreamingSessionRequest,
    current_user: User = Depends(get_current_user)
):
    """Create a streaming session (public or private)"""
    try:
        db = await get_database()
        
        # Verify model exists and is available
        model_profile = await db.model_profiles.find_one({"_id": request.model_id})
        if not model_profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Model not found"
            )
        
        if not model_profile.get("is_available", False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Model is currently unavailable"
            )
        
        # Create session
        session_id = str(uuid.uuid4())
        session_data = {
            "_id": session_id,
            "model_id": request.model_id,
            "viewer_id": current_user.id,
            "session_type": request.session_type,
            "status": "active",
            "created_at": datetime.utcnow(),
            "webrtc_config": WEBRTC_CONFIG
        }
        
        # Store session in database
        await db.streaming_sessions.insert_one(session_data)
        
        # Update model's viewer count for public sessions
        if request.session_type == "public":
            await db.model_profiles.update_one(
                {"_id": request.model_id},
                {"$inc": {"total_viewers": 1}}
            )
        
        logger.info(f"Streaming session created: {session_id} for model {request.model_id}")
        
        return StreamingSessionResponse(
            session_id=session_id,
            model_id=request.model_id,
            viewer_id=current_user.id,
            session_type=request.session_type,
            status="active",
            created_at=session_data["created_at"],
            webrtc_config=WEBRTC_CONFIG
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating streaming session: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create streaming session"
        )

@router.delete("/session/{session_id}")
async def end_streaming_session(
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """End a streaming session"""
    try:
        db = await get_database()
        
        # Get session
        session = await db.streaming_sessions.find_one({"_id": session_id})
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Streaming session not found"
            )
        
        # Verify user can end this session
        if session["viewer_id"] != current_user.id and session["model_id"] != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to end this session"
            )
        
        # Update session status
        await db.streaming_sessions.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "status": "ended",
                    "ended_at": datetime.utcnow()
                }
            }
        )
        
        logger.info(f"Streaming session ended: {session_id}")
        
        return {
            "success": True,
            "message": "Streaming session ended successfully",
            "session_id": session_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error ending streaming session: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to end streaming session"
        )

# Private Show Routes
@router.post("/private-show", response_model=PrivateShowResponse)
async def request_private_show(
    request: PrivateShowRequest,
    current_user: User = Depends(get_current_user)
):
    """Request a private show with a model"""
    try:
        db = await get_database()
        
        # Verify user has viewer role
        if current_user.role != UserRole.VIEWER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only viewers can request private shows"
            )
        
        # Verify model exists and is available
        model_profile = await db.model_profiles.find_one({"_id": request.model_id})
        if not model_profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Model not found"
            )
        
        if not model_profile.get("is_available", False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Model is currently unavailable"
            )
        
        # Check if viewer has enough tokens for at least 1 minute
        viewer_profile = await db.viewer_profiles.find_one({"user_id": current_user.id})
        if not viewer_profile or viewer_profile.get("token_balance", 0) < PRIVATE_SHOW_RATE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient tokens. You need at least {PRIVATE_SHOW_RATE} tokens for private show"
            )
        
        # Create private show request
        show_id = str(uuid.uuid4())
        private_show = PrivateShow(
            id=show_id,
            viewer_id=current_user.id,
            model_id=request.model_id,
            rate_per_minute=PRIVATE_SHOW_RATE,
            status="requested"
        )
        
        # Store in database
        await db.private_shows.insert_one(private_show.model_dump(by_alias=True))
        
        # Calculate estimated cost for requested duration
        estimated_cost = None
        if request.duration_minutes:
            estimated_cost = request.duration_minutes * PRIVATE_SHOW_RATE
        
        logger.info(f"Private show requested: {show_id} by {current_user.id} for model {request.model_id}")
        
        return PrivateShowResponse(
            show_id=show_id,
            model_id=request.model_id,
            viewer_id=current_user.id,
            rate_per_minute=PRIVATE_SHOW_RATE,
            status="requested",
            created_at=private_show.created_at,
            estimated_cost=estimated_cost
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error requesting private show: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to request private show"
        )

@router.patch("/private-show/{show_id}/accept")
async def accept_private_show(
    show_id: str,
    current_user: User = Depends(get_current_user)
):
    """Accept a private show request (model only)"""
    try:
        if current_user.role != UserRole.MODEL:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only models can accept private show requests"
            )
        
        db = await get_database()
        
        # Get private show
        private_show = await db.private_shows.find_one({"_id": show_id})
        if not private_show:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Private show request not found"
            )
        
        # Verify this model can accept this show
        model_profile = await db.model_profiles.find_one({"user_id": current_user.id})
        if not model_profile or model_profile["_id"] != private_show["model_id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to accept this private show"
            )
        
        if private_show["status"] != "requested":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Private show request has already been processed"
            )
        
        # Update show status and start time
        await db.private_shows.update_one(
            {"_id": show_id},
            {
                "$set": {
                    "status": "active",
                    "started_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        logger.info(f"Private show accepted: {show_id} by model {current_user.id}")
        
        return {
            "success": True,
            "message": "Private show accepted successfully",
            "show_id": show_id,
            "status": "active"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error accepting private show: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to accept private show"
        )

@router.patch("/private-show/{show_id}/end")
async def end_private_show(
    show_id: str,
    current_user: User = Depends(get_current_user)
):
    """End a private show and process payment"""
    try:
        db = await get_database()
        
        # Get private show
        private_show = await db.private_shows.find_one({"_id": show_id})
        if not private_show:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Private show not found"
            )
        
        # Verify user can end this show
        if private_show["viewer_id"] != current_user.id and private_show["model_id"] != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to end this private show"
            )
        
        if private_show["status"] != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Private show is not currently active"
            )
        
        # Calculate duration and cost
        started_at = private_show["started_at"]
        ended_at = datetime.utcnow()
        duration_seconds = (ended_at - started_at).total_seconds()
        duration_minutes = int(duration_seconds / 60) + 1  # Round up to next minute
        
        total_cost = duration_minutes * private_show["rate_per_minute"]
        
        # Get viewer profile to check balance
        viewer_profile = await db.viewer_profiles.find_one({"user_id": private_show["viewer_id"]})
        if not viewer_profile or viewer_profile.get("token_balance", 0) < total_cost:
            # Insufficient funds - end show but don't charge
            await db.private_shows.update_one(
                {"_id": show_id},
                {
                    "$set": {
                        "status": "ended_insufficient_funds",
                        "ended_at": ended_at,
                        "duration_minutes": duration_minutes,
                        "total_cost": 0,
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            
            return {
                "success": False,
                "message": "Show ended due to insufficient tokens",
                "show_id": show_id,
                "duration_minutes": duration_minutes,
                "cost": 0
            }
        
        # Process payment
        # 1. Deduct tokens from viewer
        await db.viewer_profiles.update_one(
            {"user_id": private_show["viewer_id"]},
            {
                "$inc": {
                    "token_balance": -total_cost,
                    "total_spent": total_cost
                },
                "$set": {"updated_at": datetime.utcnow()}
            }
        )
        
        # 2. Add earnings to model (after platform fee)
        platform_fee = total_cost * 0.5  # 50% platform fee
        model_earnings = total_cost - platform_fee
        
        await db.model_profiles.update_one(
            {"_id": private_show["model_id"]},
            {
                "$inc": {
                    "total_earnings": model_earnings,
                    "available_balance": model_earnings,
                    "total_shows": 1
                },
                "$set": {"updated_at": datetime.utcnow()}
            }
        )
        
        # 3. Create transaction records
        # Viewer transaction
        viewer_transaction = Transaction(
            user_id=private_show["viewer_id"],
            transaction_type=TransactionType.PRIVATE_SHOW,
            amount=total_cost,
            tokens=total_cost,
            status=TransactionStatus.COMPLETED,
            model_id=private_show["model_id"],
            description=f"Private show ({duration_minutes} minutes)",
            metadata={
                "show_id": show_id,
                "duration_minutes": duration_minutes,
                "rate_per_minute": private_show["rate_per_minute"]
            }
        )
        
        # Model transaction
        model_transaction = Transaction(
            user_id=private_show["model_id"],  # This should be model's user_id, need to fix
            transaction_type=TransactionType.EARNING,
            amount=model_earnings,
            tokens=int(model_earnings),
            status=TransactionStatus.COMPLETED,
            description=f"Private show earnings ({duration_minutes} minutes)",
            metadata={
                "show_id": show_id,
                "duration_minutes": duration_minutes,
                "original_amount": total_cost,
                "platform_fee": platform_fee
            }
        )
        
        await db.transactions.insert_many([
            viewer_transaction.model_dump(by_alias=True),
            model_transaction.model_dump(by_alias=True)
        ])
        
        # 4. Update private show record
        await db.private_shows.update_one(
            {"_id": show_id},
            {
                "$set": {
                    "status": "completed",
                    "ended_at": ended_at,
                    "duration_minutes": duration_minutes,
                    "total_cost": total_cost,
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        logger.info(f"Private show completed: {show_id}, duration: {duration_minutes}m, cost: {total_cost} tokens")
        
        return {
            "success": True,
            "message": "Private show completed successfully",
            "show_id": show_id,
            "duration_minutes": duration_minutes,
            "cost": total_cost,
            "model_earnings": model_earnings
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error ending private show: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to end private show"
        )

# Model Status Routes
@router.get("/models/live", response_model=List[ModelStreamingStatus])
async def get_live_models():
    """Get list of currently live models"""
    try:
        db = await get_database()
        
        # Get live models
        live_models = await db.model_profiles.find({
            "is_live": True,
            "is_available": True
        }).to_list(length=None)
        
        result = []
        for model in live_models:
            # Count current viewers
            viewer_count = await db.streaming_sessions.count_documents({
                "model_id": model["_id"],
                "session_type": "public",
                "status": "active"
            })
            
            result.append(ModelStreamingStatus(
                model_id=model["_id"],
                is_live=model["is_live"],
                is_available=model["is_available"],
                current_viewers=viewer_count,
                show_rate=model.get("show_rate", PRIVATE_SHOW_RATE),
                last_online=model.get("last_online")
            ))
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting live models: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get live models"
        )

@router.patch("/models/status")
async def update_model_status(
    is_live: bool,
    is_available: bool,
    current_user: User = Depends(get_current_user)
):
    """Update model's streaming status"""
    try:
        if current_user.role != UserRole.MODEL:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only models can update streaming status"
            )
        
        db = await get_database()
        
        # Update model profile
        await db.model_profiles.update_one(
            {"user_id": current_user.id},
            {
                "$set": {
                    "is_live": is_live,
                    "is_available": is_available,
                    "last_online": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        status_text = "live" if is_live else "offline"
        return {
            "success": True,
            "message": f"Status updated to {status_text}",
            "is_live": is_live,
            "is_available": is_available
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating model status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update model status"
        )

# WebRTC Signaling Routes (for development, in production use WebSocket)
@router.post("/webrtc/signal")
async def webrtc_signal(
    request: WebRTCSignalRequest,
    current_user: User = Depends(get_current_user)
):
    """Handle WebRTC signaling (offer/answer/ice-candidate)"""
    try:
        db = await get_database()
        
        # Verify session exists and user is authorized
        session = await db.streaming_sessions.find_one({"_id": request.session_id})
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Streaming session not found"
            )
        
        if current_user.id not in [session["viewer_id"], session["model_id"]]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized for this session"
            )
        
        # Store signaling data (in production, this would be sent via WebSocket)
        signal_data = {
            "_id": str(uuid.uuid4()),
            "session_id": request.session_id,
            "from_user_id": current_user.id,
            "to_user_id": request.target_user_id,
            "signal_type": request.signal_type,
            "signal_data": request.signal_data,
            "created_at": datetime.utcnow()
        }
        
        await db.webrtc_signals.insert_one(signal_data)
        
        return {
            "success": True,
            "message": "Signal sent successfully",
            "signal_id": signal_data["_id"]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing WebRTC signal: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process WebRTC signal"
        )

@router.get("/webrtc/signals/{session_id}")
async def get_webrtc_signals(
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get pending WebRTC signals for a session"""
    try:
        db = await get_database()
        
        # Get signals for this user
        signals = await db.webrtc_signals.find({
            "session_id": session_id,
            "to_user_id": current_user.id
        }).sort("created_at", 1).to_list(length=None)
        
        # Delete retrieved signals
        if signals:
            signal_ids = [signal["_id"] for signal in signals]
            await db.webrtc_signals.delete_many({"_id": {"$in": signal_ids}})
        
        return {
            "success": True,
            "signals": signals
        }
        
    except Exception as e:
        logger.error(f"Error getting WebRTC signals: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get WebRTC signals"
        )