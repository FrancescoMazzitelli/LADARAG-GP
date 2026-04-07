"""
api-importer/controller/dataProcessorController.py

Data processing operations exposed as REST endpoints.
Integrated into the api-importer Flask app at /processor/*.
"""
from flask import request
from flask_restx import Namespace, Resource
import math

api = Namespace("processor", description="Data processing operations on pipeline results")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def bad(msg, code=400):
    return {"error": msg}, code

def require_fields(data, *fields):
    missing = [f for f in fields if f not in data]
    return f"Missing required fields: {missing}" if missing else None


# ─────────────────────────────────────────────────────────────────────────────
# POST /processor/join
# ─────────────────────────────────────────────────────────────────────────────

@api.route("/join")
class Join(Resource):
    def post(self):
        """
        Joins two lists of objects on a common field.
        Body: { "left": [...], "right": [...], "on": "zoneId", "type": "inner" }
        type: inner (default) | left | right
        """
        data = request.get_json(force=True) or {}
        err = require_fields(data, "left", "right", "on")
        if err:
            return bad(err)

        left       = data["left"]
        right      = data["right"]
        on         = data["on"]
        join_type  = data.get("type", "inner")

        if not isinstance(left, list) or not isinstance(right, list):
            return bad("'left' and 'right' must be arrays")

        right_index = {}
        for item in right:
            key = item.get(on)
            if key is not None:
                right_index.setdefault(key, []).append(item)

        result = []
        for l_item in left:
            key = l_item.get(on)
            matches = right_index.get(key, [])
            if matches:
                for r_item in matches:
                    result.append({**r_item, **l_item})
            elif join_type == "left":
                result.append(dict(l_item))

        if join_type == "right":
            left_keys = {item.get(on) for item in left}
            for r_item in right:
                if r_item.get(on) not in left_keys:
                    result.append(dict(r_item))

        return result, 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /processor/aggregate
# ─────────────────────────────────────────────────────────────────────────────

