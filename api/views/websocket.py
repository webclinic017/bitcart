from fastapi import APIRouter, HTTPException
from fastapi.security import SecurityScopes
from starlette.endpoints import WebSocketEndpoint
from starlette.status import WS_1008_POLICY_VIOLATION

from api import crud, models, utils
from api.ext.moneyformat import currency_table
from api.invoices import InvoiceStatus

router = APIRouter()


class GenericWebsocketEndpoint(WebSocketEndpoint):
    NAME: str
    MODEL: models.db.Model
    REQUIRE_AUTH: bool = True

    subscriber = None

    async def on_connect(self, websocket, **kwargs):
        await websocket.accept()
        self.access_token = None
        self.user = None
        try:
            self.object_id = websocket.path_params["model_id"]
            if self.REQUIRE_AUTH:
                self.access_token = websocket.query_params["token"]
        except KeyError:
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return
        if self.REQUIRE_AUTH:
            try:
                self.user = await utils.authorization.AuthDependency(token=self.access_token)(
                    None, SecurityScopes([f"{self.NAME}_management"])
                )
            except HTTPException:
                await websocket.close(code=WS_1008_POLICY_VIOLATION)
                return
        self.object = await utils.database.get_object(self.MODEL, self.object_id, self.user, raise_exception=False)
        if not self.object:
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return
        if await self.maybe_exit_early(websocket):
            return
        self.subscriber = await utils.redis.make_subscriber(f"{self.NAME}:{self.object_id}")
        utils.tasks.create_task(self.poll_subs(websocket))

    async def poll_subs(self, websocket):
        async for message in utils.redis.listen_channel(self.subscriber):
            await websocket.send_json(message)

    async def on_disconnect(self, websocket, close_code):
        if self.subscriber:
            await self.subscriber.unsubscribe(f"channel:{self.NAME}:{self.object_id}")

    async def maybe_exit_early(self, websocket):
        return False


@router.websocket_route("/wallets/{model_id}")
class WalletNotify(GenericWebsocketEndpoint):
    NAME = "wallet"
    MODEL = models.Wallet


@router.websocket_route("/invoices/{model_id}")
class InvoiceNotify(GenericWebsocketEndpoint):
    NAME = "invoice"
    MODEL = models.Invoice
    REQUIRE_AUTH = False

    async def maybe_exit_early(self, websocket):
        if self.object.status in [InvoiceStatus.EXPIRED, InvoiceStatus.COMPLETE]:
            await websocket.send_json({
                "status": self.object.status,
                "exception_status": self.object.exception_status,
                "sent_amount": currency_table.format_decimal(
                    "",
                    self.object.sent_amount,
                    divisibility=crud.invoices.find_sent_amount_divisibility(
                        self.object.id, self.object.payments, self.object.paid_currency
                    ),
                ),
                "paid_currency": self.object.paid_currency,
            })
            await websocket.close()
            return True
        return False
