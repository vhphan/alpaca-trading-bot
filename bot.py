import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd
import pandas_ta as ta
# import pandas_ta as ta
import vectorbt as vbt
from alpaca.broker.client import BrokerClient
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest
from loguru import logger

from config import ALPACA_KEY_ID, ALPACA_SECRET_KEY

logger.add("bot.log", rotation="1 MB")
logger.info(ta) # so that optimize import does not delete `import pandas_ta as ta`


trading_client = TradingClient(ALPACA_KEY_ID, ALPACA_SECRET_KEY, paper=True)
broker_client = BrokerClient(ALPACA_KEY_ID, ALPACA_SECRET_KEY)
stock_history_client = StockHistoricalDataClient(
    ALPACA_KEY_ID, ALPACA_SECRET_KEY)


@dataclass
# pylint: disable=too-many-instance-attributes
class Account:
    dollar_amount: int = 10_000
    in_position_quantity: float = 0
    pending_orders: dict|None = None
    removed_order_ids: list|None = None
    df: pd.DataFrame = pd.DataFrame()
    qty: int = 1


class Bot:
    """
    trading bot class
    """

    def __init__(self, symbol, atr_band=1, atr_stretch=1):
        self.symbol = symbol
        self.atr_band = atr_band
        self.atr_stretch = atr_stretch
        self.acc = Account(pending_orders={}, removed_order_ids=[])

    def get_position(self):
        try:
            self.acc.in_position_quantity = trading_client.get_open_position(
                'SPY').qty
        except APIError as err:
            logger.debug(err)
            self.acc.in_position_quantity = 0

    @staticmethod
    def close_position():
        try:
            trading_client.close_position('SPY')
        except APIError as err:
            logger.debug(err)

    @staticmethod
    def check_market_open():
        """
        check if market is open
        """
        logger.info("checking if market is open")
        clock = trading_client.get_clock()
        if not clock.is_open:
            logger.info("market is closed")
            time_till_open = (clock.next_open - clock.timestamp).total_seconds()
            logger.info(f"sleeping for {time_till_open} seconds until market opens")
            time.sleep(round(time_till_open, 0))

    def check_order_status(self):
        logger.info("checking order status")
        self.check_market_open()
        for order_id in self.acc.pending_orders:
            logger.info("found pending orders")

            order = trading_client.get_order_by_id(order_id=order_id)

            if order.filled_at is None:
                logger.info("order has not been filled at.")
                continue

            if order.filled_at is not None:
                filled_message = f"{order.side} order"
                filled_message += f"{order.filled_qty} {order.symbol} "
                filled_message += f"was filled at {order.filled_at} "
                filled_message += f"at price {order.filled_avg_price}"
                logger.info(filled_message)
                self.acc.removed_order_ids.append(order_id)

        self.get_position()

        for order_id in self.acc.removed_order_ids:
            del self.acc.pending_orders[order_id]

    def send_order(self, limit_price, side=OrderSide.BUY):
        """
        send buy/sell order
        """
        logger.info(f"sending order for {self.acc.dollar_amount=} {side=}")

        limit_order_data = LimitOrderRequest(
            symbol=self.symbol,
            limit_price=round(limit_price, 0),
            # notional=self.acc.dollar_amount,
            qty=self.acc.qty,
            side=side,
            time_in_force=TimeInForce.GTC,
        )

        order = trading_client.submit_order(order_data=limit_order_data)
        logger.info(f"order submitted {order}")
        self.acc.pending_orders[order.id] = order

    def get_bars(self):
        """
        get ohlc bars of symbol and populate with required indicators
        """
        logger.info("getting bars")

        symbol = self.symbol
        atr_band = self.atr_band
        atr_stretch = self.atr_stretch

        request_params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=datetime.now() - timedelta(hours=360),
        )

        bars = stock_history_client.get_stock_bars(request_params)

        df: pd.DataFrame = (
            bars.df.loc[(symbol,)]
            .assign(ema_long=lambda x: x.ta.ema(length=200))
            .assign(ema_short=lambda x: x.ta.ema(length=20))
            .assign(atr=lambda x: x.ta.atr(length=5))
            .assign(atr_top=lambda x: x.ema_short + x.atr * atr_band)
            .assign(atr_bottom=lambda x: x.ema_short - x.atr * atr_band)
            .assign(buy_limit_price=None)
            .assign(
                setup_condition=lambda d: (
                                                  d.close < d.atr_bottom) & (d.low > d.ema_long)
            )
        )

        self.acc.df = pd.concat([self.acc.df, df]).drop_duplicates().reset_index()

        logger.info("cancelling all orders")
        cancel_statuses = trading_client.cancel_orders()
        logger.info(cancel_statuses)

        last_data = df.iloc[-1]
        if last_data.setup_condition:
            buy_limit = last_data.low - (last_data.atr * atr_stretch)
            self.send_order(limit_price=buy_limit, side=OrderSide.BUY)

        if last_data.high >= last_data.ema_short or last_data.close < last_data.ema_long:
            logger.info("closing position")
            self.close_position()

        logger.info(df)

    def test_order(self):
        last_data = self.acc.df.iloc[-1]
        buy_limit = last_data.low
        self.send_order(limit_price=buy_limit, side=OrderSide.BUY)


if __name__ == "__main__":
    logger.info('running main')
    bot = Bot(symbol="SPY")
    bot.get_bars()
    # bot.test_order()

    manager = vbt.ScheduleManager()
    # manager.every(2).minutes.at(':00').do(bot.check_order_status)
    manager.every(2).minutes.do(bot.check_order_status)
    manager.every().hour.at(':00').do(bot.get_bars)
    manager.start()
