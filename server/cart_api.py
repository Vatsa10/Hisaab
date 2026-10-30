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


def _refuse_if_every_app_is_cooling(conn, user: str) -> None:
    """503 + Retry-After when every app this check would price is in back-off (api.raise_if_cooling). While only
    some are cooling the check still runs and those apps answer "unavailable"; this runs before any limit is used."""
    from server import api
    ready = [x for x, st in cart.grocery_access(conn, user).items() if st == "ok"]
    refusal = None
    for site in ready:
        try:
            api.raise_if_cooling(conn, site)
        except HTTPException as e:
            refusal = refusal or e
            continue
        return
    if refusal is not None:
        raise refusal


@router.post("/checks", status_code=202)
def post_check(body: CheckBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    s = settings()
    if len(body.items) > s.cart_max_items:
        raise HTTPException(422, f"A list can have up to {s.cart_max_items} items.")
    _refuse_if_every_app_is_cooling(conn, user)
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


# --- cart fill (Plan 6) -----------------------------------------------------------------------------------------

class FillBody(BaseModel):
    site: str = Field(min_length=1, max_length=32)


def fill_error(e: Exception, site: str | None = None) -> HTTPException:
    """A fill error as the status and exact user-facing detail the PWA shows as is."""
    from server import fill, site_health
    app = fill._app(site) if site else ""
    if isinstance(e, (limits.LimitExceeded, limits.BudgetPaused)):
        return limits.to_http(e)
    if isinstance(e, site_health.SiteCooling):
        return site_health.to_http(e)
    if isinstance(e, fill.NotFound):
        return HTTPException(404, "not found")
    if isinstance(e, fill.NotReady):
        return HTTPException(409, "Prices are still loading.")
    if isinstance(e, fill.FoodNotFillable):
        return HTTPException(422, fill.FOOD_NOTE)
    if isinstance(e, fill.NoPrices):
        return HTTPException(422, f"{app} has no prices in this check.")
    if isinstance(e, fill.Unavailable):
        return HTTPException(503, f"Filling carts on {app} isn't available right now.")
    if isinstance(e, fill.NeedsConnection):
        return HTTPException(409, e.message)
    if isinstance(e, fill.Stale):
        return HTTPException(409, "These prices are over 30 minutes old. Check again first.")
    if isinstance(e, fill.AlreadyFilled):
        return HTTPException(409, f"This cart was already filled on {app}.")
    if isinstance(e, fill.NothingToAdd):
        return HTTPException(409, f"Nothing to add on {app}.")
    if isinstance(e, fill.FillRunning):
        return HTTPException(409, "A cart is already being filled.")
    if isinstance(e, fill.NotConfirmable):
        return HTTPException(409, "These prices have expired. Check again.")
    raise e


@router.get("/checks/{request_id}/fill")
def get_fill_options(request_id: str, user: str = Depends(current_user), conn=Depends(get_conn)):
    from server import fill
    try:
        return fill.fill_options(conn, user, request_id, None, settings())
    except (fill.NotFound, fill.NotReady) as e:
        raise fill_error(e) from None


@router.post("/checks/{request_id}/fill", status_code=202)
def post_fill(request_id: str, body: FillBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    from server import fill, site_health
    try:
        fill._request(conn, user, request_id)               # not the owner's: 404 before anything else
        if body.site in fill.FILL_SITES:
            site_health.raise_if_cooling(conn, body.site)   # a site that is limiting us is not asked again
        return fill.start_fill(conn, user, request_id, body.site, None, settings())
    except (fill.NotFound, fill.NotReady, fill.FoodNotFillable, fill.NoPrices, fill.Unavailable,
            fill.NeedsConnection, fill.Stale, fill.AlreadyFilled, fill.NothingToAdd, fill.FillRunning,
            limits.LimitExceeded, limits.BudgetPaused, site_health.SiteCooling) as e:
        raise fill_error(e, body.site) from None


@router.post("/fills/{fill_id}/confirm", status_code=202)
def post_confirm(fill_id: str, user: str = Depends(current_user), conn=Depends(get_conn)):
    from server import fill, site_health
    old = fill.get_fill(conn, fill_id, user)
    if old is None:
        raise HTTPException(404, "not found")
    site = old["site"]
    try:
        if site in fill.FILL_SITES:
            site_health.raise_if_cooling(conn, site)
        return fill.confirm_fill(conn, user, fill_id, None, settings())
    except (fill.NotConfirmable, fill.Unavailable, fill.NeedsConnection, limits.LimitExceeded,
            limits.BudgetPaused, site_health.SiteCooling) as e:
        raise fill_error(e, site) from None
