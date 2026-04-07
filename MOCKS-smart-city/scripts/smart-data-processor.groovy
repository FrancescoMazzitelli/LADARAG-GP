import groovy.json.JsonOutput
import java.net.URL
import java.net.HttpURLConnection

// ── Service constants ──────────────────────────────────────────────────────
def SERVICE_NAME         = "smart-data-processor-mock"
def SERVICE_ID           = "smart-data-processor"
def API_IMPORTER_HOST    = System.getenv("API_IMPORTER_HOST") ?: "api-importer"
def API_IMPORTER_PORT    = System.getenv("API_IMPORTER_PORT") ?: "7500"
def CONSUL_HOST          = System.getenv("CONSUL_HOST")        ?: "registry"
def CONSUL_INTERNAL_PORT = System.getenv("CONSUL_PORT")        ?: "8500"
def GATEWAY_HOST         = System.getenv("GATEWAY_HOST")       ?: "catalog-gateway"
def GATEWAY_PORT         = System.getenv("GATEWAY_PORT")       ?: "5000"

def BASE_URL = "http://${API_IMPORTER_HOST}:${API_IMPORTER_PORT}/processor"

// ── Retry helper ──────────────────────────────────────────────────────────
def withRetry = { int maxAttempts, long baseDelayMs, Closure action ->
    int attempt = 0
    while (true) {
        try {
            return action.call(attempt)
        } catch (Exception e) {
            attempt++
            if (attempt >= maxAttempts) throw e
            long delay = baseDelayMs * (1L << attempt)
            println "[retry ${attempt}/${maxAttempts - 1}] ${e.message} — waiting ${delay}ms"
            Thread.sleep(delay)
        }
    }
}

// ── Capabilities (used for Qdrant vector indexing) ────────────────────────
def capabilities = [
    "POST /join"       : "Inner/left/right join of two JSON arrays on a common field (e.g. zoneId). Merges fields from both sides. Use when two previous tasks return data that must be combined by a shared key.",
    "POST /aggregate"  : "Computes avg, sum, min, max, count, median, stddev on a numeric field. Supports group_by for per-group statistics (e.g. avg AQI per zone, count vehicles per zone).",
    "POST /intersect"  : "Returns items from 'left' whose key field value also appears in 'right'. Use to find items present in both datasets (e.g. parking spots in zones that ALSO have open attractions).",
    "POST /diff"       : "Returns items from 'left' whose key field value does NOT appear in 'right'. Use to find gaps (e.g. high-traffic zones with NO available charging stations).",
    "POST /group"      : "Groups a list of objects by a field value, returning a dict of {key: [items]}. Optional count_only for just the counts per group.",
    "POST /rank"       : "Multi-criteria weighted ranking. Each criterion has field, weight (0-1), and order (asc=lower is better, desc=higher is better). Returns items sorted by composite score.",
    "POST /sort"       : "Sorts a list by a numeric or string field with optional top-N selection. Simpler alternative to rank when only one criterion is needed.",
    "POST /filter"     : "Filters a list with multiple conditions (AND/OR logic). Operators: eq, neq, gt, gte, lt, lte, in, contains. Use for complex multi-field filtering not expressible in JMESPath.",
]

// ── Endpoints ─────────────────────────────────────────────────────────────
def endpoints = capabilities.collectEntries { opKey, _ ->
    def path = opKey.split(" ")[1]
    [(opKey): "${BASE_URL}${path}"]
}

// ── Request schemas (what the LLM must put in the 'input' field) ──────────
// Format: {field:type*, ...}  (* = required)
// arr = JSON array passed via JMESPath chaining from a previous task result
def request_schemas = [
    "POST /join"      : "{left:arr*, right:arr*, on:str*, type:enum(inner,left,right)}",
    "POST /aggregate" : "{data:arr*, field:str*, operations:arr*, group_by:str}",
    "POST /intersect" : "{left:arr*, right:arr*, field:str*}",
    "POST /diff"      : "{left:arr*, right:arr*, field:str*}",
    "POST /group"     : "{data:arr*, by:str*, count_only:bool}",
    "POST /rank"      : "{data:arr*, criteria:arr*, top:int}",
    "POST /sort"      : "{data:arr*, by:str*, order:enum(asc,desc), top:int}",
    "POST /filter"    : "{data:arr*, conditions:arr*, logic:enum(and,or)}",
]

// ── Response schemas (what the LLM can chain from the result) ─────────────
def response_schemas = [
    "POST /join"      : "[{obj}]",
    "POST /aggregate" : "{field:float}",
    "POST /intersect" : "[{obj}]",
    "POST /diff"      : "[{obj}]",
    "POST /group"     : "{key:[{obj}]}",
    "POST /rank"      : "[{obj}]",
    "POST /sort"      : "[{obj}]",
    "POST /filter"    : "[{obj}]",
]

