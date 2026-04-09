def req = mockRequest.getRequest()
def q = { key ->
    def vals = req?.getParameterValues(key)
    return (vals != null && vals.length > 0) ? String.valueOf(vals[0]) : null
}

def status = q("status")
if (status == "open") return "open"
if (status == "temporarily_closed") return "temporarily_closed"
if (status == "closed") return "closed"
def wheelchairAccessible = q("wheelchairAccessible")
if (wheelchairAccessible == "true") return "accessible_only"
return "example_list"

