from flask import Flask, request, jsonify
from pymongo import MongoClient
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct
from bson import ObjectId
from bson.json_util import dumps
from cheroot.wsgi import Server as WSGIServer
import multiprocessing
import uuid
import os
import sys
import logging
import bson
import json
import signal

from sentence_transformers import SentenceTransformer, CrossEncoder


def handle_sigterm(*args):
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

log_file_path = "test.txt"
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path, mode='w', encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("app")

app = Flask(__name__)

MONGO_USER = os.environ.get("MONGO_USER", "admin")
MONGO_PASS = os.environ.get("MONGO_PASS", "admin")
MONGO_HOST = os.environ.get("MONGO_HOST", "catalog-data")
MONGO_PORT = os.environ.get("MONGO_PORT", "27017")
MONGO_DB   = os.environ.get("MONGO_DB", "microcks")
MONGO_URI  = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}:{MONGO_PORT}/"

QDRANT_HOST       = os.environ.get("QDRANT_HOST", "catalog-vector")
QDRANT_PORT       = os.environ.get("QDRANT_PORT", "6333")
QDRANT_COLLECTION       = os.environ.get("QDRANT_COLLECTION", "services")
QDRANT_COLLECTION_INDEX = os.environ.get("QDRANT_COLLECTION_INDEX", "services_index")
QDRANT_URI        = f"http://{QDRANT_HOST}:{QDRANT_PORT}"

mongo_client = MongoClient(MONGO_URI)
qdrant_client = QdrantClient(QDRANT_URI)

db         = mongo_client[MONGO_DB]
collection = db["services"]

is_server_ready = False
embedding_model = None
reranker_model  = None
tokenizer       = None


def load_model():
    global embedding_model, reranker_model, tokenizer
    logger.info("Loading models...")
    embedding_model = SentenceTransformer(
        model_name_or_path='Qwen/Qwen3-Embedding-0.6B',
        device='cpu',
        trust_remote_code=True
    )
    tokenizer = embedding_model.tokenizer
    logger.info("Embedding model loaded.")
    reranker_model = CrossEncoder(
        model_name_or_path='cross-encoder/ms-marco-MiniLM-L-6-v2',
        device='cpu',
        trust_remote_code=True
    )
    logger.info("Reranker model loaded.")


def clean_doc(doc):
    doc["_id"] = str(doc["_id"])
    return doc


def embed(input):
    embedding = embedding_model.encode(
        f"query: {input}", convert_to_tensor=False, normalize_embeddings=True
    )
    return embedding.tolist()


def count_tokens(text):
    tokens = tokenizer.encode(text, add_special_tokens=True)
    return len(tokens)


# ─────────────────────────────────────────────────────────────────── parallel
def init_model():
    global model
    model = SentenceTransformer('Qwen/Qwen3-Embedding-0.6B', device='cpu')


def embed_item(args):
    doc_id, key, text = args
    vector    = model.encode(f"query: {text}", normalize_embeddings=True)
    vector_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, text))
    return PointStruct(
        id=vector_id,
        vector=vector.tolist(),
        payload={"mongo_id": doc_id, "http_operation": key}
    )
# ─────────────────────────────────────────────────────────────────── parallel


def create_vector_collection():
    existing_collections = {col.name for col in qdrant_client.get_collections().collections}

    # Collezione endpoints: 1 vettore per endpoint (capability text) — Stage 2
    if QDRANT_COLLECTION not in existing_collections:
        qdrant_client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE)
        )
        logger.info(f"Collection '{QDRANT_COLLECTION}' created")
    else:
        logger.info(f"Collection '{QDRANT_COLLECTION}' already exists")

    # Collezione services_index: 1 vettore per servizio (description text) — Stage 1
    if QDRANT_COLLECTION_INDEX not in existing_collections:
        qdrant_client.create_collection(
            collection_name=QDRANT_COLLECTION_INDEX,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE)
        )
        logger.info(f"Collection '{QDRANT_COLLECTION_INDEX}' created")
    else:
        logger.info(f"Collection '{QDRANT_COLLECTION_INDEX}' already exists")


@app.route("/health")
def index():
    if is_server_ready is True:
        return jsonify({"status": "ok", "message": "Gateway Server is ready", "model_loaded": True}), 200
    else:
        logger.error("Model not yet loaded or broken")
        return jsonify({"status": "error", "message": "Model not yet loaded or broken", "model_loaded": False}), 500


