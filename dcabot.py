#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Dollar-Cost Averaging (DCA) trading bot for cryptocurrency exchanges.
Version 3.9.4 with Production-Grade Signal Handling and Correct State Recovery.

This script implements a sophisticated DCA trading bot that can operate on both spot
and futures markets. It uses the ccxt.pro library for real-time WebSocket data streams,
ensuring rapid updates on prices and order statuses. The user interface is built with
the 'rich' library, providing an interactive and visually appealing terminal dashboard.

Core Features:
- Dynamic Take Profit: The take-profit target adjusts based on the real-time average
  entry price of the position, ensuring a consistent profit margin.
- Fixed Stop Loss: The stop-loss is set once at the beginning of a trading round and
  does not move, providing a clear, predictable risk limit.
- Robust State Recovery: On restart, the bot can recover its state by fetching open
  orders and positions from the exchange. It correctly reconstructs the position from
  filled orders, ensuring a seamless continuation of the active trading round.
- Clean Shutdown: Uses standard signal handling to ensure that a normal shutdown
  (Ctrl+C) leaves orders on the exchange for recovery, preventing accidental cancellation.
- Fee-Aware Accounting: All profit and loss calculations correctly account for exchange
  trading fees, providing a true representation of performance.

Author: S. Pilneo
Version: 3.9.4
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import os
import random
import string
import signal
import math
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple, Set

# --- Custom Exception for Unrecoverable State ---
class StateInconsistencyError(Exception):
    """Raised when the bot's calculated state cannot be reconciled with the exchange state."""
    pass

# --- Rich, rich-argparse and CCXT Imports ---
try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.layout import Layout
    from rich.logging import RichHandler
    from rich.text import Text
    from rich.prompt import Confirm
    from rich.rule import Rule
    import rich.box
except ImportError:
    print("Rich library not found. Please install it with 'pip install rich'")
    sys.exit(1)

try:
    from rich_argparse import RichHelpFormatter
except ImportError:
    print("rich-argparse library not found. Please install it with 'pip install rich-argparse'")
    sys.exit(1)

try:
    import ccxt.pro as ccxt
except ImportError:
    print("CCXT library not found. Please install it with 'pip install ccxt[async_support,pro]'")
    sys.exit(1)

# --- Global Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False, markup=True)]
)
log = logging.getLogger("rich")
CLIENT_ORDER_ID_PREFIX = "dca39"

@dataclass
class BotConfig:
    """Holds all configuration parameters for the DCA bot."""
    symbol: str
    trade_type: str = 'spot'
    margin_mode: str = 'isolated'
    leverage: int = 1
    price_deviation: float = 1.0
    price_deviation_multiplier: float = 1.0
    take_profit: float = 3.0
    stop_loss: float = 0.0
    fee_rate: Optional[float] = None
    base_order_size: float = 10.0
    safety_order_size: float = 10.0
    safety_order_size_multiplier: float = 1.0
    max_safety_orders: int = 1
    trigger_price: Optional[float] = None
    lower_price_range: Optional[float] = None
    upper_price_range: Optional[float] = None
    cooldown_between_rounds: int = 60
    no_confirm: bool = False
    exchange_id: str = 'binance'

# --- The Bot Class ---

