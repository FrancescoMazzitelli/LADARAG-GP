def req = mockRequest.getRequest()
def q = { key ->
    def vals = req?.getParameterValues(key)
    return (vals != null && vals.length > 0) ? String.valueOf(vals[0]) : null
}

def status = q("status")
if (status == "on") return "on"
if (status == "off") return "off"
if (status == "malfunctioning") return "malfunctioning"
return "example_list"