@app.route("/index/search", methods=["POST"])
def vector_search():
    """
    Two-stage retrieval pipeline:

    Stage 1 — Bi-encoder su descriptions (services_index):
      Recupera i top-K servizi per similarità semantica sulla description.
      La description cattura il contesto cross-domain del servizio
      (es: "parking near tourist attractions, low traffic zones...")
      → alta recall: trova i servizi giusti anche per query composte

        Stage 2 — Cross-encoder su capabilities (reranker ms-marco):
            Per ogni servizio recuperato nel Stage 1, carica TUTTI i suoi endpoint
            da MongoDB e li rerankerizza con il CrossEncoder.
            Mantiene sempre i top-N endpoint per score (senza soglia minima).
            → bilanciamento recall/precision costante per ogni servizio selezionato
    """
    data = request.get_json()
    if not data or "query" not in data:
        return jsonify({"error": "Missing 'query' field"}), 400

    query_text      = data["query"]
    query_embedding = embed(query_text)

    # ── Parametri two-stage ───────────────────────────────────────────────────
    STAGE1_K             = 5     # quanti servizi recuperare nel primo stage
    TOP_ENDPOINTS_PER_SERVICE = 4  # restituisce sempre i top-4 endpoint per servizio
    INTELLIGENCE_ID        = "smart-city-intelligence-mock"
    MIN_SCORE_INTELLIGENCE = 0.60  # soglia speciale per Intelligence API

    # ════════════════════════════════════════════════════════════════════════
    # STAGE 1: recupero servizi per similarità sulla description
    # ════════════════════════════════════════════════════════════════════════
    stage1_results = qdrant_client.search(
        collection_name=QDRANT_COLLECTION_INDEX,
        query_vector=query_embedding,
        limit=STAGE1_K
    )

    if not stage1_results:
        logger.warning("[SEARCH] Stage 1: nessun servizio trovato in services_index")
        return jsonify({"results": []}), 200

    # Mappa service_id → score Stage 1 (similarità description)
    stage1_scores = {
        r.payload["mongo_id"]: r.score
        for r in stage1_results
    }

    logger.info(f"[STAGE 1] {len(stage1_scores)} servizi recuperati: "
                f"{list(stage1_scores.keys())}")

    # ════════════════════════════════════════════════════════════════════════
    # STAGE 2: reranking degli endpoint per ogni servizio recuperato
    # ════════════════════════════════════════════════════════════════════════
    merged: dict = {}

    for doc_id, s1_score in stage1_scores.items():

        # Filtra Intelligence API con soglia speciale già nel Stage 1
        if doc_id == INTELLIGENCE_ID and s1_score < MIN_SCORE_INTELLIGENCE:
            logger.info(f"[STAGE 1] {doc_id} escluso: score {s1_score:.3f} < {MIN_SCORE_INTELLIGENCE}")
            continue

        # Carica il documento completo da MongoDB
        retrieved = collection.find_one({"_id": doc_id})
        if not retrieved:
            logger.warning(f"[STAGE 2] Servizio '{doc_id}' non trovato in MongoDB")
            continue
        retrieved = bson.json_util.loads(dumps(retrieved))

        capabilities     = retrieved.get("capabilities", {}) or {}
        endpoints        = retrieved.get("endpoints", {}) or {}
        response_schemas = retrieved.get("response_schemas", {}) or {}
        request_schemas  = retrieved.get("request_schemas", {}) or {}
        parameters       = retrieved.get("parameters", {}) or {}

        # Filtra endpoint /register (sempre escluso)
        ops = [op for op in capabilities.keys() if op != "POST /register"]
        if not ops:
            continue

        # Reranker CrossEncoder: (query, capability_text) per ogni endpoint
        rerank_inputs  = [(query_text, capabilities[op]) for op in ops]
        endpoint_scores = reranker_model.predict(rerank_inputs)

        # Seleziona gli endpoint sopra la soglia di rilevanza
        scored_ops = sorted(
            zip(ops, endpoint_scores),
            key=lambda x: x[1],
            reverse=True
        )

        # Per ogni servizio selezionato, tieni sempre i top-N endpoint per score.
        relevant_ops = scored_ops[:TOP_ENDPOINTS_PER_SERVICE]

        best_endpoint_score = relevant_ops[0][1]

        merged[doc_id] = {
            "_id":              doc_id,
            "name":             retrieved.get("name"),
            "description":      retrieved.get("description"),
            "capabilities":     {op: capabilities[op] for op, _ in relevant_ops},
            "endpoints":        {op: endpoints.get(op) for op, _ in relevant_ops},
            "response_schemas": {op: response_schemas.get(op) for op, _ in relevant_ops},
            "request_schemas":  {op: request_schemas.get(op) for op, _ in relevant_ops},
            "parameters":       {op: parameters.get(op) for op, _ in relevant_ops},
            "_stage1_score":    s1_score,
            "_best_ep_score":   best_endpoint_score,
        }

        logger.info(
            f"[STAGE 2] {doc_id}: {len(relevant_ops)}/{len(ops)} endpoint rilevanti "
            f"| ep_score={best_endpoint_score:.3f} | s1_score={s1_score:.3f}"
        )

    if not merged:
        return jsonify({"results": []}), 200

    # ── Ordina i servizi per best_endpoint_score (proxy di rilevanza finale) ──
    # Usiamo il reranker score dell'endpoint migliore come ranking finale.
    # Il Stage 1 score garantisce che il servizio sia semanticamente rilevante,
    # il Stage 2 score affina la rilevanza specifica per la query.
    ordered_services = sorted(
        merged.values(),
        key=lambda x: x["_best_ep_score"],
        reverse=True
    )
    for s in ordered_services:
        s.pop("_stage1_score", None)
        s.pop("_best_ep_score", None)

    # ── Token budget: include i servizi completi finché non si supera il limite ──
    max_tokens     = 3500
    current_tokens = 0
    top_results    = []

    for s in ordered_services:
        serialized = json.dumps(s)
        n_tokens   = count_tokens(serialized)
        if current_tokens + n_tokens <= max_tokens:
            top_results.append(s)
            current_tokens += n_tokens
        else:
            break

    logger.info(
        f"[SEARCH] Stage1={len(stage1_scores)} servizi → "
        f"Stage2={len(merged)} con endpoint rilevanti → "
        f"{len(top_results)} nel budget | token usati: {current_tokens}/{max_tokens}"
    )
    return jsonify({"results": top_results}), 200


