"""Cart routes (Plan 5): the usual items, which apps a check would price, starting a grocery check and answering
"Pick one". Responses are the shapes web/lib/cart.ts reads; the summary itself is cart_rank.build_summary's.
Item texts are never logged."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StringConstraints

from server import cart, limits
from server.config import settings

router = APIRouter(prefix="/cart")
_bearer = HTTPBearer(auto_error=False)


# server.api includes this router, so its dependencies are looked up at call time (no import cycle).
def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    from server import api
    return api.current_user(creds)


def get_conn():
    from server import api
    yield from api.get_conn()


class ItemIn(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    qty: int = Field(ge=1, le=cart.QTY_MAX)


class CheckBody(BaseModel):
    items: list[ItemIn] = Field(min_length=1)


class ChoiceBody(BaseModel):
    idx: int = Field(ge=0)
    site: str = Field(min_length=1, max_length=32)
    product_id: Annotated[str, StringConstraints(min_length=1, max_length=200)] | int


def http_error(e: Exception) -> HTTPException:
    """A cart error as the HTTP status and user-facing detail the PWA shows as is."""
    if isinstance(e, (limits.LimitExceeded, limits.BudgetPaused)):
        return limits.to_http(e)
    return HTTPException(getattr(e, "status", 400), getattr(e, "message", "Bad request."))


@router.get("/usual")
def get_usual(user: str = Depends(current_user), conn=Depends(get_conn)):
    return {"items": cart.usual_items(conn, user, None, tz=settings().app_tz)}


@router.get("/access")
def get_access(mode: Literal["grocery", "food"] = "grocery", user: str = Depends(current_user),
               conn=Depends(get_conn)):
    access = cart.food_access(conn, user) if mode == "food" else cart.grocery_access(conn, user)
    return {"access": access}


@router.post("/checks", status_code=202)
def post_check(body: CheckBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    s = settings()
    if len(body.items) > s.cart_max_items:
        raise HTTPException(422, f"A list can have up to {s.cart_max_items} items.")
    try:
        out = cart.start_check(conn, user, [i.model_dump() for i in body.items], None, s)
    except (cart.CartError, limits.LimitExceeded, limits.BudgetPaused) as e:
        raise http_error(e) from None
    except ValueError:
        raise HTTPException(422, "Add at least one item.") from None
    return {"request_id": out["request_id"], "sites": out["sites"], "cart_checks_left": out["cart_checks_left"]}


@router.post("/checks/{request_id}/choices")
def post_choice(request_id: str, body: ChoiceBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    try:
        summary = cart.set_choice(conn, user, request_id, body.idx, body.site, str(body.product_id), settings())
    except (cart.CartError, cart.BadChoice) as e:
        raise http_error(e) from None
    return {"summary": summary}
