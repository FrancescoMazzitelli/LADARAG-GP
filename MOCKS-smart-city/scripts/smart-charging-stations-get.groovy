def req = mockRequest.getRequest()
def q = { key ->
    def vals = req?.getParameterValues(key)
    return (vals != null && vals.length > 0) ? String.valueOf(vals[0]) : null
}

def available = q("available")
if (available == "true") return "available_true"
if (available == "false") return "available_false"
return "example_list"