@app.route("/service", methods=["POST"])
def create_or_update_service_old():
    data = request.get_json()
    if not data or "id" not in data:
        return jsonify({"error": "Missing 'id' field"}), 400

    doc_id = data["id"]
    data["_id"] = doc_id
    data.pop("id", None)

    # ── Stage 2 index: 1 vettore per endpoint (capability text) ──────────────
    capabilities = data.get("capabilities", {})
    for http_op, capability in capabilities.items():
        embedding = embed(capability)
        vector_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, capability))
        qdrant_client.upsert(
            collection_name=QDRANT_COLLECTION,
            points=[
                PointStruct(
                    id=vector_id,
                    vector=embedding,
                    payload={"mongo_id": doc_id, "http_operation": http_op}
                )
            ]
        )

    # ── Stage 1 index: 1 vettore per servizio (description text) ─────────────
    # Usa la description del servizio come testo di retrieval di primo livello.
    # La description è scritta per catturare i cross-domain use case del servizio,
    # a differenza delle capabilities che descrivono singoli endpoint.
    description = data.get("description", "")
    if description:
        desc_vector_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"desc:{doc_id}"))
        desc_embedding = embed(description)
        qdrant_client.upsert(
            collection_name=QDRANT_COLLECTION_INDEX,
            points=[
                PointStruct(
                    id=desc_vector_id,
                    vector=desc_embedding,
                    payload={"mongo_id": doc_id}
                )
            ]
        )
        logger.info(f"[INDEX] Service '{doc_id}' indexed in services_index")

    collection.replace_one({"_id": doc_id}, data, upsert=True)
    return jsonify({"status": "ok", "id": doc_id}), 200


