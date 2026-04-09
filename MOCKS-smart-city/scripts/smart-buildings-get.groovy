def req = mockRequest.getRequest()
def q = { key ->
    def vals = req?.getParameterValues(key)
    return (vals != null && vals.length > 0) ? String.valueOf(vals[0]) : null
}

def status = q("status")
if (status == "operational") return "operational"
if (status == "evacuated") return "evacuated"
if (status == "maintenance") return "maintenance"
if (status == "closed") return "closed"
return "example_list"

