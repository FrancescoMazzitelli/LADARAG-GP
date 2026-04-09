def req = mockRequest.getRequest()
def q = { key ->
    def vals = req?.getParameterValues(key)
    return (vals != null && vals.length > 0) ? String.valueOf(vals[0]) : null
}

def status = q("status")
if (status == "normal") return "normal"
if (status == "full") return "full"
if (status == "overflowing") return "overflowing"
if (status == "maintenance") return "maintenance"
if (status == "offline") return "offline"
return "example_list"

