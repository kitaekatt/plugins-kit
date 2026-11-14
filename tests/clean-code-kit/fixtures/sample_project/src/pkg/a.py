def total_price(items, tax):
    subtotal = 0
    for item in items:
        if item.quantity > 0:
            subtotal += item.price * item.quantity
    result = subtotal * (1 + tax)
    return round(result, 2)


class Cart:
    def weight(self, things):
        acc = 0
        for t in things:
            if t.count > 0:
                acc += t.mass * t.count
        out = acc * (1 + self.extra)
        return round(out, 3)
