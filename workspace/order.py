def calculate_total(prices, discount):
    total = sum(prices)
    return total * (100 - discount) / 100