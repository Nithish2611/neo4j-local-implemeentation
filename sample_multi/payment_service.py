def calculate_refund(amount):
    if amount > 0:
        return amount * 0.9
    return 0


def process_payment(user_id, amount):
    refund_amount = calculate_refund(amount)
    print(f"User {user_id} refunded {refund_amount}")
