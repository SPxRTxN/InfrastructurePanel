from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.models import User
from app.services.markdown import render_markdown
from app.web import current_user

router = APIRouter()


@router.post("/markdown/preview", response_class=HTMLResponse)
async def preview(request: Request, user: User = Depends(current_user)):
    """Предпросмотр для HTMX: принимает форму редактора, возвращает санитизированный HTML."""
    form = await request.form()
    text = next((str(form[k]) for k in ("body", "description", "text") if k in form), "")
    return render_markdown(text[:100000]) or '<p class="muted">Пусто</p>'
