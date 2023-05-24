import asyncio
import json
import logging
import re
import sys
from enum import Enum
from typing import NamedTuple
import ta
import toml
from sanic import Sanic
from sanic.response import json as sanic_json
from tenacity import after_log, retry, retry_if_result, stop_after_attempt, wait_fixed
import traceback
import MetaTrader5 as mt5
import pytz
from datetime import datetime

# 日志配置
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[
                        logging.StreamHandler(),
                        logging.FileHandler("trading_bot.log", encoding='utf-8')
                    ])
logger = logging.getLogger(__name__)


# class Config:
#     def __init__(self, config_file):
#         self.config = toml.load(config_file)

#     def get_parameters(self):
#         return {
#             'mt5_login': self.config['mt5']['login'],
#             'mt5_password': self.config['mt5']['password'],
#             'mt5_server': self.config['mt5']['server'],
#             'trading_stop_loss_pips': self.config['trading']['stop_loss_pips'],
#             'trading_take_profit_pips': self.config['trading']['take_profit_pips'],
#             'dynamic_sltp': self.config['DynamicSLTP'],
#             'magic_number': self.config['trading']['magic_number'],
#             'deviation': self.config['trading']['deviation']
#         }

class Config:
    def __init__(self, config_file):
        self.config = toml.load(config_file)

    def get_parameters(self):
        mt5_login = self.config['mt5']['login']
        mt5_password = self.config['mt5']['password']
        mt5_server = self.config['mt5']['server']
        trading_stop_loss_pips = self.config['trading']['stop_loss_pips']
        trading_take_profit_pips = self.config['trading']['take_profit_pips']
        dynamic_sltp = self.config['DynamicSLTP']
        magic_number = self.config['trading']['magic_number']
        deviation = self.config['trading']['deviation']

        return mt5_login, mt5_password, mt5_server, trading_stop_loss_pips, trading_take_profit_pips, dynamic_sltp, magic_number, deviation

# 异步Sanic应用
app = Sanic("AlgoBot")


