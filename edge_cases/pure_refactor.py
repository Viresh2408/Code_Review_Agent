# BEFORE (in main branch)
# def calculate_total(items):
#     total = 0
#     for item in items:
#         total = total + item.price
#     return total

# AFTER (in the PR diff) — pure style refactor, identical behavior.
# Expected output: near-zero findings. Architecture and Test-Coverage
# agents should recognize this changes nothing behaviorally.


def calculate_total(items):
    return sum(item.price for item in items)
