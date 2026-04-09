def req = mockRequest.getRequest()
def q = { key ->
    def vals = req?.getParameterValues(key)
    return (vals != null && vals.length > 0) ? String.valueOf(vals[0]) : null
}

def sensorType = q("sensorType")
if (sensorType == "air_quality") return "air_quality"
if (sensorType == "temperature") return "temperature"
if (sensorType == "humidity") return "humidity"
if (sensorType == "noise") return "noise"
if (sensorType == "pressure") return "pressure"
if (sensorType == "wind_speed") return "wind_speed"
return "example_list"