class DynamicStopLossTakeProfit:
    def __init__(self, symbol, action, take_profit, stop_loss, boll_periods, rsi_periods, trailing_stop_distance):
        self.symbol = symbol
        self.action = action
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.boll_periods = boll_periods
        self.rsi_periods = rsi_periods
        self.trailing_stop_distance = trailing_stop_distance

    def get_price_data(self, timeframe, num_periods):
        rates = mt5.copy_rates_from_pos(self.symbol, timeframe, 0, num_periods)
        price_data = [rate['close'] for rate in rates]
        return price_data

    def calculate_average_true_range(self) -> float:
        prices = self.get_price_data(mt5.TIMEFRAME_D1, 14)
        high_prices = [bar.high for bar in prices]
        low_prices = [bar.low for bar in prices]
        close_prices = [bar.close for bar in prices]

        atr = ta.average_true_range(high_prices, low_prices, close_prices, window=14)[-1]

        return atr

    def calculate_moving_average(self) -> float:
        prices = self.get_price_data(mt5.TIMEFRAME_D1, 14)
        close_prices = [bar.close for bar in prices]

        ma = ta.sma(close_prices, window=14)[-1]

        return ma

    def adjust_trailing_stop(self):
        price_data = self.get_price_data(mt5.TIMEFRAME_D1, 14)
        # 如果是多单，且价格上涨，提高止损位
        if self.action == "BUY" and price_data[-1] > self.stop_loss + self.trailing_stop_distance:
            self.stop_loss = price_data[-1] - self.trailing_stop_distance

        # 如果是空单，且价格下跌，降低止损位
        elif self.action == "SELL" and price_data[-1] < self.stop_loss - self.trailing_stop_distance:
            self.stop_loss = price_data[-1] + self.trailing_stop_distance

    def calculate_fixed_sl_tp(self, stop_loss_pips: float, take_profit_pips: float) -> tuple[float, float]:
        # 获取当前市场价格
        current_price = mt5.symbol_info_tick(self.symbol).ask

        # 计算止盈价和止损价
        stop_loss_price = current_price - stop_loss_pips * mt5.symbol_info(self.symbol).point
        take_profit_price = current_price + take_profit_pips * mt5.symbol_info(self.symbol).point

        return stop_loss_price, take_profit_price

    def adjust_stop_loss_take_profit(self):
        price_data = self.get_price_data(mt5.TIMEFRAME_D1, 14)
        atr = self.calculate_average_true_range()
        ma = self.calculate_moving_average()

        # adjust stop loss and take profit based on action
        if self.action == "BUY":
            self.take_profit = ma + atr
            self.stop_loss = ma - atr
        elif self.action == "SELL":
            self.take_profit = ma - atr
            self.stop_loss = ma + atr
        return stop_loss,take_profit

    def adjust_stop_loss_take_profit(self):
        price_data = self.get_price_data(mt5.TIMEFRAME_D1, 14)
        atr = self.calculate_average_true_range()
        ma = self.calculate_moving_average()

        if stop_loss_take_profit_type == "trailing":
            self.adjust_trailing_stop()
        elif stop_loss_take_profit_type == "fixed":
            self.stop_loss, self.take_profit = self.calculate_fixed_sl_tp(stop_loss, take_profit)
        elif stop_loss_take_profit_type == "atr":
            self.adjust_stop_loss_take_profit_by_atr(atr)
        elif stop_loss_take_profit_type == "ma":
            self.adjust_stop_loss_take_profit_by_ma(ma)
        else:
            logging.error("Invalid stop loss/take profit type.")

    def modify_sl_tp(self):
        """ 修改止盈止损 """
        # 注意这里是根据 action 方向来确定 stoploss 和 takeprofit 的价格
        stoploss_price = self.stop_loss if self.action == "BUY" else self.stop_loss
        takeprofit_price = self.take_profit if self.action == "BUY" else self.take_profit

        # 这里是调用 MT5 的函数修改订单
        result = mt5.order_modify(order, stoploss=stoploss_price, takeprofit=takeprofit_price)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print("Failed to modify order: ", result.comment)
        else:
            print(f"Order modified successfully: SL: {stoploss_price}, TP: {takeprofit_price}")

    def update_stop_loss_take_profit(self, ticket: int, type: int, symbol: str, stop_loss: float, take_profit: float):
        order_select = mt5.order_select(ticket, type)
        if not order_select:
            logging.error(f"Failed to select order {ticket}")
            return

        order_modify = mt5.order_modify(ticket, stoploss=stop_loss, takeprofit=take_profit)
        if not order_modify:
            logging.error(f"Failed to modify order {ticket}")
            return

        logging.info(f"Stop loss and take profit of order {ticket} modified successfully!")


class ActionType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    CLOSE = "CLOSE"


class TradingSignal(NamedTuple):
    action: str
    symbol: str
    volume: float
    open_position: str
    position_closed: str

    def normalize_action(self):
        if self.action.lower() in ['enter_long', 'buy']:
            self = self._replace(action='BUY')
        elif self.action.lower() in ['enter_short', 'sell']:
            self = self._replace(action='SELL')
        return self

    @classmethod
    def from_webhook(cls, webhook_data: str):
        pattern = r'action=(\w+);symbol=([\w/]+);volume=(.*);open_position=(.*);position_closed=(\w+_\d+)?'
        match = re.match(pattern, webhook_data)
        if not match:
            logging.error("Invalid webhook data: %s", webhook_data)
            raise ValueError("Invalid webhook data")
        logging.info("Parsed signal: %s", match.groups())

        return cls(*match.groups())


def is_requote_error(result):
    if isinstance(result, Exception):
        return "Trade failed" in str(result)
    return False