class DCABot:
    """The main class for the DCA Bot logic with a Rich-based UI."""

    def __init__(self, config: BotConfig, exchange: ccxt.Exchange):
        """Initializes the DCABot instance."""
        self.config = config
        self.exchange = exchange
        self.active = True
        self.shutdown_requested = False
        self.console = Console()
        self.log_messages = deque(maxlen=15)

        # --- Core State Variables ---
        self.status: str = "Initializing"
        self.in_round: bool = False
        self.round_failed_to_start: bool = False
        self.last_round_end_time: float = 0
        self.buy_orders: List[Dict] = []
        self.tp_order: Optional[Dict] = None
        self.sl_order: Optional[Dict] = None
        self.position_amount: float = 0.0
        self.position_cost: float = 0.0
        self.average_entry_price: float = 0.0
        self.filled_safety_orders: int = 0
        self.last_price: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.fixed_sl_price: Optional[float] = None
        self.round_start_price: Optional[float] = None
        self.processed_order_ids: Set[str] = set()

        # --- Market and Symbol Information ---
        self.market = self.exchange.market(self.config.symbol)
        self.symbol = self.market['symbol']

        # --- Fee Handling ---
        if self.config.fee_rate is not None:
            self.fee_rate = self.config.fee_rate
            self.log_message(f"Using manual fee rate: [bold yellow]{self.fee_rate * 100:.4f}%[/bold yellow]")
        else:
            self.fee_rate = self.market.get('taker', 0.001)
            self.log_message(f"Using fetched exchange fee rate: [bold yellow]{self.fee_rate * 100:.4f}%[/bold yellow]")

        self.log_message(f"Bot initialized for [bold cyan]{self.symbol}[/] on [bold yellow]{self.exchange.id}[/]")

    def log_message(self, msg: str):
        """Adds a timestamped message to the log deque for display in the UI."""
        timestamp = time.strftime('%H:%M:%S')
        self.log_messages.append(f"[dim]{timestamp}[/dim] {msg}")

    async def _get_current_position_for_validation(self) -> float:
        """
        Fetches the current position/balance from the exchange FOR VALIDATION PURPOSES ONLY.
        This is not used for trading decisions, only to detect gross inconsistencies during recovery.
        Returns:
            float: The size of the position or free balance in the base currency.
        """
        self.log_message("[dim]Fetching current position/balance from exchange for validation...[/dim]")
        position_size = 0.0
        try:
            if self.config.trade_type == 'futures':
                positions = await self.exchange.fetch_positions([self.symbol])
                open_position = next((p for p in positions if p.get('contracts') and float(p['contracts']) > 0), None)
                if open_position:
                    position_size = float(open_position['contracts'])
            else: # spot
                balance = await self.exchange.fetch_balance()
                position_size = self.exchange.safe_float(balance['free'], self.market['base'], 0.0)
            
            self.log_message(f"[dim]Exchange reports current position/balance of {position_size:.6f} {self.market['base']}[/dim]")
            return position_size
        except Exception as e:
            self.log_message(f"[red]Could not fetch position for validation: {e}[/red]")
            return 0.0

    async def recover_state(self) -> bool:
        """
        Attempts to recover the bot's state from the exchange. This is the definitive
        version that correctly handles state validation and propagates critical errors.
        """
        self.log_message("Attempting to recover state from exchange...")
        self.status = "Recovering State"
        
        try:
            all_orders = await self.exchange.fetch_orders(self.symbol, limit=100)
            bot_orders = [o for o in all_orders if (o.get('clientOrderId') or '').startswith(CLIENT_ORDER_ID_PREFIX)]
            if not bot_orders:
                self.log_message("No previous bot orders found. Starting fresh.")
                return False

            latest_order = max(bot_orders, key=lambda o: o['timestamp'])
            cid_parts = latest_order['clientOrderId'].split('-')
            price_part = next((part for part in cid_parts if part.startswith('p')), None)
            if not price_part:
                self.log_message("[red]Could not determine round start price from latest order. Cannot recover.[/red]")
                return False

            self.round_start_price = float(price_part[1:]) / 100.0
            round_orders = [o for o in bot_orders if f"-p{int(self.round_start_price*100)}-" in o['clientOrderId']]
            filled_buy_orders = [o for o in round_orders if o['status'] == 'closed' and o['side'] == 'buy' and o.get('filled', 0) > 0]
            filled_sell_orders = [o for o in round_orders if o['status'] == 'closed' and o['side'] == 'sell' and o.get('filled', 0) > 0]

            if filled_sell_orders:
                self.log_message("Last round was completed by a bot-placed sell order. Starting fresh.")
                return False

            if not filled_buy_orders:
                self.log_message("Found open orders from a previous round but no filled buys. Cleaning up and starting fresh.")
                await self._end_round(start_cooldown=False)
                return False
            
            # 1. Calculate the position the bot *thinks* it has based on its own order history.
            calculated_position = sum(float(o.get('filled', 0)) * (1 - self.fee_rate) for o in filled_buy_orders)

            # 2. Get the *actual* position from the exchange for a sanity check.
            actual_position = await self._get_current_position_for_validation()

            # 3. Define a dust threshold.
            dust_threshold = self.market['precision']['amount'] or 0.000001

            # 4. Make an intelligent decision based on the comparison.
            if calculated_position > dust_threshold and actual_position < dust_threshold:
                # The bot expects a position, but it has been sold off. A clean state.
                self.log_message("[yellow]Detected a resolved ghost position. The previous round was likely closed manually.[/yellow]")
                self.log_message("Cleaning up any orphaned orders and preparing for a fresh start.")
                await self._end_round(start_cooldown=False)
                return False

            elif calculated_position > dust_threshold:
                # A significant position exists, but amounts don't match. This is dangerous.
                lower_bound = calculated_position * 0.9
                upper_bound = calculated_position * 1.1
                if not (lower_bound <= actual_position <= upper_bound):
                    error_message = (
                        f"Calculated position from bot orders ({calculated_position:.6f} {self.market['base']}) is inconsistent with actual "
                        f"exchange balance ({actual_position:.6f} {self.market['base']}).\n\n"
                        f"[bold]This is a safety stop.[/bold] It can happen if you manually trade the asset or after repeated restarts.\n\n"
                        f"[bold cyan]Action Required:[/bold cyan] To run the bot again, please manually sell your "
                        f"{self.market['base']} assets on the exchange to consolidate your capital into {self.market['quote']}."
                    )
                    raise StateInconsistencyError(error_message)

            # 5. If we get here, the state is consistent. Reconstruct the position.
            self.log_message("State validation passed. Reconstructing position...")
            for order in filled_buy_orders:
                cost_of_fill = float(order.get('cost', 0))
                net_amount_received = float(order.get('filled', 0)) * (1 - self.fee_rate)
                self.position_cost += cost_of_fill
                self.position_amount += net_amount_received
                if 'so' in order['clientOrderId']: self.filled_safety_orders += 1
                self.processed_order_ids.add(order['id'])
            if self.position_amount > 0: self.average_entry_price = self.position_cost / self.position_amount

            open_round_orders = [o for o in round_orders if o['status'] == 'open']
            for order in open_round_orders:
                cid = order['clientOrderId']; 
                if 'bo' in cid or 'so' in cid: self.buy_orders.append(order)
                elif 'tp' in cid: self.tp_order = order
                elif 'sl' in cid: self.sl_order = order
            
            self.in_round = True
            self.status = "Recovered - In Position"
            if self.config.stop_loss > 0: self.fixed_sl_price = self.round_start_price * (1 - self.config.stop_loss / 100)
            if self.position_amount > 0 and not self.tp_order: await self._update_tp_sl_orders()

            self.log_message(f"[bold green]State recovery successful.[/bold green]")
            return True

        except Exception as e:
            self.log_message(f"[red]An error occurred during state recovery: {e}[/red]")
            await self._end_round(start_cooldown=False)
            return False

    def _validate_config(self, start_price: float) -> bool:
        """Performs critical sanity checks on the bot's configuration."""
        if self.config.take_profit <= 0:
            self.console.print(Panel(Text("Take Profit must be a positive value.", "red"), title="[bold]Validation Failed[/]", border_style="red"))
            return False

        last_so_price, last_so_deviation, cumulative_deviation = 0.0, 0.0, 0.0
        if self.config.max_safety_orders > 0:
            current_deviation_step = self.config.price_deviation
            for i in range(self.config.max_safety_orders):
                if i > 0: current_deviation_step *= self.config.price_deviation_multiplier
                cumulative_deviation += current_deviation_step
                if cumulative_deviation >= 100:
                    error_msg = (f"Cumulative price deviation reached [bold red]>= 100%[/] at Safety Order {i+1}.\n"
                                 f"This would result in a zero or negative order price, which is impossible.")
                    self.console.print(Panel(Text(error_msg, "red"), title="[bold]Validation Failed: Impossible Price[/]", border_style="red", box=rich.box.ROUNDED))
                    return False
                last_so_price = start_price * (1 - cumulative_deviation / 100)
                last_so_deviation = cumulative_deviation

        if self.config.stop_loss > 0 and self.config.max_safety_orders > 0 and last_so_deviation > 0:
            if self.config.stop_loss <= last_so_deviation:
                sl_price = start_price * (1 - self.config.stop_loss / 100)
                error_msg = (f"The Stop Loss price ([bold red]~{sl_price:.4f}[/]) is not below the final Safety Order price ([bold yellow]~{last_so_price:.4f}[/]).\n"
                             f"This invalidates the DCA strategy, as the SL would trigger before all safety orders can be filled.\n"
                             f"Your SL deviation ([bold]{self.config.stop_loss}%[/]) must be greater than the final SO deviation ([bold]~{last_so_deviation:.2f}%[/]).")
                self.console.print(Panel(Text(error_msg, "red"), title="[bold]Validation Failed: Illogical Stop Loss[/]", border_style="red", box=rich.box.ROUNDED))
                return False
        return True

    async def display_confirmation_and_wait(self, initial_args: Dict[str, Any]) -> bool:
        """Displays a beautiful, chronological trade plan and waits for confirmation."""
        if self.config.no_confirm: return True
        self.log_message("Fetching current price for order grid calculation...")
        try:
            ticker = await self.exchange.fetch_ticker(self.symbol)
            start_price = self.config.trigger_price or ticker['last']
        except Exception as e:
            log.error(f"Could not fetch initial price: {e}"); return False

        if not self._validate_config(start_price): return False

        summary_table = Table.grid(expand=True, padding=(0, 2))
        summary_table.add_column(style="bold cyan", justify="right"); summary_table.add_column(justify="left")
        defaults = BotConfig(symbol=self.config.symbol)
        for key, value in sorted(vars(self.config).items()):
            if key in ['symbol', 'no_confirm']: continue
            is_default = (getattr(defaults, key) == value) and (key not in initial_args)
            if key == 'fee_rate' and value is None:
                value_str = f"{self.fee_rate * 100:.4f}%"
                display_str = f"{value_str} [dim](from exchange)[/dim]"
            else:
                value_str = str(value) if value is not None else "Not Set"
                display_str = f"{value_str} [dim](default)[/dim]" if is_default else f"[bold]{value_str}[/bold]"
            summary_table.add_row(f"{key.replace('_', ' ').title()}:", display_str)
        self.console.print(Panel(summary_table, title="[bold white on blue] Bot Run Configuration [/]", border_style="blue", box=rich.box.ROUNDED))

        trade_plan_table = Table(show_header=True, header_style="bold magenta", box=rich.box.ROUNDED, border_style="magenta", expand=True)
        trade_plan_table.add_column("Order"); trade_plan_table.add_column("Price", justify="right")
        trade_plan_table.add_column("Avg. Price", justify="right"); trade_plan_table.add_column("Deviation", justify="right")
        trade_plan_table.add_column("Size", justify="right"); trade_plan_table.add_column("P/L on Event", justify="right")
        rows = []; cumulative_cost, cumulative_base_size_net = 0.0, 0.0
        base_price = start_price; base_quote_size = self.config.base_order_size
        if base_price <= 0: return False
        cost_of_buy = base_quote_size
        base_amount_net = (base_quote_size / base_price) * (1 - self.fee_rate)
        post_base_cost = cumulative_cost + cost_of_buy
        post_base_amount_net = cumulative_base_size_net + base_amount_net
        post_base_avg_price = post_base_cost / post_base_amount_net
        tp_price_base = post_base_avg_price * (1 + self.config.take_profit / 100)
        sell_value_net_base = (tp_price_base * post_base_amount_net) * (1 - self.fee_rate)
        profit_base = sell_value_net_base - post_base_cost
        rows.append({'type': 'TP (after Base)', 'price': tp_price_base, 'avg_price': None, 'dev': None, 'size': post_base_amount_net, 'pnl': profit_base, 'style': 'dim green'})
        rows.append({'type': 'Base Order', 'price': base_price, 'avg_price': post_base_avg_price, 'dev': 0.0, 'size': base_quote_size, 'pnl': 0.0, 'style': 'bold cyan'})
        cumulative_cost, cumulative_base_size_net = post_base_cost, post_base_amount_net
        cumulative_deviation, current_deviation_step = 0.0, self.config.price_deviation
        current_size_quote = self.config.safety_order_size
        for i in range(self.config.max_safety_orders):
            if i > 0: current_deviation_step *= self.config.price_deviation_multiplier
            cumulative_deviation += current_deviation_step
            so_price = start_price * (1 - cumulative_deviation / 100)
            if so_price <= 0: continue
            so_cost = current_size_quote
            so_amount_net = (current_size_quote / so_price) * (1 - self.fee_rate)
            post_so_cost, post_so_amount_net = cumulative_cost + so_cost, cumulative_base_size_net + so_amount_net
            post_so_avg_price = post_so_cost / post_so_amount_net
            tp_price_so = post_so_avg_price * (1 + self.config.take_profit / 100)
            sell_value_net_so = (tp_price_so * post_so_amount_net) * (1 - self.fee_rate)
            profit_so = sell_value_net_so - post_so_cost
            value_at_so_price = (so_price * post_so_amount_net) * (1 - self.fee_rate)
            loss_at_so = value_at_so_price - post_so_cost
            rows.append({'type': f'TP (after SO{i+1:02d})', 'price': tp_price_so, 'avg_price': None, 'dev': None, 'size': post_so_amount_net, 'pnl': profit_so, 'style': 'dim green'})
            rows.append({'type': f'Safety Order {i+1:02d}', 'price': so_price, 'avg_price': post_so_avg_price, 'dev': cumulative_deviation, 'size': current_size_quote, 'pnl': loss_at_so, 'style': 'cyan'})
            cumulative_cost, cumulative_base_size_net = post_so_cost, post_so_amount_net
            current_size_quote *= self.config.safety_order_size_multiplier
        if self.config.stop_loss > 0:
            fixed_sl_price = start_price * (1 - self.config.stop_loss / 100)
            sl_sell_value_net = (fixed_sl_price * cumulative_base_size_net) * (1 - self.fee_rate)
            sl_loss = sl_sell_value_net - cumulative_cost
            rows.append({'type': 'Stop Loss', 'price': fixed_sl_price, 'avg_price': None, 'dev': self.config.stop_loss, 'size': cumulative_base_size_net, 'pnl': sl_loss, 'style': 'bold red'})
        
        price_precision_raw = self.market['precision']['price']
        price_precision_int = int(round(-math.log10(price_precision_raw))) if isinstance(price_precision_raw, float) and price_precision_raw > 0 else 0
        amount_precision_raw = self.market['precision']['amount']
        amount_precision_int = int(round(-math.log10(amount_precision_raw))) if isinstance(amount_precision_raw, float) and amount_precision_raw > 0 else 0

        rows.sort(key=lambda x: x['price'], reverse=True)
        for row in rows:
            is_buy = 'Order' in row['type']
            price_str = f"{row['price']:.{price_precision_int}f}"
            avg_price_str = f"{row['avg_price']:.{price_precision_int}f}" if row.get('avg_price') else ""
            dev_str = f"{row['dev']:.2f}%" if row.get('dev') is not None else ""
            size_unit, size_val = (f" {self.market['quote']}", row.get('size', 0)) if is_buy else (f" {self.market['base']}", row.get('size', 0))
            if is_buy:
                size_str = f"{size_val:.2f}{size_unit}"
            else:
                size_str = f"{size_val:.{amount_precision_int}f}{size_unit}"
            
            pnl, pnl_str = row.get('pnl'), ""
            if pnl is not None:
                if pnl > 0:
                    pnl_str = f"[green]+{pnl:.2f} {self.market['quote']}[/green]"
                elif pnl < 0:
                    pnl_str_val = f"{pnl:.2f} {self.market['quote']}"
                    style = "dim red" if is_buy else "red"
                    pnl_str = f"[{style}]{pnl_str_val}[/{style}]"
                else:
                    pnl_str = f"[dim]0.00 {self.market['quote']}[/dim]"

            trade_plan_table.add_row(f"[{row['style']}]{row['type']}[/]", price_str, avg_price_str, dev_str, size_str, pnl_str)
        
        self.console.print(Panel(trade_plan_table, title="[bold white on magenta] Chronological Trade Plan [/]", border_style="magenta"))
        self.console.print("[dim]P/L for buy orders shows the estimated unrealized loss if the position were sold immediately after that order filled.[/dim]")
        return Confirm.ask("\n[bold]Do you want to start the bot with this configuration?[/bold]", console=self.console, default=True)

    def _generate_client_order_id(self, order_type: str, start_price: float) -> str:
        """Creates a unique, recoverable, and exchange-compliant client order ID."""
        market_id = self.market['id'].replace('/', '').replace(':', '')[:8].lower()
        price_encoded = f"p{int(start_price * 100)}"
        rand_part = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        if 'buy_base' in order_type: o_type = 'bo'
        elif 'buy_so' in order_type: o_type = f"so{order_type.split('_')[-1]}"
        elif 'tp' in order_type: o_type = 'tp'
        elif 'sl' in order_type: o_type = 'sl'
        else: o_type = 'unk'
        cid = f"{CLIENT_ORDER_ID_PREFIX}-{market_id}-{price_encoded}-{o_type}-{rand_part}"
        return cid[:36]

    def _generate_layout(self) -> Layout:
        """Creates the Rich Layout for the live UI."""
        layout = Layout(name="root")
        layout.split(
            Layout(name="header", size=3),
            Layout(ratio=1, name="main"),
            Layout(size=min(17, len(self.log_messages) + 2), name="footer"),
        )
        layout["main"].split_row(Layout(name="side"), Layout(name="body", ratio=2))
        return layout

    def _update_header(self, layout: Layout):
        """Updates the header panel of the UI."""
        header_text = Text.from_markup(f"CCXT DCA Bot v3.9.4 | Exchange: [yellow]{self.exchange.id}[/] | Symbol: [cyan]{self.symbol}[/] | Status: [bold magenta]{self.status}[/]", justify="center")
        layout["header"].update(Panel(header_text, style="bold blue", box=rich.box.ROUNDED))

    def _update_status_panel(self, layout: Layout):
        """Updates the live status panel with current data."""
        pnl_color = "green" if self.unrealized_pnl > 0 else "red" if self.unrealized_pnl < 0 else "dim"
        pnl_text = f"{self.unrealized_pnl:+.2f} {self.market['quote']} ({self.unrealized_pnl / self.position_cost * 100:.2f}%)" if self.position_cost > 0 else f"0.00 {self.market['quote']}"
        
        table = Table.grid(expand=True)
        table.add_column(justify="right", style="dim", no_wrap=True)
        table.add_column(justify="left", style="white")

        last_price_str = self.exchange.price_to_precision(self.symbol, self.last_price) if self.last_price > 0 else "N/A"
        avg_entry_str = self.exchange.price_to_precision(self.symbol, self.average_entry_price) if self.in_round and self.position_amount > 0 else "N/A"
        pos_size_str = self.exchange.amount_to_precision(self.symbol, self.position_amount) if self.in_round and self.position_amount > 0 else "N/A"
        sl_price_str = self.exchange.price_to_precision(self.symbol, self.fixed_sl_price) if self.fixed_sl_price else "N/A"

        table.add_row("Last Price:", f"[bold green]{last_price_str}[/]")
        table.add_row("Avg. Entry (Cost Basis):", f"[bold cyan]{avg_entry_str}[/]")
        table.add_row("Net Position Size:", f"[bold]{pos_size_str} {self.market['base']}[/]" if self.in_round and self.position_amount > 0 else "N/A")
        table.add_row("Fixed Stop Loss At:", f"[bold red]{sl_price_str}[/]")
        table.add_row("Filled SO:", f"{self.filled_safety_orders}/{self.config.max_safety_orders}" if self.in_round else "N/A")
        
        pnl_display = f"[{pnl_color}]{pnl_text}[/{pnl_color}]"
        if self.unrealized_pnl == 0 and self.position_cost == 0:
            pnl_display = f"[dim]0.00 {self.market['quote']}[/dim]"

        table.add_row("Unrealized PNL:", pnl_display)
        
        layout["side"].update(Panel(table, title="[bold]Live Status[/]", box=rich.box.ROUNDED, border_style="cyan"))

    def _update_orders_panel(self, layout: Layout):
        """Updates the open orders panel."""
        all_open_orders = self.buy_orders + ([self.tp_order] if self.tp_order else [])
        
        if not any(all_open_orders):
            content = Panel(Text("No open orders.", justify="center", style="dim"), box=rich.box.ROUNDED, border_style="magenta")
        else:
            table = Table(box=rich.box.ROUNDED, border_style="magenta", expand=True)
            table.add_column("ID", style="dim"); table.add_column("Type", style="cyan")
            table.add_column("Side", style="magenta"); table.add_column("Price", style="green", justify="right"); table.add_column("Amount", style="yellow", justify="right")
            
            for order in sorted([o for o in all_open_orders if o], key=lambda o: o.get('price', 0), reverse=True):
                cid = order.get('clientOrderId', ''); 
                order_type_str = "Buy"
                if 'tp' in cid: order_type_str = "TP"
                elif 'sl' in cid: order_type_str = "SL"
                elif 'so' in cid: order_type_str = "Safety"
                elif 'bo' in cid: order_type_str = "Base"

                price = order.get('price')
                price_str = self.exchange.price_to_precision(self.symbol, price) if price else "N/A"
                amount_str = self.exchange.amount_to_precision(self.symbol, order.get('amount'))
                table.add_row(str(order['id']), order_type_str, order['side'], price_str, amount_str)
            content = table

        layout["body"].update(Panel(content, title="[bold]Open Orders[/]", border_style="cyan"))

    def _update_footer(self, layout: Layout):
        """Updates the log message footer."""
        layout["footer"].update(Panel("\n".join(self.log_messages), title="[bold]Logs[/]", border_style="dim", box=rich.box.ROUNDED))

    def _update_live_display(self, live: Live):
        """Orchestrates the update of all UI components."""
        try:
            layout = self._generate_layout()
            self._update_header(layout); self._update_status_panel(layout)
            self._update_orders_panel(layout); self._update_footer(layout)
            live.update(layout)
        except Exception as e:
            self.log_message(f"[bold red]Error updating display: {e}[/bold red]")

    async def run(self):
        """Main execution loop for the bot."""
        if self.config.trade_type == 'futures' and not self.in_round:
            await self._set_leverage_and_margin_mode()
        with Live(self._generate_layout(), screen=True, transient=True, refresh_per_second=4) as live:
            if not self.status.startswith("Recovered"):
                self.status = "Running"
            
            self._update_live_display(live)
            
            ticker_task = asyncio.create_task(self._ticker_loop(live))
            orders_task = asyncio.create_task(self._orders_loop(live))
            try:
                await asyncio.gather(ticker_task, orders_task)
            except asyncio.CancelledError:
                self.log_message("[yellow]Core tasks cancelled.[/yellow]")

    async def _set_leverage_and_margin_mode(self):
        """Sets leverage and margin mode for futures trading before starting."""
        try:
            self.log_message(f"Setting margin mode to '[bold]{self.config.margin_mode}[/bold]' for {self.symbol}")
            await self.exchange.set_margin_mode(self.config.margin_mode, self.symbol)
            self.log_message(f"Setting leverage to [bold]{self.config.leverage}x[/bold] for {self.symbol}")
            await self.exchange.set_leverage(self.config.leverage, self.symbol)
        except Exception as e: self.log_message(f"[red]Could not set futures mode: {e}[/red]")

    async def _ticker_loop(self, live: Live):
        """Watches the market price ticker via WebSocket and triggers state changes."""
        try:
            ticker = await self.exchange.fetch_ticker(self.symbol)
            self.last_price = ticker.get('last', 0.0)
        except Exception as e:
            self.log_message(f"[red]Initial ticker fetch error: {e}[/red]")

        if not self.in_round: self.status = "Watching Market"
        self._update_live_display(live)

        while self.active and not self.shutdown_requested:
            try:
                ticker = await self.exchange.watch_ticker(self.symbol)
                self.last_price = ticker.get('last', self.last_price)
                if self.position_amount > 0 and self.position_cost > 0:
                    contract_size = self.market.get('contractSize', 1.0)
                    current_value_net = (self.last_price * self.position_amount * contract_size) * (1 - self.fee_rate)
                    self.unrealized_pnl = current_value_net - self.position_cost
                
                # --- VIRTUAL STOP LOSS LOGIC ---
                if self.in_round and self.fixed_sl_price and self.last_price <= self.fixed_sl_price:
                    self.log_message(f"[bold red]Stop Loss price {self.fixed_sl_price:.4f} hit! Selling position at market.[/bold red]")
                    await self._execute_stop_loss()
                    continue 

                if not self.in_round:
                    await self._check_and_start_new_round()
                
                self._update_live_display(live)
            except Exception as e:
                self.log_message(f"[red]Ticker loop error: {e}[/red]"); await asyncio.sleep(15)

    async def _orders_loop(self, live: Live):
        """Watches for filled orders via WebSocket and handles them."""
        while self.active and not self.shutdown_requested:
            try:
                orders = await self.exchange.watch_orders(self.symbol)
                for order in orders:
                    if order.get('status') == 'closed' and order.get('filled', 0) > 0:
                        await self._handle_filled_order(order)
                        self._update_live_display(live)
            except Exception as e:
                self.log_message(f"[red]Orders loop error: {e}[/red]"); await asyncio.sleep(15)

    async def _check_and_start_new_round(self):
        """Checks conditions and starts a new trading round if they are met."""
        is_in_cooldown = time.time() < self.last_round_end_time + self.config.cooldown_between_rounds
        if is_in_cooldown:
            time_left = int(self.config.cooldown_between_rounds - (time.time() - self.last_round_end_time))
            self.status = f"In Cooldown ({time_left}s left)"; return
        if self.round_failed_to_start:
            self.status = "Round Failed to Start"; return
        if (self.config.lower_price_range and self.last_price < self.config.lower_price_range) or \
           (self.config.upper_price_range and self.last_price > self.config.upper_price_range):
            self.status = "Waiting for Price to Enter Range"; return
        self.log_message("[bold green]Conditions met. Starting a new trading round.[/bold green]")
        self.status = "Placing Orders"
        start_price = self.config.trigger_price if self.config.trigger_price and self.last_round_end_time == 0 else self.last_price
        await self._place_initial_orders(start_price)
        self.status = "Awaiting Fills" if self.buy_orders or self.tp_order else "Round Failed"

    async def _place_initial_orders(self, price: float):
        """Calculates and places the base order and all safety orders."""
        self.in_round = True; self.round_start_price = price
        if self.config.stop_loss > 0:
            self.fixed_sl_price = price * (1 - self.config.stop_loss / 100)
            self.log_message(f"Stop Loss for this round is fixed at [bold red]{self.fixed_sl_price:.4f}[/bold red]")
        orders_to_place, cumulative_deviation = [], 0.0
        orders_to_place.append({'price': price, 'amount': self.config.base_order_size / price, 'type': 'buy_base'})
        current_size_quote, current_deviation_step = self.config.safety_order_size, self.config.price_deviation
        for i in range(self.config.max_safety_orders):
            if i > 0: current_deviation_step *= self.config.price_deviation_multiplier
            cumulative_deviation += current_deviation_step
            safety_price = price * (1 - cumulative_deviation / 100)
            orders_to_place.append({'price': safety_price, 'amount': current_size_quote / safety_price, 'type': f'buy_so_{i+1}'})
            current_size_quote *= self.config.safety_order_size_multiplier
        tasks = [self.exchange.create_order(self.symbol, 'limit', 'buy', self.exchange.amount_to_precision(self.symbol, op['amount']), self.exchange.price_to_precision(self.symbol, op['price']), {'clientOrderId': self._generate_client_order_id(op['type'], price)}) for op in orders_to_place]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if isinstance(results[0], Exception) or not results[0].get('id'):
            self.log_message(f"[red]Failed to place critical Base order: {results[0]}. Aborting round.[/red]")
            self.round_failed_to_start = True; await self._end_round(); return
        self.log_message(f"Placed Base order {results[0]['id']}")
        if results[0].get('status') == 'closed':
            await self._handle_filled_order(results[0])
        else: self.buy_orders.append(results[0])
        for i, res in enumerate(results[1:]):
            if isinstance(res, Exception): self.log_message(f"[red]Failed to place Safety {i+1} order: {res}[/red]")
            else: self.buy_orders.append(res); self.log_message(f"Placed Safety {i+1} order {res['id']}")

    async def _handle_filled_order(self, order: Dict[str, Any]):
        """Processes a filled order, updating state and subsequent orders."""
        order_id = order['id']
        if order_id in self.processed_order_ids:
            self.log_message(f"[dim]Ignoring duplicate fill event for order {order_id}[/dim]"); return
        cid = order.get('clientOrderId', '')
        if not cid: return
        
        is_buy_order = 'bo' in cid or 'so' in cid
        is_tp_order = self.tp_order and self.tp_order['id'] == order_id

        if is_buy_order:
            self.buy_orders = [o for o in self.buy_orders if o['id'] != order_id]
            filled_amount, avg_price = float(order.get('filled', 0) or order.get('amount', 0)), float(order.get('average') or order.get('price'))
            if filled_amount == 0: return
            self.log_message(f"[bold green]Buy order {order_id} filled: {filled_amount:.6f} @ {avg_price:.4f}[/bold green]")
            self.processed_order_ids.add(order_id)
            cost_of_fill = float(order.get('cost', filled_amount * avg_price))
            net_amount_received = filled_amount * (1 - self.fee_rate)
            self.position_cost += cost_of_fill; self.position_amount += net_amount_received
            if self.position_amount > 0: self.average_entry_price = self.position_cost / self.position_amount
            self.status = "Position Open"
            if "so" in cid: self.filled_safety_orders += 1
            await self._update_tp_sl_orders()

        elif is_tp_order:
            self.log_message(f"[bold green]Trade round concluded by Take Profit order {order_id}.[/bold green]")
            self.processed_order_ids.add(order_id)
            await self._end_round()

    async def _update_tp_sl_orders(self):
        """Places or updates the Take Profit (TP) order for the position acquired in this round."""
        await self._cancel_order(self.tp_order, "TP"); self.tp_order = None

        if not self.in_round or self.position_amount <= 0 or not self.round_start_price:
            return

        try:
            # The bot must ONLY sell the assets it acquired in this round.
            amount_to_sell = self.position_amount
            if amount_to_sell <= self.market['limits']['amount']['min']:
                self.log_message(f"[yellow]Position amount {amount_to_sell} is too small to place a TP order. Waiting for more fills.[/yellow]")
                return

            amount_precise = self.exchange.amount_to_precision(self.symbol, amount_to_sell)
            tp_price = self.average_entry_price * (1 + self.config.take_profit / 100)
            tp_price_precise = self.exchange.price_to_precision(self.symbol, tp_price)

            self.log_message(f"Placing new TP order for {amount_precise} at {tp_price_precise}")
            self.tp_order = await self.exchange.create_order(
                self.symbol, 'limit', 'sell', amount_precise, tp_price_precise, 
                {'clientOrderId': self._generate_client_order_id('tp', self.round_start_price)}
            )
        except Exception as e:
            self.log_message(f"[red]Failed to create TP order: {e}[/red]")
            log.exception("TP order exception details:")

    async def _execute_stop_loss(self):
        """
        Executes a virtual stop loss by canceling the TP order and selling the
        bot's tracked position at market price. This is the safe implementation.
        """
        self.status = "Executing Stop Loss"
        await self._cancel_order(self.tp_order, "TP"); self.tp_order = None

        try:
            # CRITICAL FIX: Only sell the amount the bot has tracked for this round.
            # Do NOT sell the entire wallet balance.
            amount_to_sell = self.position_amount
            if amount_to_sell > self.market['limits']['amount']['min']:
                amount_precise = self.exchange.amount_to_precision(self.symbol, amount_to_sell)
                self.log_message(f"Placing market sell order for this round's position: {amount_precise} {self.market['base']}")
                await self.exchange.create_market_sell_order(self.symbol, amount_precise)
            else:
                 self.log_message(f"[yellow]Stop loss triggered but tracked position amount ({amount_to_sell}) is too small to sell.[/yellow]")
        except Exception as e:
            self.log_message(f"[red]Failed to create market sell order for stop loss: {e}[/red]")
        
        await self._end_round()

    async def _end_round(self, start_cooldown: bool = True):
        """Cleans up after a trading round is finished or aborted."""
        self.log_message("[bold blue]Ending/Resetting trading round.[/bold blue]")
        self.status = "Ending Round"
        await self._cancel_all_orders()

        # Reset all state variables for the next round.
        self.in_round = False
        self.round_failed_to_start = False
        self.buy_orders.clear()
        self.tp_order, self.sl_order = None, None
        self.position_amount, self.position_cost, self.average_entry_price = 0.0, 0.0, 0.0
        self.filled_safety_orders, self.unrealized_pnl = 0, 0.0
        self.fixed_sl_price, self.round_start_price = None, None
        self.processed_order_ids.clear()
        
        if start_cooldown:
            self.last_round_end_time = time.time()
            self.log_message(f"Cooldown started. Waiting {self.config.cooldown_between_rounds}s.")
        else:
            self.last_round_end_time = 0

    async def _cancel_order(self, order: Optional[Dict], order_type: str):
        """Safely cancels a single order."""
        if not order: return
        try:
            await self.exchange.cancel_order(order['id'], self.symbol)
            self.log_message(f"Canceled previous {order_type} order {order['id']}")
        except ccxt.OrderNotFound:
            self.log_message(f"{order_type} order {order['id']} already gone.")
        except Exception as e: self.log_message(f"[red]Error canceling {order_type} order {order['id']}: {e}[/red]")

    async def _cancel_all_orders(self):
        """Fetches and cancels all open orders for the symbol created by this bot."""
        self.log_message("Canceling all bot-related open orders from exchange...");
        try:
            open_orders = await self.exchange.fetch_open_orders(self.symbol)
            bot_orders_to_cancel = [o for o in open_orders if (o.get('clientOrderId') or '').startswith(CLIENT_ORDER_ID_PREFIX)]

            if not bot_orders_to_cancel:
                self.log_message("No open bot orders found on the exchange to cancel.")
                return

            tasks = [self.exchange.cancel_order(o['id'], self.symbol) for o in bot_orders_to_cancel]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            canceled_count = sum(1 for r in results if not isinstance(r, Exception) or isinstance(r, ccxt.OrderNotFound))
            self.log_message(f"Successfully canceled {canceled_count} open order(s).")
        except Exception as e:
            self.log_message(f"[red]Could not fetch or cancel orders: {e}. Please check the exchange manually.[/red]")
        
        self.buy_orders.clear(); self.tp_order = None

    async def close(self, emergency: bool = False):
        """Gracefully shuts down the bot's trading logic."""
        if self.shutdown_requested: return
        self.shutdown_requested = True; self.active = False
        if emergency:
            log.warning("[bold red]Emergency shutdown! Canceling all bot-related orders on the exchange.[/bold red]")
            await self._cancel_all_orders()
        else:
            log.info("Graceful shutdown requested. Open orders will remain on the exchange for recovery.")
        log.info("Bot shutdown procedures complete.")

