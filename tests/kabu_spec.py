"""公式の OpenAPI 仕様(kabu_STATION_API.yaml)でリクエストと応答を検証する。"""

from __future__ import annotations

import copy
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SPEC_PATH = Path(__file__).parent / "fixtures" / "kabusapi" / "kabu_STATION_API.yaml"


@cache
def spec() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    return loaded


def _nullable(schema: Any) -> Any:
    """応答の検証用に、すべての項目で null を許す(実際の API は該当なしで null を返す)。"""
    if isinstance(schema, dict):
        out = {k: _nullable(v) for k, v in schema.items()}
        if "type" in out and isinstance(out["type"], str):
            out["type"] = [out["type"], "null"]
        return out
    if isinstance(schema, list):
        return [_nullable(x) for x in schema]
    return schema


def _validator(schema: dict[str, Any], *, allow_null: bool) -> Draft202012Validator:
    components = copy.deepcopy(spec()["components"])
    if allow_null:
        components = _nullable(components)
        schema = _nullable(schema)
    return Draft202012Validator({**schema, "components": components})


def _resolve(schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        return spec()["components"]["schemas"][name]  # type: ignore[no-any-return]
    return schema


def _operation(method: str, path: str) -> dict[str, Any]:
    paths = spec()["paths"]
    rel = path.removeprefix("/kabusapi")
    if rel in paths:
        return paths[rel][method.lower()]  # type: ignore[no-any-return]
    for template, ops in paths.items():
        if "{" in template:
            prefix = template.split("{", 1)[0]
            if rel.startswith(prefix) and rel.count("/") == template.count("/"):
                return ops[method.lower()]  # type: ignore[no-any-return]
    raise KeyError(f"仕様にない API です: {method} {path}")


def validate_request(method: str, path: str, body: Any, query: dict[str, str]) -> list[str]:
    """リクエストの本文とクエリを仕様と照らし、問題の一覧を返す(空なら合格)。"""
    problems: list[str] = []
    op = _operation(method, path)
    if body is not None:
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        validator = _validator(schema, allow_null=False)
        problems += [f"{method} {path}: {e.message}" for e in validator.iter_errors(body)]
        # 仕様に無い項目名(綴りの誤りなど)も検出する
        problems += _unknown_keys(_resolve(schema), body, f"{method} {path}")
    allowed = {p["name"] for p in op.get("parameters", []) if p.get("in") == "query"}
    problems += [f"{method} {path}: 仕様に無いクエリ {k}" for k in query if k not in allowed]
    return problems


def _unknown_keys(schema: dict[str, Any], value: Any, where: str) -> list[str]:
    schema = _resolve(schema)
    if isinstance(value, dict) and "properties" in schema:
        out = []
        for key, sub in value.items():
            if key not in schema["properties"]:
                out.append(f"{where}: 仕様に無い項目 {key}")
            else:
                out += _unknown_keys(schema["properties"][key], sub, f"{where}.{key}")
        return out
    if isinstance(value, list) and "items" in schema:
        return [p for item in value for p in _unknown_keys(schema["items"], item, where)]
    return []


def validate_response(method: str, path: str, body: Any) -> list[str]:
    op = _operation(method, path)
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    validator = _validator(schema, allow_null=True)
    return [f"{method} {path} 応答: {e.message}" for e in validator.iter_errors(body)]