class TradingBot:
    def __init__(self, login, password, server, dynamic_sltp, magic_number, deviation):
        self.login = login
        self.password = password
        self.server = server
        self.dynamic_sltp = dynamic_sltp
        self.magic_number = magic_number
        self.deviation = deviation
        self.initialize_mt5()


    def initialize_mt5(self):
        if not mt5.initialize():
            logging.error("initialize() failed, error code =", mt5.last_error())
            quit()

        authorized = mt5.login(self.login, self.password, server=self.server)
        if not authorized:
            logging.error(f"Failed to connect to trade account with login = {self.login}")
            logging.error(f"Error code = , {mt5.last_error()}")
            quit()

        logging.info(f"Connected to trade account with login = {self.login}")

    async def execute_order(self, signal: TradingSignal):
        logging.info("Executing order: %s", signal)
        try:
            if self.is_trading_time() and signal.open_position:
                if signal.action == "BUY":
                    self.enter_long(signal)
                    if signal.position_closed:
                        self.close_position_by_comment(signal.position_closed)
                elif signal.action == "SELL":
                    self.enter_short(signal)
                    if signal.position_closed:
                        self.close_position_by_comment(signal.position_closed)
                else:
                    logging.error("Action is not supported.")
            else:
                if not self.is_trading_time():
                    self.close_all_positions()
                    logging.error("Now is not trading time, all positions closed.")
                else:
                    logging.error("Correct open position (like Long_12345) is required!")
        except Exception as err:
            logging.error(f"An error occurred: {err}")
            traceback.print_exc()

    @retry(retry=retry_if_result(is_requote_error), stop=stop_after_attempt(5), wait=wait_fixed(1),
           reraise=True, after=after_log(logger, logging.DEBUG))
    def enter_long(self, signal: TradingSignal):
        logging.debug(f"Entering long with signal: {signal}")
        symbol_info_tick = mt5.symbol_info_tick(signal.symbol)
        ask = symbol_info_tick.ask
        point = mt5.symbol_info(signal.symbol).point

        trade_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": signal.symbol,
            "volume": float(signal.volume),
            "type": mt5.ORDER_TYPE_BUY,
            "price": ask,
            "sl": ask - stop_loss_pips * point,
            "tp": ask + take_profit_pips * point,
            "deviation": self.deviation,
            "magic": self.magic_number,
            "comment": signal.open_position,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        trade_result = mt5.order_send(trade_request)
        logging.debug(f"Trade result: {trade_result}")
        if trade_result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error("Trade failed, retcode={}".format(trade_result.retcode))
            logging.error(mt5.last_error())
            raise Exception("Trade failed")
        else:
            logging.info(f"Enter long {signal.open_position} processed successfully!")
        return trade_result

    @retry(retry=retry_if_result(is_requote_error), stop=stop_after_attempt(5), wait=wait_fixed(1),
           reraise=True, after=after_log(logger, logging.DEBUG))
    def enter_short(self, signal: TradingSignal):
        logging.debug(f"Entering short with signal: {signal}")
        symbol_info_tick = mt5.symbol_info_tick(signal.symbol)
        bid = symbol_info_tick.bid
        point = mt5.symbol_info(signal.symbol).point

        trade_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": signal.symbol,
            "volume": float(signal.volume),
            "type": mt5.ORDER_TYPE_SELL,
            "price": bid,
            "sl": bid + stop_loss_pips * point,
            "tp": bid - take_profit_pips * point,
            "deviation": self.deviation,
            "magic": self.magic_number,
            "comment": signal.open_position,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        trade_result = mt5.order_send(trade_request)
        logging.debug(f"Trade result: {trade_result}")
        if trade_result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error("Trade failed, retcode={}".format(trade_result.retcode))
            logging.error(mt5.last_error())
            raise Exception("Trade failed")
        else:
            logging.info(f"Enter short {signal.open_position} processed successfully!")
        return trade_result

    def close_all_positions(self):
        positions = mt5.positions_get()
        if positions is None:
            logging.error("Error: Unable to retrieve positions")
            return
        for position in positions:
            self.close_position_by_comment(position.comment)

    @retry(retry=retry_if_result(is_requote_error), stop=stop_after_attempt(5), wait=wait_fixed(1),
           reraise=True, after=after_log(logger, logging.DEBUG))
    def close_position_by_comment(self, comment):
        # Get all positions
        positions = mt5.positions_get()

        if positions is None:
            logging.error("Error: Unable to retrieve positions")
            return

        # Find the position with the specified comment
        position_info = None
        for position in positions:
            if position.comment == comment:
                position_info = position
                break

        if position_info is None:
            logging.error(f"Error: Position with comment {comment} does not exist")
            return

        # Look for the position with the specified comment
        for position in positions:
            if position.comment == comment:
                # Update the symbol's tick info

                # Get the current bid and ask prices
                symbol_info_tick = mt5.symbol_info_tick(position.symbol)
                bid = symbol_info_tick.bid
                ask = symbol_info_tick.ask
                point = mt5.symbol_info(position.symbol).point
                if position.type == mt5.POSITION_TYPE_BUY:
                    price = bid
                    sl = price - 100 * point
                    tp = price + 100 * point
                else:
                    price = ask
                    sl = price + 100 * point
                    tp = price - 100 * point

                trade_request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": position.symbol,
                    "volume": position.volume,
                    "type": mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                    "price": bid if position.type == mt5.POSITION_TYPE_BUY else ask,
                    "deviation": self.deviation + 10,  # Increase the allowed deviation
                    "magic": self.magic_number,
                    "comment": f"closed {position.ticket}",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_FOK,
                    "position": position.ticket,
                }

                # Execute the trade request
                trade_result = mt5.order_send(trade_request)
                logging.debug(f"Trade result: {trade_result}")
                if trade_result is None:
                    logging.error("Error: Order send result is None")
                    return
                if trade_result.retcode != mt5.TRADE_RETCODE_DONE:
                    logging.error("Trade failed, retcode={}".format(trade_result.retcode))
                    logging.error(mt5.last_error())
                    raise Exception("Trade failed")
                else:
                    logging.info(f"Close {comment} processed successfully!")
                return trade_result

    def is_trading_time(self):
        # 获取当前服务器时间（这将根据你的服务器设置返回不同的时间）
        server_time = datetime.now()
        tradetime = 1
        # 将服务器时间转换为北京时间
        beijing_time = server_time.astimezone(pytz.timezone('Asia/Shanghai'))

        # 获取当前小时和星期几
        hour = beijing_time.hour
        day_of_week = beijing_time.weekday()

        # 判断是否在交易时间内
        if day_of_week >= 5 or (hour >= 6 and hour < 7):
            tradetime = 0
            print("非交易时间")
        else:
            tradetime = 1
            print("正常交易中")

        return tradetime

