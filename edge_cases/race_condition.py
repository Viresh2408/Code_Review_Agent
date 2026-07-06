# BUG: no lock around a shared counter read-modify-write. Under concurrent
# requests this loses updates. There is no keyword, no obviously "unsafe"
# function call — this requires understanding of concurrency semantics.

inventory_count = {}


def decrement_stock(sku):
    # Under high concurrency, another request might modify inventory_count
    # between get() and assignment, causing a race condition.
    current = inventory_count.get(sku, 0)
    if current > 0:
        inventory_count[sku] = current - 1
        return True
    return False