# ─────────────────────────────────────────────────────────────────── parallel
@app.route("/service/old", methods=["POST"])
def create_or_update_service():
    data = request.get_json()
    if not data or "id" not in data:
        return jsonify({"error": "Missing 'id' field"}), 400

    doc_id = data["id"]
    data["_id"] = doc_id
    data.pop("id", None)

    capabilities = data.get("capabilities")
    input_data   = [(doc_id, k, v) for k, v in capabilities.items()]

    try:
        with multiprocessing.Pool(initializer=init_model) as pool:
            points = pool.map(embed_item, input_data)
    except Exception as e:
        logger.exception("Embedding failed")
        return jsonify({"error": "Embedding failed", "details": str(e)}), 500

    qdrant_client.upsert(collection_name=QDRANT_COLLECTION, points=points)
    collection.replace_one({"_id": doc_id}, data, upsert=True)
    return jsonify({"status": "ok", "id": doc_id}), 200
# ─────────────────────────────────────────────────────────────────── parallel


@app.route("/services", methods=["GET"])
def list_services():
    docs = list(collection.find())
    return dumps(docs), 200


@app.route("/services/<string:service_id>", methods=["GET"])
def get_service(service_id):
    doc = collection.find_one({"_id": service_id})
    if not doc:
        return jsonify({"error": "Service not found"}), 404
    return dumps(doc), 200


@app.route("/services/<string:service_id>", methods=["DELETE"])
def delete_service(service_id):
    result = collection.delete_one({"_id": service_id})
    if result.deleted_count == 0:
        return jsonify({"error": "Service not found"}), 404
    return jsonify({"status": "deleted", "id": service_id}), 200


@app.route("/index/reindex", methods=["POST"])
def reindex_descriptions():
    """
    Reindicizza le description di tutti i servizi in MongoDB nella collezione
    services_index (Stage 1). Necessario dopo il primo deploy o dopo aver
    aggiunto nuovi servizi quando services_index era vuota.
    """
    docs = list(collection.find())
    indexed = 0
    skipped = 0
    for doc in docs:
        doc_id      = doc.get("_id")
        description = doc.get("description", "")
        if not description:
            logger.warning(f"[REINDEX] {doc_id}: description vuota, saltato")
            skipped += 1
            continue
        try:
            desc_vector_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"desc:{doc_id}"))
            desc_embedding = embed(description)
            qdrant_client.upsert(
                collection_name=QDRANT_COLLECTION_INDEX,
                points=[
                    PointStruct(
                        id=desc_vector_id,
                        vector=desc_embedding,
                        payload={"mongo_id": doc_id}
                    )
                ]
            )
            logger.info(f"[REINDEX] {doc_id} indicizzato")
            indexed += 1
        except Exception as e:
            logger.error(f"[REINDEX] {doc_id} failed: {e}")
            skipped += 1

    logger.info(f"[REINDEX] Completato: {indexed} indicizzati, {skipped} saltati")
    return jsonify({"indexed": indexed, "skipped": skipped}), 200


@app.route("/services/<string:service_id>/schemas", methods=["PATCH"])
def update_service_schemas(service_id):
    """
    Aggiorna response_schemas, request_schemas e/o parameters di un servizio.
    Chiamato dall'api-importer dopo aver estratto gli schema con prance.
    """
    data = request.get_json()
    update_fields = {}
    if "response_schemas" in data:
        update_fields["response_schemas"] = data["response_schemas"]
    if "request_schemas" in data:
        update_fields["request_schemas"] = data["request_schemas"]
    if "parameters" in data:
        update_fields["parameters"] = data["parameters"]

    if not update_fields:
        return jsonify({"error": "No valid schema fields provided"}), 400

    result = collection.update_one(
        {"_id": service_id},
        {"$set": update_fields}
    )

    if result.matched_count == 0:
        return jsonify({"error": f"Service '{service_id}' not found"}), 404

    logger.info(f"[SCHEMAS] Updated {list(update_fields.keys())} for {service_id}")
    return jsonify({"status": "ok", "id": service_id}), 200


if __name__ == "__main__":
    try:
        with app.app_context():
            logger.info("🛠️ Creating Qdrant collection...")
            create_vector_collection()
            logger.info("📦 Loading embedding model...")
            load_model()
            is_server_ready = True
            logger.info("✅ Server is ready.")
    except Exception as e:
        logger.exception("❌ Failed to initialize application")
        sys.exit(1)

    server = WSGIServer(('0.0.0.0', 5000), app)
    try:
        print("🚀 Starting Flask app with Cheroot on http://0.0.0.0:5000")
        server.start()
    except KeyboardInterrupt:
        print("🛑 Shutting down server...")
        server.stop()