// ── Parameters (none — all inputs are POST body, not query params) ─────────
def parameters = capabilities.collectEntries { opKey, _ ->
    [(opKey): null]
}

// ── Full catalog payload ───────────────────────────────────────────────────
def catalogPayload = [
    id              : SERVICE_ID,
    name            : SERVICE_NAME,
    description     : "Real data processing microservice integrated into the api-importer. Performs post-processing operations on JSON arrays returned by previous pipeline tasks: join, aggregate, intersect, diff, group, rank, sort, filter. NOT a mock — executes real computation. Call as the final task when results from multiple previous tasks need to be combined or analyzed.",
    capabilities    : capabilities,
    endpoints       : endpoints,
    request_schemas : request_schemas,
    response_schemas: response_schemas,
    parameters      : parameters,
]

// ── Consul payload ─────────────────────────────────────────────────────────
def consulPayload = [
    Name: SERVICE_NAME,
    Id  : SERVICE_ID,
    Meta: [service_doc_id: SERVICE_ID],
    Check: [
        TlsSkipVerify                 : true,
        Method                        : "GET",
        Http                          : "http://${API_IMPORTER_HOST}:${API_IMPORTER_PORT}/health",
        Interval                      : "10s",
        Timeout                       : "5s",
        DeregisterCriticalServiceAfter: "30s"
    ]
]

def jsonConsul  = JsonOutput.prettyPrint(JsonOutput.toJson(consulPayload))
def jsonCatalog = JsonOutput.prettyPrint(JsonOutput.toJson(catalogPayload))
def responseMap = [:]

// ── 1. Deregister from Gateway ────────────────────────────────────────────
try {
    def conn = new URL("http://${GATEWAY_HOST}:${GATEWAY_PORT}/service/${SERVICE_ID}").openConnection() as HttpURLConnection
    conn.setRequestMethod("DELETE")
    conn.connect()
    println "[deregister] Gateway HTTP ${conn.responseCode} for ${SERVICE_ID}"
} catch (Exception e) {
    println "[deregister] Gateway skipped: ${e.message}"
}

// ── 2. Deregister from Consul ─────────────────────────────────────────────
try {
    def conn = new URL("http://${CONSUL_HOST}:${CONSUL_INTERNAL_PORT}/v1/agent/service/deregister/${SERVICE_ID}").openConnection() as HttpURLConnection
    conn.setRequestMethod("PUT")
    conn.connect()
    println "[deregister] Consul HTTP ${conn.responseCode} for ${SERVICE_ID}"
} catch (Exception e) {
    println "[deregister] Consul skipped: ${e.message}"
}

// ── 3. Register to Gateway ────────────────────────────────────────────────
try {
    withRetry(3, 2000L) { int attempt ->
        println "[gateway] Registration attempt ${attempt + 1}…"
        def conn = new URL("http://${GATEWAY_HOST}:${GATEWAY_PORT}/service").openConnection() as HttpURLConnection
        conn.setRequestMethod("POST")
        conn.setDoOutput(true)
        conn.setRequestProperty("Content-Type", "application/json")
        conn.outputStream.withWriter("UTF-8") { it << jsonCatalog }
        def code = conn.responseCode
        println "[gateway] HTTP ${code}"
        if (code >= 400) throw new RuntimeException("Gateway returned HTTP ${code}")
    }
} catch (Exception e) {
    println "[gateway] Failed: ${e.message}"
    responseMap.status = "error"; responseMap.gatewayError = e.message
    return responseMap
}

// ── 4. Register to Consul ─────────────────────────────────────────────────
try {
    withRetry(3, 2000L) { int attempt ->
        println "[consul] Registration attempt ${attempt + 1}…"
        def conn = new URL("http://${CONSUL_HOST}:${CONSUL_INTERNAL_PORT}/v1/agent/service/register").openConnection() as HttpURLConnection
        conn.setRequestMethod("PUT")
        conn.setDoOutput(true)
        conn.setRequestProperty("Content-Type", "application/json")
        conn.outputStream.withWriter("UTF-8") { it << jsonConsul }
        def code = conn.responseCode
        println "[consul] HTTP ${code}"
        if (code >= 400) throw new RuntimeException("Consul returned HTTP ${code}")
        responseMap.status = "success"
        responseMap.httpCode = code
        responseMap.message = "Service registered successfully"
    }
} catch (Exception e) {
    println "[consul] Failed: ${e.message}"
    responseMap.status = "error"; responseMap.httpCode = 500
    responseMap.message = "Failed to register to Consul"; responseMap.error = e.message
}

if (responseMap.status == "success") { return "ok" } else { return "fail" }