@api.route("/aggregate")
class Aggregate(Resource):
    def post(self):
        """
        Computes avg, sum, min, max, count, median, stddev on a numeric field.
        Body: { "data": [...], "field": "lastReading", "operations": ["avg","min"], "group_by": "zoneId" }
        """
        data = request.get_json(force=True) or {}
        err = require_fields(data, "data", "field", "operations")
        if err:
            return bad(err)

        items      = data["data"]
        field      = data["field"]
        operations = data["operations"]
        group_by   = data.get("group_by")

        valid_ops = {"avg", "sum", "min", "max", "count", "median", "stddev"}
        invalid = [op for op in operations if op not in valid_ops]
        if invalid:
            return bad(f"Invalid operations: {invalid}. Valid: {sorted(valid_ops)}")

        def compute(subset):
            values = [item[field] for item in subset
                      if field in item and isinstance(item[field], (int, float))]
            res = {}
            if not values:
                return {op: None for op in operations}
            if "count"  in operations: res["count"]  = len(values)
            if "sum"    in operations: res["sum"]    = sum(values)
            if "min"    in operations: res["min"]    = min(values)
            if "max"    in operations: res["max"]    = max(values)
            if "avg"    in operations: res["avg"]    = sum(values) / len(values)
            if "median" in operations:
                s = sorted(values)
                n = len(s)
                res["median"] = (s[n//2-1] + s[n//2]) / 2 if n % 2 == 0 else s[n//2]
            if "stddev" in operations:
                if len(values) > 1:
                    avg = sum(values) / len(values)
                    res["stddev"] = math.sqrt(sum((v - avg)**2 for v in values) / len(values))
                else:
                    res["stddev"] = 0.0
            return res

        if group_by:
            groups = {}
            for item in items:
                key = str(item.get(group_by, "__ungrouped__"))
                groups.setdefault(key, []).append(item)
            return {
                key: {"group": key, "count_items": len(sub), **compute(sub)}
                for key, sub in groups.items()
            }, 200

        return compute(items), 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /processor/intersect
# ─────────────────────────────────────────────────────────────────────────────

@api.route("/intersect")
class Intersect(Resource):
    def post(self):
        """
        Returns items from 'left' whose 'field' value also appears in 'right'.
        Body: { "left": [...], "right": [...], "field": "zoneId" }
        """
        data = request.get_json(force=True) or {}
        err = require_fields(data, "left", "right", "field")
        if err:
            return bad(err)

        left  = data["left"]
        right = data["right"]
        field = data["field"]

        right_values = {item.get(field) for item in right if field in item}
        return [item for item in left if item.get(field) in right_values], 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /processor/diff
# ─────────────────────────────────────────────────────────────────────────────

@api.route("/diff")
class Diff(Resource):
    def post(self):
        """
        Returns items from 'left' whose 'field' value does NOT appear in 'right'.
        Body: { "left": [...], "right": [...], "field": "zoneId" }
        """
        data = request.get_json(force=True) or {}
        err = require_fields(data, "left", "right", "field")
        if err:
            return bad(err)

        left  = data["left"]
        right = data["right"]
        field = data["field"]

        right_values = {item.get(field) for item in right if field in item}
        return [item for item in left if item.get(field) not in right_values], 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /processor/group
# ─────────────────────────────────────────────────────────────────────────────

@api.route("/group")
class Group(Resource):
    def post(self):
        """
        Groups a list by a field value.
        Body: { "data": [...], "by": "zoneId", "count_only": false }
        """
        data = request.get_json(force=True) or {}
        err = require_fields(data, "data", "by")
        if err:
            return bad(err)

        items      = data["data"]
        by         = data["by"]
        count_only = data.get("count_only", False)

        groups = {}
        for item in items:
            key = str(item.get(by, "__ungrouped__"))
            groups.setdefault(key, []).append(item)

        if count_only:
            return {key: len(vals) for key, vals in groups.items()}, 200
        return groups, 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /processor/rank
# ─────────────────────────────────────────────────────────────────────────────

@api.route("/rank")
class Rank(Resource):
    def post(self):
        """
        Multi-criteria weighted ranking.
        Body: { "data": [...], "criteria": [{"field":"price","weight":0.4,"order":"asc"}], "top": 3 }
        """
        data = request.get_json(force=True) or {}
        err = require_fields(data, "data", "criteria")
        if err:
            return bad(err)

        items    = data["data"]
        criteria = data["criteria"]
        top      = data.get("top")

        if not items:
            return [], 200

        for crit in criteria:
            field  = crit["field"]
            order  = crit.get("order", "asc")
            values = [item.get(field) for item in items
                      if isinstance(item.get(field), (int, float))]
            if not values:
                for item in items:
                    item[f"_norm_{field}"] = 0.5
                continue
            v_min, v_max = min(values), max(values)
            r = v_max - v_min if v_max != v_min else 1.0
            for item in items:
                raw = item.get(field)
                if not isinstance(raw, (int, float)):
                    item[f"_norm_{field}"] = 0.5
                    continue
                norm = (raw - v_min) / r
                item[f"_norm_{field}"] = (1.0 - norm) if order == "asc" else norm

        total_weight = sum(c.get("weight", 1.0) for c in criteria) or 1.0
        for item in items:
            item["_score"] = round(
                sum(item.get(f"_norm_{c['field']}", 0.5) * c.get("weight", 1.0)
                    for c in criteria) / total_weight, 4)

        items_sorted = sorted(items, key=lambda x: x["_score"], reverse=True)
        for item in items_sorted:
            item.pop("_score", None)
            for crit in criteria:
                item.pop(f"_norm_{crit['field']}", None)

        result = items_sorted[:top] if top else items_sorted
        return result, 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /processor/sort
# ─────────────────────────────────────────────────────────────────────────────

@api.route("/sort")
class Sort(Resource):
    def post(self):
        """
        Sorts a list by a field with optional top-N.
        Body: { "data": [...], "by": "lastReading", "order": "asc", "top": 1 }
        """
        data = request.get_json(force=True) or {}
        err = require_fields(data, "data", "by")
        if err:
            return bad(err)

        items = data["data"]
        by    = data["by"]
        order = data.get("order", "asc")
        top   = data.get("top")

        if not isinstance(items, list):
            return bad("'data' must be an array")

        try:
            reverse = (order == "desc")
            sorted_items = sorted(
                items,
                key=lambda x: x.get(by, 0)
                if isinstance(x.get(by), (int, float))
                else str(x.get(by, "")),
                reverse=reverse
            )
        except Exception as e:
            return bad(f"Sort failed: {e}")

        result = sorted_items[:top] if top else sorted_items
        return result, 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /processor/filter
# ─────────────────────────────────────────────────────────────────────────────

@api.route("/filter")
class Filter(Resource):
    def post(self):
        """
        Filters a list with multiple conditions.
        Body: { "data": [...], "conditions": [{"field":"available","op":"eq","value":true}], "logic":"and" }
        Operators: eq, neq, gt, gte, lt, lte, in, contains
        """
        data = request.get_json(force=True) or {}
        err = require_fields(data, "data", "conditions")
        if err:
            return bad(err)

        items      = data["data"]
        conditions = data["conditions"]
        logic      = data.get("logic", "and").lower()

        def match(item, cond):
            field = cond.get("field")
            op    = cond.get("op", "eq")
            value = cond.get("value")
            iv    = item.get(field)
            try:
                if op == "eq":       return iv == value
                if op == "neq":      return iv != value
                if op == "gt":       return iv >  value
                if op == "gte":      return iv >= value
                if op == "lt":       return iv <  value
                if op == "lte":      return iv <= value
                if op == "in":       return iv in value
                if op == "contains": return str(value).lower() in str(iv).lower()
            except Exception:
                return False
            return False

        def passes(item):
            results = [match(item, c) for c in conditions]
            return all(results) if logic == "and" else any(results)

        return [item for item in items if passes(item)], 200