def calculate_total(prices, discount):
    total = sum(prices)
    if discount < 0 or discount > 100:
        raise ValueError("Discount must be between 0 and 100")
    return total * (100 - discount) / 100