from fastapi import APIRouter, Depends, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Notification, User
from app.web import current_user

router = APIRouter(prefix="/api/notifications")


@router.get("")
def list_notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = db.scalars(
        select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(20)
    ).all()
    return {
        "unread": sum(1 for n in items if not n.is_read),
        "items": [
            {"id": n.id, "title": n.title, "body": n.body, "link": n.link, "read": n.is_read,
             "created_at": n.created_at.isoformat() + "Z"}
            for n in items
        ],
    }


@router.post("/read-all")
def read_all(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(update(Notification).where(Notification.user_id == user.id).values(is_read=True))
    db.commit()
    return Response(status_code=204)
