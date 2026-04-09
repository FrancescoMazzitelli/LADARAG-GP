def req = mockRequest.getRequest()
def q = { key ->
    def vals = req?.getParameterValues(key)
    return (vals != null && vals.length > 0) ? String.valueOf(vals[0]) : null
}

def status = q("status")
if (status == "open") return "open"
if (status == "resolved") return "resolved"
if (status == "in_progress") return "in_progress"
return "example_list"