async def webhook_handler(request,trading_bot):
    trading_bot = trading_bot
    try:
        signal = TradingSignal.from_webhook(request.body.decode())
        signal = signal.normalize_action()
        task = asyncio.create_task(trading_bot.execute_order(signal))
        task.add_done_callback(lambda task: task_callback(task, trading_bot))
        return sanic_json({'message': 'Signal received'}, status=200)
    except ValueError as e:
        return sanic_json({'message': str(e)}, status=400)


# @app.listener('before_server_start')
# async def init(app, loop):
#     if not mt5.initialize():
#         logger.error("Failed to connect to MetaTrader 5: %s", mt5.last_error())
#         sys.exit(1)

#     authorized = mt5.login(login=mt5_login, password=mt5_password, server=mt5_server)
#     if not authorized:
#         logging.error(f"Failed to connect to trade account with login = {login}")
#         logging.error(f"Error code = , {mt5.last_error()}")
#         quit()

#     logging.info(f"Connected to trade account with login = {login}")


@app.listener('before_server_stop')
async def close(app, loop):
    mt5.shutdown()
    logger.info("Disconnected from MetaTrader 5")


def task_callback(task):
    try:
        task.result()
    except Exception as e:
        logger.error(f"An error occurred during task execution: {e}")


    
def main():
    config_file = 'config.toml'
    config = Config(config_file)
    mt5_login, mt5_password, mt5_server, trading_stop_loss_pips, trading_take_profit_pips, dynamic_sltp, magic_number, deviation = config.get_parameters()

    trading_bot = TradingBot(mt5_login, mt5_password, mt5_server, dynamic_sltp, magic_number, deviation)
    app.route('/webhook', methods=['POST'])(lambda request: webhook_handler(request, trading_bot))
    # Rest of the main function
    app.run(host="0.0.0.0", port=8099, debug=True)

if __name__ == "__main__":
    main()
    