import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import cached_property
from typing import Any, Iterable

from ghostcompanion.core.entity.cash_balance import CashBalance
from ghostcompanion.core.entity.onchain_movement import OnChainMovement
from ghostcompanion.core.entity.trade import Trade
from ghostcompanion.core.entity.transaction_type import TransactionType
from ghostcompanion.core.ports.coinbase import CoinbasePort

logger = logging.getLogger(__name__)

_TRADING_FEE_DESCRIPTION = "Trading fee"
_BALANCE_CORRECTION_DESCRIPTION = "Balance correction"
_REWARDS = {
    "interest": "Interest earned",
    "staking_reward": "Staking reward",
    "inflation_reward": "Staking reward",
}


def _parse_created_at(transaction: dict[str, Any]) -> datetime:
    return datetime.strptime(transaction["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


class CoinbaseProvider:
    ACCOUNT_NAME = "Coinbase"

    def __init__(self, coinbase_api: CoinbasePort) -> None:
        self.coinbase_api = coinbase_api

    def get_coins(self) -> list[str]:
        coins = self.coinbase_api.get_accounts()
        coins = self._filter_not_traded_coins(coins)
        coins = self._filter_fiat(coins)
        # A staked coin has an account of its own besides the coin's wallet.
        return list(dict.fromkeys(x["currency"]["code"] for x in coins))

    def get_trades(self, coin: str) -> list[Trade]:
        transactions = self.coinbase_api.get_transactions(coin)

        trades = []
        unbooked: Counter[str] = Counter()
        for transaction in transactions:
            match transaction:
                case _ if self._is_coinbase_earn(transaction):
                    trades.append(self._adapt_free_coins(transaction))
                # Credited seconds after some simple buys, undocumented by
                # Coinbase; like Earn, coins received for nothing.
                case _ if self._is_price_improvement(transaction):
                    trades.append(self._adapt_free_coins(transaction))
                case _ if transaction["type"] in _REWARDS:
                    trades.append(
                        self._adapt_free_coins(
                            transaction, _REWARDS[transaction["type"]]
                        )
                    )
                case _ if self._is_trade(transaction):
                    trades.append(self._adapt_trade(transaction))
                case _ if self._is_buy_or_sell(transaction):
                    trades.append(self._adapt_buy_sell(transaction))
                case _ if self._is_advanced_trade_fill(transaction):
                    if self._quote_currency(transaction) in self._fiat_currencies:
                        trades.append(self._adapt_advanced_trade_fill(transaction))
                    else:
                        trades += self._adapt_crypto_quoted_fill(transaction)
                case _ if transaction["type"] == "send":
                    continue
                case _:
                    unbooked[transaction["type"]] += 1

        for transaction_type, count in unbooked.items():
            logger.warning(
                f"`{self.ACCOUNT_NAME}`: {count} `{transaction_type}` transaction(s)"
                f" of `{coin}` aren't booked"
            )

        return trades + self._balance_correction(coin, transactions, trades)

    def get_movements(self, coin: str) -> list[OnChainMovement]:
        """Coins sent from or received into Coinbase, which may be transfers from or
        to the user's own wallets."""
        movements = []
        for transaction in self.coinbase_api.get_transactions(coin):
            if self._is_booked_send(transaction):
                movements += self._adapt_send(transaction)

        return movements

    @classmethod
    def _is_booked_send(cls, transaction: dict[str, Any]) -> bool:
        return (
            transaction["type"] == "send"
            and transaction["status"] == "completed"
            and not cls._is_coinbase_earn(transaction)
            and Decimal(transaction["amount"]["amount"]) != 0
        )

    @cached_property
    def _accounts_by_coin(self) -> dict[str, list[dict[str, Any]]]:
        accounts: dict[str, list[dict[str, Any]]] = {}
        for account in self.coinbase_api.get_accounts():
            accounts.setdefault(account["currency"]["code"], []).append(account)
        return accounts

    def _balance_correction(
        self, coin: str, transactions: list[dict[str, Any]], trades: list[Trade]
    ) -> list[Trade]:
        accounts = self._accounts_by_coin.get(coin, [])
        balance = sum(
            (Decimal(x["balance"]["amount"]) for x in accounts), Decimal("0")
        )
        signs = {TransactionType.BUY: 1, TransactionType.SELL: -1}
        moves = [
            (x.executed_at, signs.get(x.transaction_type, 0) * x.quantity)
            for x in trades
        ] + [
            (_parse_created_at(x), Decimal(x["amount"]["amount"]))
            for x in transactions
            if self._is_booked_send(x)
        ]
        booked = sum((quantity for _, quantity in moves), Decimal("0"))
        exponents = [
            int(x["currency"]["exponent"])
            for x in accounts
            if x["currency"].get("exponent") is not None
        ]
        unit = Decimal(1).scaleb(-max(exponents)) if exponents else None
        if unit is None or not moves or abs(balance - booked) >= unit:
            if balance != booked:
                logger.warning(
                    f"`{self.ACCOUNT_NAME}` holds {balance} {coin}, but its"
                    f" transactions add up to {booked}"
                )
            return []

        currency = transactions[0]["native_amount"]["currency"]
        corrections = []
        held = Decimal("0")
        for executed_at, quantity in sorted(moves, key=lambda x: (x[0], x[1] < 0)):
            held += quantity
            if held != 0 and abs(held) < unit:
                corrections.append(
                    self._adapt_correction(coin, currency, executed_at, -held)
                )
                held = Decimal("0")

        if balance != held:
            corrections.append(
                self._adapt_correction(
                    coin, currency, max(x[0] for x in moves), balance - held
                )
            )

        return corrections

    @staticmethod
    def _adapt_correction(
        coin: str, currency: str, after: datetime, quantity: Decimal
    ) -> Trade:
        return Trade(
            currency=currency,
            description=_BALANCE_CORRECTION_DESCRIPTION,
            executed_at=after + timedelta(seconds=1),
            fee=Decimal("0"),
            quantity=abs(quantity),
            symbol=coin,
            transaction_type=(
                TransactionType.BUY if quantity > 0 else TransactionType.SELL
            ),
            unit_price=Decimal("0"),
        )

    @staticmethod
    def _filter_not_traded_coins(
        coins: list[dict[str, Any]],
    ) -> Iterable[dict[str, Any]]:
        return filter(
            lambda x: not (
                float(x["balance"]["amount"]) == 0.0
                and x["created_at"] == x["updated_at"]
            ),
            coins,
        )

    @staticmethod
    def _filter_fiat(coins: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        return filter(lambda x: not x["type"] == "fiat", coins)

    @cached_property
    def _fiat_currencies(self) -> set[str]:
        return {
            x["currency"]["code"]
            for x in self.coinbase_api.get_accounts()
            if x["type"] == "fiat"
        }

    @staticmethod
    def _is_buy_or_sell(transaction: dict[str, Any]) -> bool:
        return True if transaction["type"] in ("buy", "sell") else False

    @staticmethod
    def _is_coinbase_earn(transaction: dict[str, Any]) -> bool:
        if (
            transaction["type"] == "send"
            and transaction.get("from", {}).get("name", "") == "Coinbase Earn"
        ):
            return True

        return False

    @classmethod
    def _adapt_send(cls, transaction: dict[str, Any]) -> list[OnChainMovement]:
        amount = Decimal(transaction["amount"]["amount"])
        symbol = transaction["amount"]["currency"]
        executed_at = _parse_created_at(transaction)
        network = transaction.get("network") or {}
        transaction_fee = network.get("transaction_fee")

        # A send's amount already includes the network fee Coinbase charged, so
        # the receiving side got that much less.
        fee = Decimal("0")
        fee_movements = []
        if amount < 0 and transaction_fee:
            if transaction_fee["currency"] == symbol:
                fee = Decimal(transaction_fee["amount"])
            else:
                fee_movements.append(
                    OnChainMovement(
                        account=cls.ACCOUNT_NAME,
                        currency=transaction["native_amount"]["currency"],
                        executed_at=executed_at,
                        fee=Decimal(transaction_fee["amount"]),
                        market_unit_price=Decimal("0"),
                        quantity=Decimal("0"),
                        symbol=transaction_fee["currency"],
                    )
                )

        transaction_hash = network.get("hash")
        movement = OnChainMovement(
            account=cls.ACCOUNT_NAME,
            currency=transaction["native_amount"]["currency"],
            destination_address=(
                (transaction.get("to") or {}).get("address") if amount < 0 else None
            ),
            executed_at=executed_at,
            fee=fee,
            market_unit_price=abs(Decimal(transaction["native_amount"]["amount"]))
            / abs(amount),
            quantity=amount + fee if amount < 0 else amount,
            symbol=symbol,
            transaction_id=transaction_hash,
        )

        return [movement] + fee_movements

    @staticmethod
    def _is_trade(transaction: dict[str, Any]) -> bool:
        return True if transaction["type"] == "trade" else False

    @staticmethod
    def _adapt_buy_sell(transaction: dict[str, Any]) -> Trade:
        transaction_type = transaction["type"]

        return Trade(
            currency=transaction[transaction_type]["total"]["currency"],
            executed_at=_parse_created_at(transaction),
            fee=Decimal(
                transaction[transaction_type].get("fee", {}).get("amount", "0.0")
            ),
            quantity=abs(Decimal(transaction["amount"]["amount"])),
            symbol=transaction["amount"]["currency"],
            transaction_type=TransactionType[transaction_type.upper()],
            value=abs(Decimal(transaction[transaction_type]["subtotal"]["amount"])),
        )

    @staticmethod
    def _is_price_improvement(transaction: dict[str, Any]) -> bool:
        return transaction["type"] == "retail_simple_price_improvement"

    @staticmethod
    def _adapt_free_coins(
        transaction: dict[str, Any], description: str | None = None
    ) -> Trade:
        return Trade(
            currency=transaction["native_amount"]["currency"],
            description=description,
            executed_at=_parse_created_at(transaction),
            fee=Decimal("0"),
            quantity=abs(Decimal(transaction["amount"]["amount"])),
            symbol=transaction["amount"]["currency"],
            transaction_type=TransactionType["BUY"],
            value=Decimal("0"),
        )

    @staticmethod
    def _adapt_trade(transaction: dict[str, Any]) -> Trade:
        amount = Decimal(transaction["amount"]["amount"])

        transaction_type = "sell" if amount < 0 else "buy"

        return Trade(
            currency=transaction["native_amount"]["currency"],
            executed_at=_parse_created_at(transaction),
            fee=Decimal("0"),
            quantity=abs(amount),
            symbol=transaction["amount"]["currency"],
            transaction_type=TransactionType[transaction_type.upper()],
            value=abs(Decimal(transaction["native_amount"]["amount"])),
        )

    @staticmethod
    def _is_advanced_trade_fill(transaction: dict[str, Any]) -> bool:
        return transaction["type"] == "advanced_trade_fill"

    @staticmethod
    def _quote_currency(transaction: dict[str, Any]) -> str:
        return transaction["advanced_trade_fill"]["product_id"].split("-")[-1]

    @classmethod
    def _adapt_advanced_trade_fill(cls, transaction: dict[str, Any]) -> Trade:
        fill = transaction["advanced_trade_fill"]
        quantity = abs(Decimal(transaction["amount"]["amount"]))

        # Coinbase charges the commission in the quote currency apart from the
        # fill, so neither side's amount includes it.
        return Trade(
            currency=cls._quote_currency(transaction),
            executed_at=_parse_created_at(transaction),
            fee=Decimal(fill["commission"]),
            quantity=quantity,
            symbol=transaction["amount"]["currency"],
            transaction_type=TransactionType[fill["order_side"].upper()],
            value=quantity * Decimal(fill["fill_price"]),
        )

    @classmethod
    def _adapt_crypto_quoted_fill(cls, transaction: dict[str, Any]) -> list[Trade]:
        amount = Decimal(transaction["amount"]["amount"])
        symbol = transaction["amount"]["currency"]
        currency = transaction["native_amount"]["currency"]
        executed_at = _parse_created_at(transaction)

        trades = []
        if amount != 0:
            trades.append(
                Trade(
                    currency=currency,
                    executed_at=executed_at,
                    fee=Decimal("0"),
                    quantity=abs(amount),
                    symbol=symbol,
                    transaction_type=(
                        TransactionType.BUY if amount > 0 else TransactionType.SELL
                    ),
                    value=abs(Decimal(transaction["native_amount"]["amount"])),
                )
            )

        commission = Decimal(transaction["advanced_trade_fill"]["commission"])
        if symbol == cls._quote_currency(transaction) and commission > 0:
            trades.append(
                Trade(
                    currency=currency,
                    description=_TRADING_FEE_DESCRIPTION,
                    executed_at=executed_at + timedelta(seconds=1),
                    fee=Decimal("0"),
                    quantity=commission,
                    symbol=symbol,
                    transaction_type=TransactionType.SELL,
                    unit_price=Decimal("0"),
                )
            )

        return trades

    # Cash Balance Methods

    def get_current_cash_balance(self, currency: str = "USD") -> CashBalance:
        """Get the current cash balance from Coinbase account.

        Returns a single CashBalance with today's date and the current
        cash amount from fiat accounts matching the specified currency.

        Args:
            currency: The account currency (default "USD")

        Returns:
            CashBalance with today's date and current cash amount
        """
        cash_amount = self.coinbase_api.get_current_cash_balance(currency)
        return CashBalance(date=date.today(), amount=cash_amount, currency=currency)
