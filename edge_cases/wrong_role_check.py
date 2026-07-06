# BUG: checks that the user is logged in, but never checks that they own
# the resource they're modifying. No "auth" keyword is obviously missing —
# there IS an auth check, it's just the wrong one. Pattern-matching for
# "missing auth" will pass this; semantic understanding should catch it.

from django.http import HttpResponse, HttpResponseForbidden
from .models import Invoice


def update_invoice(request, invoice_id):
    if not request.user.is_authenticated:
        return HttpResponseForbidden()
    invoice = Invoice.objects.get(id=invoice_id)
    # BUG: ownership (invoice.owner == request.user) is never checked.
    # This is IDOR, but structurally different from a missing auth check.
    invoice.amount = request.POST["amount"]
    invoice.save()
    return HttpResponse("Updated")
