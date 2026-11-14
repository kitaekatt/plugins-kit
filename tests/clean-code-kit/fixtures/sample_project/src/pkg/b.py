def sum_cost(rows, rate):
    base = 0
    for row in rows:
        if row.quantity > 0:
            base += row.price * row.quantity
    value = base * (1 + rate)
    return round(value, 2)


def sum_cost_again(rows, rate):
    base = 0
    for row in rows:
        if row.quantity > 0:
            base += row.price * row.quantity
    value = base * (1 + rate)
    return round(value, 2)


def branchy(x):
    if x > 1 and x < 10:
        return 1
    elif x == 0 or x == -1:
        return 2
    while x:
        x -= 1
    return 3
