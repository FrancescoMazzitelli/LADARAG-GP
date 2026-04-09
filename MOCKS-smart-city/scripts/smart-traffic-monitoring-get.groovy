def req = mockRequest.getRequest()
def q = { key ->
    def vals = req?.getParameterValues(key)
    return (vals != null && vals.length > 0) ? String.valueOf(vals[0]) : null
}

def congestionLevel = q("congestionLevel")
if (congestionLevel == "low") return "low"
if (congestionLevel == "medium") return "medium"
if (congestionLevel == "high") return "high"
if (congestionLevel == "critical") return "critical"
return "example_list"