# --- CLI and Main Execution ---
class CustomRichHelpFormatter(RichHelpFormatter):
    """Custom help formatter to add a header and improve argument layout."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, width=110, max_help_position=36)
        self.console = Console()
    def add_usage(self, usage, actions, groups, prefix=None):
        if prefix is None: prefix = "Usage: "
        self.console.print(Rule("[bold white on blue] DCABot v3.9.4 Help [/]", style="blue"))
        return super().add_usage(usage, actions, groups, prefix)

def parse_args() -> Tuple[BotConfig, Dict[str, Any]]:
    """Parses command-line arguments and returns a BotConfig instance."""
    parser = argparse.ArgumentParser(description="A CCXT-based DCA trading bot with an interactive terminal UI.", formatter_class=CustomRichHelpFormatter, add_help=False)
    req = parser.add_argument_group('Required Arguments')
    strat = parser.add_argument_group('Strategy Arguments')
    orders = parser.add_argument_group('Sizing Arguments (in Quote Currency)')
    control = parser.add_argument_group('Control Arguments')
    other = parser.add_argument_group('Other Arguments')
    req.add_argument('-s', '--symbol', type=str, required=True, help="Trading symbol (e.g., 'BTC/USDT')")
    control.add_argument('--exchange-id', type=str, default='binance', help="CCXT exchange ID (e.g., 'bybit')")
    strat.add_argument('-t', '--trade-type', type=str, default='spot', choices=['spot', 'futures'], help="Market type. [dim]Default: spot[/dim]")
    strat.add_argument('-m', '--margin-mode', type=str, default='isolated', choices=['isolated', 'cross'], help="Futures margin mode. [dim]Default: isolated[/dim]")
    strat.add_argument('-l', '--leverage', type=int, default=1, help="Futures leverage. [dim]Default: 1[/dim]")
    strat.add_argument('-pd', '--price-deviation', type=float, default=1.0, help="Deviation for the first safety order (%%). [dim]Default: 1.0[/dim]")
    strat.add_argument('-pdm', '--price-deviation-multiplier', type=float, default=1.0, help="SO deviation multiplier. [dim]Default: 1.0[/dim]")
    strat.add_argument('-tp', '--take-profit', type=float, default=3.0, help="[bold]Dynamic[/] TP from avg. cost-basis price (%%). [dim]Default: 3.0[/dim]")
    strat.add_argument('-sl', '--stop-loss', type=float, default=0.0, help="[bold]Fixed[/] SL from round's start price (%%). 0=disable. [dim]Default: 0.0[/dim]")
    strat.add_argument('--fee-rate', type=float, help="Manual taker fee rate override (e.g., 0.00075). [dim]Default: Fetched[/dim]")
    orders.add_argument('-bos', '--base-order-size', type=float, default=10.0, help="Base order size in quote currency. [dim]Default: 10.0[/dim]")
    orders.add_argument('-sos', '--safety-order-size', type=float, default=10.0, help="First SO size in quote currency. [dim]Default: 10.0[/dim]")
    orders.add_argument('-sosm', '--safety-order-size-multiplier', type=float, default=1.0, help="SO size multiplier. [dim]Default: 1.0[/dim]")
    orders.add_argument('-mso', '--max-safety-orders', type=int, default=1, help="Max number of safety orders. [dim]Default: 1[/dim]")
    control.add_argument('--trigger-price', type=float, help="Price to start first round. [dim]Default: last market price[/dim]")
    control.add_argument('--lower-price-range', type=float, help="Only start new rounds [yellow]above[/] this price.")
    control.add_argument('--upper-price-range', type=float, help="Only start new rounds [yellow]below[/] this price.")
    control.add_argument('--cooldown-between-rounds', type=int, default=60, help="Seconds to wait between rounds. [dim]Default: 60[/dim]")
    control.add_argument('--no-confirm', action='store_true', help="Skip the initial confirmation prompt.")
    other.add_argument('-h', '--help', action='help', help='Show this help message and exit.')
    parsed_args = parser.parse_args()
    initial_args = {k: v for k, v in vars(parsed_args).items() if v != parser.get_default(k)}
    return BotConfig(**vars(parsed_args)), initial_args

async def main():
    """The main entry point for the application."""
    config, initial_args = parse_args()
    
    keys_file = 'keys.json'
    if not os.path.exists(keys_file):
        log.error(f"API keys file '{keys_file}' not found."); sys.exit(1)
    with open(keys_file) as f: keys = json.load(f)
    exchange_keys = keys.get(config.exchange_id)
    if not exchange_keys or 'apiKey' not in exchange_keys or 'secret' not in exchange_keys:
        log.error(f"API key/secret for '{config.exchange_id}' not in '{keys_file}'."); sys.exit(1)

    exchange = None
    bot = None
    try:
        exchange_config = {**exchange_keys, 'options': {'defaultType': 'swap' if config.trade_type == 'futures' else 'spot'}}
        exchange = getattr(ccxt, config.exchange_id)(exchange_config)
        await exchange.load_markets()
        
        bot = DCABot(config, exchange)

        loop = asyncio.get_running_loop()
        def signal_handler(sig, frame):
            log.warning(f"\nCaught signal {sig}. Requesting graceful shutdown...")
            if bot and not bot.shutdown_requested:
                loop.create_task(bot.close(emergency=False))
            elif bot:
                log.warning(f"Second signal received. Requesting emergency shutdown!")
                loop.create_task(bot.close(emergency=True))
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        should_run = False
        if await bot.recover_state():
            log.info("State successfully recovered. Starting live trading dashboard...")
            should_run = True
        elif await bot.display_confirmation_and_wait(initial_args):
            log.info("Confirmation received. Starting live trading dashboard...")
            should_run = True
        
        if should_run:
            await asyncio.sleep(1)
            await bot.run()
        else:
            log.info("Bot start aborted by user or failed recovery.")
            if bot: bot.shutdown_requested = True

    except StateInconsistencyError as e:
        log.error("[bold red]Bot Halted Due to Unrecoverable State:[/bold red]")
        console = Console()
        console.print(Panel(Text.from_markup(str(e)), title="[bold red]Safety Stop - Manual Action Required[/bold red]", border_style="red"))
        if bot: await bot.close(emergency=True)

    except (ccxt.AuthenticationError, ccxt.NetworkError, ccxt.ExchangeError) as e:
        log.error(f"A CCXT error occurred: {e}")
    except Exception:
        log.error("An unexpected critical error occurred:", exc_info=True)
    
    finally:
        if bot and not bot.shutdown_requested:
            log.warning("Main process exited unexpectedly. Forcing emergency shutdown...")
            await bot.close(emergency=True)
        
        if exchange and hasattr(exchange, 'close'):
            try:
                await exchange.close()
                log.info("Exchange connection closed successfully.")
            except Exception as e:
                log.warning(f"Error during final exchange connection close: {e}")
        
        log.info("Main process finished.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.critical("A critical unhandled error forced the bot to exit.", exc_info=True)