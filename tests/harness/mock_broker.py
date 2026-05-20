"""
Mock Broker for Deterministic Testing
"""
import logging

logger = logging.getLogger("mock_broker")

class MockFutuBroker:
    def __init__(self):
        self.connected = True
        self.orders = []  # List of order dicts
        self.positions = {} # symbol -> {qty, cost}
        self.submitted_order_count = 0

    def submit_order(self, order) -> dict:
        if not self.connected:
            raise ConnectionError("Broker disconnected")
        
        self.submitted_order_count += 1
        order_id = f"mock_{self.submitted_order_count}"
        
        # Mock Immediate Fill
        order_status = {
            "order_id": order_id,
            "symbol": order.symbol,
            "status": "FILLED",
            "price": order.price,
            "qty": order.quantity
        }
        self.orders.append(order_status)
        
        # Update Position Logic
        qty = order.quantity if order.direction.value == "LONG" else -order.quantity
        self._update_position(order.symbol, qty, order.price)
        
        logger.info(f"[MockBroker] Filled: {order.symbol} {order.direction.value} {order.quantity} @ {order.price}")
        return order_status

    def _update_position(self, symbol: str, qty: float, price: float):
        if symbol in self.positions:
            current = self.positions[symbol]
            new_qty = current["qty"] + qty
            
            if abs(new_qty) < 0.001:
                del self.positions[symbol]
            else:
                # Simplified cost basis calc
                total_val = (current["qty"] * current["cost"]) + (qty * price)
                current["cost"] = total_val / new_qty
                current["qty"] = new_qty
        else:
            self.positions[symbol] = {"qty": qty, "cost": price}

    def get_positions(self):
        return self.positions
