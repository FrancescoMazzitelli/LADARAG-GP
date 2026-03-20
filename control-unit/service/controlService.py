from service.discoveryService import Discovery
from flask import Response
import json
import requests
import re
import aiohttp
import asyncio
import os
import mimetypes
import shutil
import time


class Controller:

    def __init__(self):
        '''
        Inizializza il Controller impostando il nome del modello LLM da utilizzare
        e assicurandosi che la directory di base 'Files' per il salvataggio temporaneo
        degli allegati esista nel file system.
        '''
        self.model_name = "phi4-reasoning:14b"
        os.makedirs("Files", exist_ok=True)

    def analyze_files(self, files: list):
        '''
        Cicla su una lista di file caricati dall'utente, li salva localmente nella 
        cartella 'Files', deduce il loro MIME type e ne determina la categoria 
        (document, image, tabular, unknown). Ritorna una lista di metadati dei file.
        '''
        analyzed = []
        if files:
            for f in files:
                filename = f.filename
                content_type = f.mimetype or mimetypes.guess_type(filename)[0]
                size = f.content_length
                path = os.path.join("Files", filename)
                f.save(path)

                file_info = {
                    "filename": filename,
                    "content_type": content_type,
                    "size": size,
                    "path": path
                }

                if content_type == "application/pdf":
                    file_info["category"] = "document"
                elif content_type and content_type.startswith("image/"):
                    file_info["category"] = "image"
                elif content_type in ["text/csv", "application/vnd.ms-excel"]:
                    file_info["category"] = "tabular"
                else:
                    file_info["category"] = "unknown"

                analyzed.append(file_info)
        return analyzed

    def query_ollama(self, prompt: str) -> tuple[str, float]:
        '''
        Invia una richiesta HTTP POST al server locale Ollama per generare una 
        risposta tramite il modello LLM specificato. Calcola e ritorna il tempo di 
        latenza (in secondi) insieme alla risposta testuale pulita. Gestisce le 
        eccezioni di rete e di parsing.
        '''
        url = os.environ.get("OLLAMA_API_URL", "http://localhost:11434")
        try:
            start_time = time.perf_counter()
            response = requests.post(
                f"{url}/api/generate",
                json={
                    "model": self.model_name,
                    "prompt": prompt,
                    "options": {
                        "temperature": 0.0,
                        "max_tokens": 4096, # NOTA: se vedi JSON mozzati, alza questo a 8192
                        "num_ctx": 8192,
                    },
                    "stream": False
                }
            )
            response.raise_for_status()
            end_time = time.perf_counter()

            latency = end_time - start_time
            data = response.json()
            return data.get("response", "").strip(), latency

        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"[HTTP ERROR] Errore nella richiesta a Ollama: {e}")
        except ValueError:
            raise RuntimeError(f"[PARSE ERROR] Risposta non JSON valida da Ollama")

    def decompose_task(self, discovered_services, discovered_capabilities, discovered_endpoints,
                       discovered_schemas, discovered_request_schemas, query, input_files=None):
        '''
        Prende in input le informazioni scoperte sui microservizi (capabilities, endpoint, 
        schemi, file) e la query dell'utente. Costruisce un prompt di sistema estremamente 
        dettagliato con le regole di output (System Prompt) e interroga l'LLM. Ritorna la 
        risposta grezza dell'LLM e il tempo di generazione.
        '''
        # --- MODIFICA PROMPT: Esempio corretto con URL assoluti ---
        example = {
            "tasks": [
                {
                    "task_name": "get_incident_info",
                    "service_id": "svc-emergency",
                    "url": "http://mock-server:8080/rest/EmergencyAPI/1.0/incident",
                    "operation": "GET",
                    "input": ""
                },
                {
                    "task_name": "dispatch_ambulance",
                    "service_id": "svc-ambulance",
                    "url": "http://mock-server:8080/rest/AmbulanceAPI/1.0/dispatch",
                    "operation": "POST",
                    "input": {
                        "incident_id": "{{get_incident_info.id}}",
                        "location": "{{get_incident_info.location}}"
                    }
                }
            ]
        }

        example_str = json.dumps(example)
        # --- MODIFICA PROMPT: Regole aggiunte in fondo per forzare URL, vietare Dummy Task e risparmiare Token ---
        prompt = f"""
            <|system|>
            You have access to a list of services registered in a distributed system, each described by:
            - services
            - capabilities
            - endpoints
            - response schemas (field names and types of the JSON response for each endpoint)
            - request schemas (field names and types required in the body for POST/PUT endpoints)
            - optional user-provided files

            You will receive a query in natural language and must:
            1. Decompose it into atomic tasks.
            2. Associate each task with one or more compatible services based on their capabilities, endpoints, and file types.
            3. Order the tasks sequentially. If a task depends on the output of a previous task, it MUST appear after it.
            4. Return an execution plan.

            REPLY ONLY with a valid JSON, WITHOUT any introductory text or comments.

            TEMPLATE:
            {example_str}

            RULES:
            - Use only the data provided. Do not make assumptions or invent services or endpoints.
            - To chain services, you can pass the output of a previous task as input using {{{{task_name.field_name}}}}.
            - IMPORTANT: If a previous task returns an array and you need to filter it by a condition, use the FIND syntax:
              {{{{task_name.FIND(key=value).field_name}}}}
              Example: {{{{get_sensors.FIND(sensorType=temperature).id}}}}
            - IMPORTANT: If a previous task returns an array and you need to access a specific element by position,
              use the index syntax (0-based): {{{{task_name.N.field_name}}}}
              Example: {{{{get_all_bins.0.id}}}} → the 'id' field of the first element returned by get_all_bins.
              Use this instead of hardcoding values you see in examples or documentation.
            - Use RESPONSE SCHEMAS to know the exact field names returned by each endpoint.
              Always prefer field names from RESPONSE SCHEMAS over guessing.
              Example: if RESPONSE SCHEMAS shows "{{{{id:int, roomId:int, status:enum}}}}", use {{{{task.roomId}}}}, NOT {{{{task.room_id}}}} or {{{{task.id}}}}.
            - Use REQUEST SCHEMAS to know what fields to include in POST/PUT request bodies.
              Fields marked with * are required and MUST always be included in the input.
              Example: if REQUEST SCHEMAS shows "{{{{location:str*, fillLevel:int*, status:enum*}}}}", the POST body must include at least location, fillLevel and status.
            - If files are provided by the user, you can upload them as input by writing strictly: [FILE]filename.ext[/FILE]
            - Use [TEXT]...[/TEXT] to explicitly mark text/json payloads only if necessary to disambiguate from files.
            - CRITICAL FOR PATH PARAMETERS: If an endpoint contains a placeholder like {{id}} or {{vehicleId}} (e.g., /api/vehicle/{{id}}), you MUST inject the chaining syntax DIRECTLY into the endpoint URL string.
            - Example WRONG: endpoint: "/api/vehicle/{{id}}", input: {{"id": "{{{{task_1.FIND(route=Linea 1).id}}}}"}}
            - Example RIGHT: endpoint: "/api/vehicle/{{{{task_1.FIND(route=Linea 1).id}}}}", input: ""
            - NEVER return an endpoint containing unresolved placeholders like {{id}}. The final URL must only contain the {{{{task_name...}}}} syntax or the actual extracted value.
            - If files are images, prefer OCR/image-processing services. If PDFs, prefer document-analysis services.
            - JSON KEYS CRITICAL RULE: You MUST use exactly these keys for each task: 'task_name', 'service_id', 'url', 'operation', and 'input'.
            - URL CRITICAL RULE: The 'url' field MUST contain the complete, absolute HTTP URL found in the ENDPOINTS list (e.g., http://mock-server:8080/rest/...).
            - OPERATION CRITICAL RULE: The 'operation' field MUST be exactly one of: "GET", "POST", "PUT", or "DELETE". NEVER invent operations like "FILTER" or "FIND". Inline filtering MUST be done inside the {{{{...}}}} syntax.
            
            <|end|>
            <|user|>
            
            SERVICES:
            {discovered_services}

            CAPABILITIES:
            {discovered_capabilities}

            ENDPOINTS:
            {discovered_endpoints}

            RESPONSE SCHEMAS (field names and types returned by each endpoint):
            {discovered_schemas}

            REQUEST SCHEMAS (body fields for POST/PUT — field* means required):
            {discovered_request_schemas}

            FILES:
            {input_files}

            QUERY:
            {query}
            <|end|>
            <|assistant|>
        """

        response, plan_latency = self.query_ollama(prompt)
        print(f"[LLM RESPONSE] {response}")
        print("=" * 100)
        return response, plan_latency

    def extract_agents(self, agents_json):
        '''
        Estrae il piano JSON dalla risposta dell'LLM.
        Strategia a tre livelli:
          1. Cerca JSON dopo </think> (phi4-reasoning e modelli CoT con tag esplicito)
          2. Cerca il primo blocco JSON valido nell'intera risposta (modelli senza think tag)
          3. Fallback: ritorna piano vuoto con log diagnostico
        '''
        plan = {}

        def try_parse_json_block(text):
            """Cerca e parsa il primo oggetto JSON valido in un testo."""
            start = text.find('{')
            end   = text.rfind('}') + 1
            if start != -1 and end > start:
                candidate = text[start:end].strip()
                try:
                    return json.loads(candidate), candidate
                except json.JSONDecodeError:
                    pass
            return None, ""

        # --- Strategia 1: dopo </think> ---
        think_match = re.search(r'</think>', agents_json, flags=re.IGNORECASE)
        if think_match:
            after_think = agents_json[think_match.end():].strip()
            result, json_str = try_parse_json_block(after_think)
            if result is not None:
                print("[PARSE] Piano estratto dopo </think>.")
                return result
            else:
                print("[WARN] </think> trovato ma nessun JSON valido dopo di esso. Tentativo fallback...")

        # --- Strategia 2: JSON grezzo nell'intera risposta ---
        result, json_str = try_parse_json_block(agents_json)
        if result is not None:
            print("[PARSE] Piano estratto dalla risposta grezza (no </think>).")
            return result

        # --- Strategia 3: fallback con log diagnostico ---
        preview = agents_json[:300].replace('\n', ' ') if agents_json else "(risposta vuota)"
        print(f"[FORMAT ERROR] Nessun JSON valido trovato nella risposta LLM.")
        print(f"[FORMAT ERROR] Anteprima risposta: {preview}")
        return plan


    '''
        resolve_placeholders riceve:
        │
        ├── stringa "{{task.field}}"          → Caso 1: restituisce valore con tipo originale
        │                                               int, bool, str, dict, list...
        │
        ├── stringa "url/{{task.field}}/path" → Caso 2: sostituzione testuale → sempre str
        │
        ├── dizionario {k: v, ...}            → ricorre su ogni valore
        │
        ├── lista [item, ...]                 → ricorre su ogni elemento
        │
        └── int, bool, None, ...              → restituisce invariato (nessun placeholder possibile)
    '''
    def resolve_placeholders(self, data, context):
        '''
        Sostituisce dinamicamente i placeholder (es. {{task.id}}) presenti in `data`
        recuperando i valori attuali dal dizionario di contesto (i risultati dei task precedenti).
        Supporta sintassi estese come .FIND() e la pulizia da allucinazioni nei placeholder.
        '''
        if isinstance(data, str):
            matches = re.findall(r'\{\{(.*?)\}\}', data)

            # CASO 1: L'intera stringa è ESATTAMENTE un solo placeholder
            if len(matches) == 1 and data.strip() == f"{{{{{matches[0]}}}}}":
                match = matches[0]
                
                # --- AGGIUNTA: NORMALIZZAZIONE (PULIZIA SINTASSI) ---
                match_clean = match.replace(".output", "").replace(".response", "").replace(".data", "")
                match_clean = re.sub(r'\[(\d+)\]', r'.\1', match_clean)
                
                parts = match_clean.split('.')
                task_name = parts[0]

                val = context.get(task_name, {})
                for part in parts[1:]:
                    if part.startswith("FIND(") and part.endswith(")"):
                        condition = part[5:-1]
                        if "=" in condition and isinstance(val, list):
                            key, expected_val = condition.split("=", 1)
                            key = key.strip()
                            expected_val = expected_val.strip().lower()

                            # --- NUOVA RIGA: Rimuove URL encoding (es. Ponte+Vanvitelli -> ponte vanvitelli) ---
                            expected_val = expected_val.replace('+', ' ').replace('%20', ' ')

                            # 1. TENTATIVO STANDARD
                            found = next((item for item in val if isinstance(item, dict) and expected_val in str(item.get(key, "")).lower()), None)

                            # 2. AUTO-CORREZIONE INTELLIGENTE (Omni-Search Fallback)
                            if not found:
                                print(f"[AUTO-CORREZIONE] Chiave '{key}' non trovata o nessun match per '{expected_val}'. Ricerca globale in corso...")
                                for item in val:
                                    if isinstance(item, dict):
                                        if any(expected_val in str(v).lower() for k, v in item.items() if v is not None):
                                            found = item
                                            print(f"[AUTO-CORREZIONE] Match trovato in un campo alternativo: {found}")
                                            break

                            val = found if found else ""
                        else:
                            val = ""
                    elif isinstance(val, dict):
                        val = val.get(part, "")
                    elif isinstance(val, list) and part.isdigit():
                        idx = int(part)
                        val = val[idx] if 0 <= idx < len(val) else ""
                    else:
                        val = ""
                return val

            # CASO 2: Placeholder mescolati con URL o testo
            for match in matches:
                
                # --- AGGIUNTA: NORMALIZZAZIONE (PULIZIA SINTASSI) ---
                match_clean = match.replace(".output", "").replace(".response", "").replace(".data", "")
                match_clean = re.sub(r'\[(\d+)\]', r'.\1', match_clean)
                
                parts = match_clean.split('.')
                task_name = parts[0]

                val = context.get(task_name, {})
                for part in parts[1:]:
                    if part.startswith("FIND(") and part.endswith(")"):
                        condition = part[5:-1]
                        if "=" in condition and isinstance(val, list):
                            key, expected_val = condition.split("=", 1)
                            key = key.strip()
                            expected_val = expected_val.strip().lower()

                            # --- NUOVA RIGA: Rimuove URL encoding (es. Ponte+Vanvitelli -> ponte vanvitelli) ---
                            expected_val = expected_val.replace('+', ' ').replace('%20', ' ')

                            # 1. TENTATIVO STANDARD
                            found = next((item for item in val if isinstance(item, dict) and expected_val in str(item.get(key, "")).lower()), None)

                            # 2. AUTO-CORREZIONE INTELLIGENTE
                            if not found:
                                print(f"[AUTO-CORREZIONE] Chiave '{key}' non trovata o nessun match per '{expected_val}'. Ricerca globale in corso...")
                                for item in val:
                                    if isinstance(item, dict):
                                        if any(expected_val in str(v).lower() for k, v in item.items() if v is not None):
                                            found = item
                                            print(f"[AUTO-CORREZIONE] Match trovato in un campo alternativo: {found}")
                                            break

                            val = found if found else ""
                        else:
                            val = ""
                    elif isinstance(val, dict):
                        val = val.get(part, "")
                    elif isinstance(val, list) and part.isdigit():
                        idx = int(part)
                        val = val[idx] if 0 <= idx < len(val) else ""
                    else:
                        val = ""

                if isinstance(val, (dict, list)):
                    val = json.dumps(val)

                data = data.replace(f"{{{{{match}}}}}", str(val))
            return data

        elif isinstance(data, dict):
            return {k: self.resolve_placeholders(v, context) for k, v in data.items()}
        elif isinstance(data, list):
            return [self.resolve_placeholders(item, context) for item in data]
        return data

    async def call_agent(self, session, task, discovered_services):
        '''
        Esegue un singolo task atomico generato dall'LLM in modo asincrono. Pulisce 
        e normalizza le chiavi JSON inventate dal modello (task_name, endpoint, operation),
        prepara l'eventuale payload e i file da inviare, fa la richiesta HTTP tramite 
        aiohttp e restituisce l'esito formattato per il contesto globale.
        '''
        # 1. Fallback estremo per il nome e l'url
        task_name  = task.get("task_name") or task.get("name") or task.get("taskName") or "unnamed_task"
        endpoint   = task.get("url") or task.get("endpoint") or ""
        input_data = task.get("input", "")
        
        # 2. Estrae l'operazione in modo sicuro (gestisce anche l'allucinazione 'method')
        operation  = str(task.get("operation") or task.get("method") or "GET").upper()

        # 3. Pulizia dell'endpoint se l'LLM scrive (es. "POST http://...")
        if " " in endpoint:
            endpoint = endpoint.split(" ")[-1]

        # 4. Auto-correzione se l'LLM si dimentica "http://" e mette solo la route
        if endpoint and not endpoint.startswith("http"):
            mock_url = os.environ.get("MOCK_SERVER_URL", "http://mock-server:8080")
            endpoint = f"{mock_url}{endpoint}" if endpoint.startswith("/") else f"{mock_url}/{endpoint}"

        # 5. FORZATURA PORTA 8080 (se l'LLM genera l'url senza la porta)
        if endpoint and "mock-server/" in endpoint:
            endpoint = endpoint.replace("mock-server/", "mock-server:8080/")

        # 6. SKIP DEI TASK FANTASMA (Es: task vuoti generati come reference)
        if not endpoint or str(endpoint).strip() == "":
            print(f"[WARN] Task fantasma '{task_name}' rilevato (nessun endpoint). Task ignorato in sicurezza.")
            return {
                "task_name": task_name,
                "operation": operation,
                "status": "SUCCESS", 
                "status_code": 200, 
                "result": {"skipped": True, "message": "Dummy task ignored"}
            }

        response_result = {
            "task_name": task_name,
            "operation": operation
        }

        # Gestione Tag Multi-modali (File vs Testo)
        tag_pattern = r"\[(\w+)\](.*?)\[/\1\]"
        payload   = input_data
        is_file   = False
        file_path = None
        filename  = None

        if isinstance(input_data, str):
            match = re.search(tag_pattern, input_data, re.DOTALL)
            if match:
                tag     = match.group(1)
                content = match.group(2)
                if tag == "TEXT":
                    try:
                        payload = json.loads(content)
                    except Exception:
                        payload = content
                elif tag == "FILE":
                    filename  = content
                    file_path = os.path.join("Files", filename)
                    if not os.path.exists(file_path):
                        response_result.update({"status": "ERROR", "status_code": 404, "result": f"File '{filename}' non trovato"})
                        return response_result
                    is_file = True

        try:
            request_kwargs = {"timeout": 5}

            if is_file:
                form = aiohttp.FormData()
                form.add_field("file", open(file_path, "rb"), filename=filename, content_type="application/octet-stream")
                request_kwargs["data"] = form
            else:
                if payload is not None and payload != "":
                    request_kwargs["json"] = payload

            # Esecuzione dinamica della chiamata HTTP asincrona
            if operation == "POST":
                resp_ctx = session.post(endpoint, **request_kwargs)
            elif operation == "PUT":
                resp_ctx = session.put(endpoint, **request_kwargs)
            elif operation == "GET":
                resp_ctx = session.get(endpoint, timeout=5)
            elif operation == "DELETE":
                resp_ctx = session.delete(endpoint, timeout=5)
            else:
                raise ValueError(f"HTTP Operation non supportata: {operation}")

            async with resp_ctx as resp:
                status       = resp.status
                content_type = resp.headers.get("Content-Type", "")

                if 200 <= status < 300:
                    # Risposta binaria (PDF / immagini): ritorno diretto senza parse
                    if content_type.startswith("application/pdf") or content_type.startswith("image/"):
                        return {
                            "status": "FILE",
                            "status_code": status,
                            "headers": dict(resp.headers),
                            "body": await resp.read()
                        }

                    # Leggo sempre il body come testo prima, per gestire body vuoti
                    raw_text = await resp.text()

                    if not raw_text.strip():
                        print(f"[WARN] Task '{task_name}': risposta {status} con body vuoto.")
                        result = None
                    elif "application/json" in content_type:
                        try:
                            result = json.loads(raw_text)
                        except json.JSONDecodeError as e:
                            print(f"[WARN] Risposta dichiarata JSON ma parsing fallito. Tratto come testo raw. Body: {raw_text[:200]}")
                            result = raw_text  # Fallback: salva la risposta come testo normale invece di sollevare l'eccezione
                    else:
                            result = raw_text

                    print(f"[SUCCESS] Task '{task_name}' completed.")
                    response_result.update({"status": "SUCCESS", "status_code": status, "result": result})

                else:
                    error_text = await resp.text()
                    print(f"[ERROR] Task '{task_name}' failed with status {status}: {error_text}")
                    response_result.update({"status": "ERROR", "status_code": status, "result": error_text})

        except Exception as e:
            print(f"[EXCEPTION] Task '{task_name}' fallito: → {e}")
            response_result.update({"status": "EXCEPTION", "status_code": 500, "result": str(e)})

        return response_result

    async def trigger_agents_async(self, agents: dict, discovered_services):
        '''
        Motore di orchestrazione asincrona dei task. Attraversa l'array JSON prodotto
        dall'LLM, risolve i placeholder dinamici iterazione dopo iterazione (salvando
        il contesto globale) e invoca call_agent. Se un task fallisce, la pipeline 
        si interrompe in sicurezza (short-circuit).
        '''
        tasks = agents.get("tasks", [])
        results = []
        execution_context = {}

        async with aiohttp.ClientSession() as session:
            for task in tasks:
                # 1. Chaining dinamico: risoluzione placeholder prima dell'esecuzione
                # Controllo sicuro per 'endpoint' / 'url'
                task_url = task.get("url") or task.get("endpoint") or ""
                task["url"] = self.resolve_placeholders(task_url, execution_context)
                
                if task.get("input"):
                    task["input"] = self.resolve_placeholders(task.get("input"), execution_context)

                # 2. Esecuzione task
                result = await self.call_agent(session, task, discovered_services)

                # Se il task ha generato un file, ritorna il blob binario direttamente
                if result.get("status") == "FILE":
                    return result

                results.append(result)

                # 3. Aggiorna contesto (chaining) o interrompi (short-circuit)
                # Ricaviamo il nome in modo sicuro
                safe_task_name = task.get("task_name") or task.get("name") or task.get("taskName") or "unnamed_task"

                if result.get("status") == "SUCCESS":
                    execution_context[safe_task_name] = result.get("result", {})
                else:
                    print(f"[CHAIN BROKEN] Task '{safe_task_name}' fallito. Interruzione pipeline.")
                    break

        return results

    def trigger_agents(self, agents: dict, discovered_services):
        '''
        Wrapper sincrono che avvia l'event loop di asyncio per l'esecuzione della
        pipeline asincrona (trigger_agents_async).
        '''
        return asyncio.run(self.trigger_agents_async(agents, discovered_services))

    def replace_endpoints(self, endpoints_list, mock_server_address):
        '''
        Aggiorna staticamente la lista degli endpoint, sostituendo il localhost 
        hardcodato di base con l'effettivo host del server mock caricato 
        dall'ambiente.
        '''
        updated = []
        for endpoint_dict in endpoints_list:
            new_dict = {}
            for k, v in endpoint_dict.items():
                if isinstance(v, str):
                    new_url = re.sub(r"http://localhost:8585", mock_server_address, v)
                    new_dict[k] = new_url
                else:
                    new_dict[k] = v
            updated.append(new_dict)
        return updated

    def control(self, query, files=None):
        '''
        Entry point principale del controller. Inietta i file caricati, recupera dal 
        registry e dal catalogo i servizi semantici rilevanti in base alla query,
        chiama 'decompose_task' per farsi generare il JSON orchestrativo, avvia i task
        tramite 'trigger_agents', pulisce il file system e ritorna il report completo 
        dei risultati al chiamante.
        '''
        input_files    = files or []
        analyzed_files = self.analyze_files(input_files)

        catalog_url         = os.environ.get("CATALOG_URL")
        registry            = Discovery(os.environ.get("REGISTRY_URL"))
        mock_server_address = os.environ.get("MOCK_SERVER_URL")

        discovered_services        = []
        discovered_capabilities    = []
        discovered_endpoints       = []
        discovered_schemas         = []
        discovered_request_schemas = []

        services     = registry.services()
        register_key = "POST /register"
        print("DISCOVERED SERVICES:")

        input_data   = {"query": query}
        service_data = requests.post(f"{catalog_url}/index/search", json=input_data)
        service_data = service_data.json()
        service_list = service_data["results"]

        if not service_list:
            return {"execution_plan": {}, "execution_results": [], "error": "No services matched the query"}

        registry_service_ids  = set(s["id"] for s in services)
        filtered_service_list = [s for s in service_list if s["_id"] in registry_service_ids]
        orphaned_services     = [s for s in service_list if s["_id"] not in registry_service_ids]

        if orphaned_services:
            print("[WARNING] Services found via semantic search but are no longer in the registry:")
            for s in orphaned_services:
                print(f"- {s.get('_id')} : {s.get('name')}")

        if not filtered_service_list:
            return {"execution_plan": {}, "execution_results": [], "error": "None of the discovered services are currently available in the registry"}

        for service in filtered_service_list:
            if isinstance(service.get("capabilities"), dict):
                service["capabilities"].pop(register_key, None)
            if isinstance(service.get("endpoints"), dict):
                service["endpoints"].pop(register_key, None)

            service_preamble = {
                "_id":         service.get("_id"),
                "name":        service.get("name"),
                "description": service.get("description"),
            }
            discovered_services.append(service_preamble)
            discovered_capabilities.append(service.get("capabilities", {}))
            discovered_endpoints.append(service.get("endpoints", {}))
            discovered_schemas.append(service.get("response_schemas", {}))
            discovered_request_schemas.append(service.get("request_schemas", {}))

        discovered_endpoints = self.replace_endpoints(discovered_endpoints, mock_server_address)

        # Generazione piano
        plan_json, plan_latency = self.decompose_task(
            discovered_services,
            discovered_capabilities,
            discovered_endpoints,
            discovered_schemas,
            discovered_request_schemas,
            query,
            analyzed_files
        )
        plan = self.extract_agents(plan_json)

        # Esecuzione
        results = self.trigger_agents(plan, discovered_services)

        # Gestione risposta file binario
        if isinstance(results, dict) and results.get("status") == "FILE":
            return Response(results["body"], status=results["status_code"], headers=results["headers"])

        # Pulizia cartella Files temporanea
        if os.path.exists('Files'):
            for filename in os.listdir('Files'):
                file_path = os.path.join('Files', filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shultil.rmtree(file_path)
                except Exception as e:
                    print(f'Failed to delete {file_path}. Reason: {e}')

        return {
            "execution_plan":          plan,
            "execution_results":       results,
            "plan_generation_latency": plan_latency
        }