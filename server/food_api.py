"""Food compare routes (Plan 5 T12). Mounted by server/api.py. Bodies never carry a menu URL: unknown fields are
rejected, and the URL is read from the user's own restaurant search results."""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from server import cart, food_cart, limits
from server.config import settings

router = APIRouter(prefix="/cart/food")
_bearer = HTTPBearer(auto_error=False)

NO_APP = "Connect Swiggy or Zomato to compare."
Id = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# server.api is imported lazily so either module can be imported first.
def _user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    from server import api
    return api.current_user(creds)


def _conn():
    from server import api
    yield from api.get_conn()


class MenusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    swiggy_id: Id | None = None
    zomato_id: Id | None = None


class RestaurantBody(MenusBody):
    confirmed: bool = False
    declined: list[str] = Field(default_factory=list, max_length=2)


class DishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    qty: int = Field(ge=1, le=20)
    variant: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] | None = None
    veg: bool | None = None


class CheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    restaurant: RestaurantBody
    items: list[DishBody] = Field(min_length=1)


def _http(e: Exception) -> HTTPException:
    if isinstance(e, cart.NoAddress):
        return HTTPException(409, "Add your delivery address first.")
    if isinstance(e, cart.NoConnectedApp):
        return HTTPException(409, NO_APP)
    if isinstance(e, cart.CheckRunning):
        return HTTPException(409, "A cart check is already running.")
    if isinstance(e, food_cart.LookupBusy):
        return HTTPException(409, "A lookup is already running. Try again in a moment.")
    if isinstance(e, cart.Unavailable):
        return HTTPException(503, "Cart checks are unavailable right now.")
    if isinstance(e, (limits.LimitExceeded, limits.BudgetPaused)):
        return limits.to_http(e)
    return HTTPException(422, str(e))


_HANDLED = (cart.NoAddress, cart.NoConnectedApp, cart.CheckRunning, cart.Unavailable, food_cart.LookupBusy,
            limits.LimitExceeded, limits.BudgetPaused, ValueError)


def _respond(out: dict):
    return JSONResponse(jsonable_encoder(out), status_code=200 if out["status"] == "ready" else 202)


@router.get("/usual")
def usual(user: str = Depends(_user), conn=Depends(_conn)):
    return {"restaurants": food_cart.usual_restaurants(conn, user)}


@router.get("/restaurants")
def restaurants(q: Annotated[str, Query(min_length=2, max_length=60)], user: str = Depends(_user),
                conn=Depends(_conn)):
    try:
        return _respond(food_cart.lookup_restaurants(conn, user, q.strip(), _now(), settings()))
    except _HANDLED as e:
        raise _http(e) from None


@router.post("/menus")
def menus(body: MenusBody, user: str = Depends(_user), conn=Depends(_conn)):
    listings = {site: rid for site, rid in (("swiggy", body.swiggy_id), ("zomato", body.zomato_id)) if rid}
    try:
        return _respond(food_cart.lookup_menus(conn, user, listings, _now(), settings()))
    except _HANDLED as e:
        raise _http(e) from None


@router.post("/checks", status_code=202)
def checks(body: CheckBody, user: str = Depends(_user), conn=Depends(_conn)):
    s = settings()
    if len(body.items) > s.food_max_items:
        raise HTTPException(422, f"Pick between 1 and {s.food_max_items} dishes.")
    r = body.restaurant
    restaurant = {"name": r.name, "swiggy_id": r.swiggy_id, "zomato_id": r.zomato_id, "confirmed": r.confirmed,
                  "declined": r.declined}
    items = [d.model_dump(exclude_none=True) for d in body.items]
    try:
        return food_cart.start_food_check(conn, user, restaurant, items, _now(), s)
    except _HANDLED as e:
        raise _http(e) from None
