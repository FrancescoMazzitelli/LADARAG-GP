def req = mockRequest.getRequest()
def q = { key ->
    def vals = req?.getParameterValues(key)
    return (vals != null && vals.length > 0) ? String.valueOf(vals[0]) : null
}

def status = q("status")
if (status == "in-service") return "in-service"
if (status == "delayed") return "delayed"
if (status == "suspended") return "suspended"
if (status == "out-of-service") return "out-of-service"
if (status == "maintenance") return "maintenance"
if (status == "end-of-line") return "end-of-line"
return "example_list"

