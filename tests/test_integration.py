"""Integration tests against real MongoDB servers (4.4 and 8.0, with authentication) and a replica set."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

from mongo_mcp import server as T
from mongo_mcp.client import ClientManager
from mongo_mcp.config import MongoProfile

from .conftest import TEST_DB, call, err, ok

pytestmark = pytest.mark.integration


def docs(res):
    return ok(res)["documents"]


# ====================================================================== connection

def test_test_connection(ctx):
    info = ok(call(T.test_connection))["server"]
    assert info["version"].split(".")[0] in ("4", "5", "6", "7", "8")
    assert info["authenticated_users"] == ["root@admin"] and info["topology"] == "standalone"
    assert info["transactions_supported"] is False and info["default_database"] == TEST_DB


def test_poll_monitoring_is_used(ctx):
    cli = ctx["mgr"].client("main")
    assert cli.options.server_monitoring_mode == "poll"
    assert ok(call(T.count_documents, collection="x"))["count"] == 0


def test_list_connections_hides_secrets(ctx):
    res = ok(call(T.list_connections))
    text = json.dumps(res)
    assert "Str0ng" not in text and "***" in text and res["connections"][0]["client_open"] is True


def test_wrong_password_and_unreachable_server(ctx):
    mgr = ctx["mgr"]
    bad = ctx["uri"].replace("Str0ng%21Pass", "wrong")
    mgr.add(MongoProfile(name="bad", uri=bad, database=TEST_DB, server_selection_timeout_ms=5000))
    e = err(call(T.test_connection, connection="bad"))
    assert e.get("code") == 18 or "Authentication failed" in e["message"]
    mgr.add(MongoProfile(name="dead", host="127.0.0.1:1", server_selection_timeout_ms=1500))
    started = time.monotonic()
    e = err(call(T.test_connection, connection="dead"))
    assert "Could not reach" in e["message"] and time.monotonic() - started < 15
    assert e["servers"][0]["address"] == "127.0.0.1:1" and e["servers"][0]["type"] == "Unknown"
    assert "refused" in e["servers"][0]["error"].lower() or e["servers"][0]["error"]
    mgr.remove("bad")
    mgr.remove("dead")
    assert "Unknown connection" in err(call(T.find, collection="x", connection="dead"))["message"]


def test_no_database_configured(ctx):
    ctx["mgr"].add(MongoProfile(name="nodb", uri=ctx["uri"]))
    try:
        assert "No database given" in err(call(T.find, collection="x", connection="nodb"))["message"]
        assert ok(call(T.find, collection="x", connection="nodb", database=TEST_DB))["count"] == 0
    finally:
        ctx["mgr"].remove("nodb")


def test_add_connection_runtime(ctx, monkeypatch):
    monkeypatch.setenv("TEST_MONGO_PW", "Str0ng!Pass")
    host = ctx["uri"].split("@")[1].rstrip("/")
    res = ok(call(T.add_connection, name="rt", host=host, user="root", password_env="TEST_MONGO_PW",
                  database=TEST_DB))
    assert res["server"]["authenticated_users"] == ["root@admin"]
    e = err(call(T.add_connection, name="rt2", host=host, user="root", password_env="NOT_SET_ANYWHERE"))
    assert "NOT_SET_ANYWHERE" in e["message"] and "mcp.json" in e["message"]
    ok(call(T.remove_connection, name="rt"))


# ====================================================================== CRUD

def test_crud_round_trip(ctx):
    res = ok(call(T.insert_documents, collection="customers", documents=[
        {"name": "Ahmet", "city": "İstanbul", "balance": {"$numberDecimal": "10.50"}, "tags": ["vip"],
         "joined": {"$date": "2024-01-31T10:00:00Z"}},
        {"name": "Ayşe", "city": "Ankara", "balance": 0, "tags": []},
        {"name": "Çağrı", "city": "İzmir", "balance": 5, "address": {"zip": "35000"}},
    ]))
    assert res["inserted_count"] == 3 and all("$oid" in i for i in res["inserted_ids"])
    one = ok(call(T.insert_documents, collection="customers", documents={"name": "Öykü", "city": None}))
    oid = one["inserted_ids"][0]

    got = docs(call(T.find, collection="customers", filter={"_id": oid}))
    assert got[0]["name"] == "Öykü" and got[0]["_id"] == oid  # output _id reusable as input filter
    # regex "i" does not fold the Turkish dotted İ ...
    got = docs(call(T.find, collection="customers", filter={"city": {"$regex": "^i", "$options": "i"}}))
    assert got == []
    # ... a Turkish collation does: "istanbul" == "İstanbul", "İZMİR" == "İzmir" (but "IZMIR" is "ızmır")
    tr = {"locale": "tr", "strength": 1}
    got = docs(call(T.find, collection="customers", filter={"city": {"$in": ["istanbul", "İZMİR"]}},
                    projection={"_id": 0, "name": 1}, sort={"name": 1}, collation=tr))
    assert got == [{"name": "Ahmet"}, {"name": "Çağrı"}]
    assert ok(call(T.count_documents, collection="customers", filter={"name": "AYŞE"}, collation=tr))["count"] == 1
    # in Turkish ş is its own letter, not an accented s
    assert ok(call(T.count_documents, collection="customers", filter={"name": "AYSE"}, collation=tr))["count"] == 0
    assert ok(call(T.count_documents, collection="customers", filter={"city": "IZMIR"}, collation=tr))["count"] == 0
    assert ok(call(T.aggregate, collection="customers", pipeline=[{"$match": {"city": "ankara"}}],
                   collation=tr))["count"] == 1
    assert "locale" in err(call(T.find, collection="customers", collation={"strength": 1}))["message"]
    first = docs(call(T.find, collection="customers", filter='{"name": "Ahmet"}'))[0]
    assert first["balance"] == {"$numberDecimal": "10.50"} and first["joined"] == {"$date": "2024-01-31T10:00:00Z"}
    got = docs(call(T.find, collection="customers", filter={"joined": {"$gte": {"$date": "2024-01-01T00:00:00Z"}}}))
    assert [d["name"] for d in got] == ["Ahmet"]

    res = ok(call(T.update_documents, collection="customers", filter={"city": {"$ne": None}},
                  update={"$inc": {"visits": 1}}, many=True))
    assert res["matched_count"] == 3 and res["modified_count"] == 3
    res = ok(call(T.update_documents, collection="customers", filter={"name": "Yeni"},
                  update={"$set": {"city": "Bursa"}}, upsert=True))
    assert res["matched_count"] == 0 and "$oid" in res["upserted_id"]
    res = ok(call(T.update_documents, collection="customers", filter={"name": "Ayşe"},
                  update=[{"$set": {"label": {"$concat": ["$name", " / ", "$city"]}}}]))  # pipeline update
    assert res["modified_count"] == 1
    assert docs(call(T.find, collection="customers", filter={"name": "Ayşe"}))[0]["label"] == "Ayşe / Ankara"
    res = ok(call(T.replace_document, collection="customers", filter={"name": "Yeni"},
                  replacement={"name": "Yeni", "city": "Konya"}))
    assert res["modified_count"] == 1
    assert ok(call(T.count_documents, collection="customers"))["count"] == 5
    assert ok(call(T.count_documents, collection="customers", filter={"visits": 1}))["count"] == 3
    assert ok(call(T.count_documents, collection="customers", estimated=True))["count"] == 5
    vals = ok(call(T.distinct, collection="customers", field="city"))["values"]
    assert set(vals) >= {"İstanbul", "Ankara", "İzmir", "Konya"}

    assert ok(call(T.delete_documents, collection="customers", filter={"name": "Yeni"}))["deleted_count"] == 1
    assert ok(call(T.delete_documents, collection="customers", filter={"visits": 1}, many=True))["deleted_count"] == 3
    assert ok(call(T.count_documents, collection="customers"))["count"] == 1


def test_update_array_filters_and_nested(ctx):
    ok(call(T.insert_documents, collection="orders", documents={"_id": 1, "items": [
        {"sku": "a", "qty": 1}, {"sku": "b", "qty": 2}]}))
    ok(call(T.update_documents, collection="orders", filter={"_id": 1}, update={"$inc": {"items.$[it].qty": 10}},
            array_filters=[{"it.sku": "b"}]))
    items = docs(call(T.find, collection="orders", filter={"_id": 1}))[0]["items"]
    assert items == [{"sku": "a", "qty": 1}, {"sku": "b", "qty": 12}]


def test_safety_guards(ctx):
    ok(call(T.insert_documents, collection="guard", documents=[{"a": 1}, {"a": 2}]))
    assert "confirm_all" in err(call(T.delete_documents, collection="guard", filter={}, many=True))["message"]
    assert "confirm_all" in err(call(T.update_documents, collection="guard", filter={},
                                     update={"$set": {"b": 1}}, many=True))["message"]
    assert "update operators" in err(call(T.update_documents, collection="guard", filter={"a": 1},
                                          update={"b": 2}))["message"]
    assert "non-empty filter" in err(call(T.replace_document, collection="guard", filter={},
                                          replacement={"x": 1}))["message"]
    assert ok(call(T.count_documents, collection="guard"))["count"] == 2
    res = ok(call(T.update_documents, collection="guard", filter={}, update={"$set": {"b": 1}}, many=True,
                  confirm_all=True))
    assert res["modified_count"] == 2
    assert ok(call(T.delete_documents, collection="guard", filter={}, many=True, confirm_all=True))["deleted_count"] == 2
    assert "confirm_name" in err(call(T.drop_collection, collection="guard", confirm_name="nope"))["message"]
    assert "does not exist" in err(call(T.drop_collection, collection="ghost", confirm_name="ghost"))["message"]
    assert "system database" in err(call(T.drop_database, database="admin", confirm_name="admin"))["message"]
    assert "confirm_name" in err(call(T.drop_database, database=TEST_DB, confirm_name="x"))["message"]
    assert "cannot be dropped" in err(call(T.drop_index, collection="guard", name="_id_"))["message"]


def test_input_errors(ctx):
    assert "not valid" in err(call(T.find, collection="c", filter="{bad"))["message"]
    assert "JSON object" in err(call(T.find, collection="c", filter="[1,2]"))["message"]
    assert "JSON array" in err(call(T.aggregate, collection="c", pipeline={"$match": {}}))["message"]
    assert "required" in err(call(T.find, collection=""))["message"]
    assert "sort direction" in err(call(T.find, collection="c", sort={"a": 5}))["message"]
    assert "limit" in err(call(T.find, collection="c", limit=0))["message"]
    e = err(call(T.find, collection="c", filter={"a": {"$nosuchop": 1}}))
    assert e["code_name"] == "BadValue" or "unknown operator" in e["message"]
    e = err(call(T.aggregate, collection="c", pipeline=[{"$bogus": 1}]))
    assert "Unrecognized pipeline stage" in e["message"]
    assert "non-empty array" in err(call(T.insert_documents, collection="c", documents=[]))["message"]


def test_duplicate_key_and_bulk_partial(ctx):
    ok(call(T.create_index, collection="uniq", keys={"email": 1}, unique=True))
    ok(call(T.insert_documents, collection="uniq", documents={"email": "a@x"}))
    e = err(call(T.insert_documents, collection="uniq", documents={"email": "a@x"}))
    assert e["code"] == 11000 and "duplicate key" in e["message"]
    e = err(call(T.insert_documents, collection="uniq", documents=[{"email": "b@x"}, {"email": "a@x"},
                                                                    {"email": "c@x"}], ordered=False))
    assert e["write_errors"] == 1 and e["failed_index"] == 1 and e["inserted"] == 2
    assert ok(call(T.count_documents, collection="uniq"))["count"] == 3


def test_limits_and_truncation(ctx):
    ok(call(T.insert_documents, collection="many", documents=[{"i": i} for i in range(250)]))
    res = ok(call(T.find, collection="many"))
    assert res["count"] == 100 and res["truncated"] is True  # profile default max_docs
    res = ok(call(T.find, collection="many", limit=250, sort={"i": 1}))
    assert res["count"] == 250 and "truncated" not in res and res["documents"][-1]["i"] == 249
    res = ok(call(T.find, collection="many", limit=10, skip=245, sort={"i": 1}))
    assert [d["i"] for d in res["documents"]] == [245, 246, 247, 248, 249] and "truncated" not in res
    res = ok(call(T.aggregate, collection="many", pipeline=[{"$match": {"i": {"$lt": 50}}}], limit=20))
    assert res["count"] == 20 and res["truncated"] is True


def test_query_timeout(ctx):
    ok(call(T.insert_documents, collection="slow", documents=[{"i": i} for i in range(5)]))
    started = time.monotonic()
    e = err(call(T.find, collection="slow", filter={"$where": "sleep(1000) || true"}, max_time_ms=500))
    assert e.get("code") == 50 or "time limit" in e["message"].lower() or "$where" in e["message"]
    assert time.monotonic() - started < 10


# ====================================================================== aggregation & schema

def test_aggregate_and_out(ctx):
    ok(call(T.insert_documents, collection="sales", documents=[
        {"region": "Marmara", "amount": 10}, {"region": "Marmara", "amount": 5}, {"region": "Ege", "amount": 7}]))
    res = docs(call(T.aggregate, collection="sales", pipeline=[
        {"$group": {"_id": "$region", "total": {"$sum": "$amount"}}}, {"$sort": {"_id": 1}}]))
    assert res == [{"_id": "Ege", "total": 7}, {"_id": "Marmara", "total": 15}]
    ok(call(T.aggregate, collection="sales", pipeline=[{"$match": {"region": "Ege"}}, {"$out": "sales_ege"}],
            allow_disk_use=True))
    assert ok(call(T.count_documents, collection="sales_ege"))["count"] == 1


def test_infer_schema_and_exploration(ctx):
    ok(call(T.insert_documents, collection="people", documents=[
        {"name": "a", "age": 30, "addr": {"city": "x"}, "phones": [{"type": "m"}]},
        {"name": "b", "age": "unknown", "created": {"$date": "2024-01-01T00:00:00Z"}}]))
    res = ok(call(T.infer_schema, collection="people", sample_size=50))
    fields = {f["path"]: f for f in res["fields"]}
    assert res["sampled"] == 2 and fields["age"]["types"] == {"int": 1, "string": 1}
    assert fields["addr.city"]["present_pct"] == 50.0 and fields["phones[].type"]["types"] == {"string": 1}
    assert fields["created"]["types"] == {"date": 1} and fields["_id"]["types"] == {"objectId": 2}

    dbs = ok(call(T.list_databases))["databases"]
    assert TEST_DB in [d["name"] for d in dbs]
    ok(call(T.create_collection, name="v_people", options={"viewOn": "people", "pipeline": [{"$match": {}}]}))
    colls = {c["name"]: c for c in ok(call(T.list_collections))["collections"]}
    assert colls["people"]["estimated_count"] == 2 and colls["v_people"]["type"] == "view"
    assert colls["v_people"]["view_on"] == "people"
    only = ok(call(T.list_collections, name_filter="^peo"))["collections"]
    assert [c["name"] for c in only] == ["people"]
    stats = ok(call(T.collection_stats, collection="people"))
    assert stats["count"] == 2 and stats["index_count"] == 1


def test_indexes(ctx):
    ok(call(T.insert_documents, collection="idx", documents={"a": 1, "created": {"$date": "2024-01-01T00:00:00Z"}}))
    assert ok(call(T.create_index, collection="idx", keys={"a": 1, "b": -1}))["index"] == "a_1_b_-1"
    ok(call(T.create_index, collection="idx", keys={"created": 1}, name="ttl", expire_after_seconds=3600))
    ok(call(T.create_index, collection="idx", keys={"c": 1}, name="partial", partial_filter={"c": {"$exists": True}}))
    ok(call(T.create_index, collection="idx", keys={"t": "text"}, name="txt"))
    idx = {i["name"]: i for i in ok(call(T.list_indexes, collection="idx"))["indexes"]}
    assert idx["ttl"]["expireAfterSeconds"] == 3600 and idx["partial"]["partialFilterExpression"]
    assert idx["a_1_b_-1"]["key"] == {"a": 1, "b": -1}
    ok(call(T.drop_index, collection="idx", name="partial"))
    assert "partial" not in {i["name"] for i in ok(call(T.list_indexes, collection="idx"))["indexes"]}
    assert err(call(T.drop_index, collection="idx", name="nope"))["message"]


def test_collection_admin(ctx):
    ok(call(T.create_collection, name="validated", options={"validator": {"$jsonSchema": {
        "bsonType": "object", "required": ["name"], "properties": {"name": {"bsonType": "string"}}}}}))
    ok(call(T.insert_documents, collection="validated", documents={"name": "ok"}))
    e = err(call(T.insert_documents, collection="validated", documents={"nope": 1}))
    assert e["code"] == 121 or "validation" in e["message"].lower()
    ok(call(T.create_collection, name="capped", options={"capped": True, "size": 65536, "max": 3}))
    ok(call(T.insert_documents, collection="capped", documents=[{"i": i} for i in range(5)]))
    assert ok(call(T.count_documents, collection="capped"))["count"] == 3
    assert err(call(T.create_collection, name="capped"))["message"]  # already exists
    ok(call(T.rename_collection, collection="capped", new_name="capped2"))
    names = [c["name"] for c in ok(call(T.list_collections))["collections"]]
    assert "capped2" in names and "capped" not in names
    ok(call(T.drop_collection, collection="capped2", confirm_name="capped2"))
    assert "capped2" not in [c["name"] for c in ok(call(T.list_collections))["collections"]]


def test_run_command_and_admin(ctx):
    res = ok(call(T.run_command, command={"dbStats": 1}))["result"]
    assert res["db"] == TEST_DB and res["ok"] == 1
    res = ok(call(T.run_command, command='{"ping": 1}', database="admin"))["result"]
    assert res["ok"] == 1
    pw = "Us3r!Pass"
    ok(call(T.run_command, command={"createUser": "reader", "pwd": pw, "roles": [{"role": "read", "db": TEST_DB}]}))
    try:
        users = ok(call(T.run_command, command={"usersInfo": "reader"}))["result"]["users"]
        assert users[0]["roles"][0]["role"] == "read"
        e = err(call(T.run_command, command={"nosuchcommand": 1}))
        assert e.get("code") == 59 or "no such" in e["message"].lower()
        st = ok(call(T.server_status))
        assert st["version"] and st["connections"]["current"] >= 1
        ops = ok(call(T.current_ops, active_only=False))
        assert ops["count"] >= 1  # idle connections and system operations included
        # login as the read-only user: reads work, writes are refused by the server
        host = ctx["uri"].split("@")[1]
        ctx["mgr"].add(MongoProfile(name="reader", uri=f"mongodb://reader:Us3r%21Pass@{host}",
                                    auth_source=TEST_DB, database=TEST_DB))
        ok(call(T.insert_documents, collection="pub", documents={"x": 1}))
        assert ok(call(T.count_documents, collection="pub", connection="reader"))["count"] == 1
        e = err(call(T.insert_documents, collection="pub", documents={"x": 2}, connection="reader"))
        assert e.get("code") == 13 and "privilege" in e.get("hint", "")
        info = ok(call(T.test_connection, connection="reader"))["server"]
        assert info["authenticated_users"] == [f"reader@{TEST_DB}"]
        assert ok(call(T.list_databases, connection="reader"))["databases"]  # authorizedDatabases
    finally:
        ctx["mgr"].remove("reader") if "reader" in [c.get("name") for c in ctx["mgr"].describe()] else None
        call(T.run_command, command={"dropUser": "reader"})


def test_kill_op(ctx):
    ok(call(T.insert_documents, collection="ko", documents=[{"i": i} for i in range(3)]))
    other = ClientManager({"o": MongoProfile(name="o", uri=ctx["uri"], database=TEST_DB)}, "o")
    cli = other.client("o")

    def slow():
        try:
            list(cli[TEST_DB]["ko"].find({"$where": "sleep(200) || true"}).batch_size(1).max_time_ms(60000))
        except Exception:
            pass

    th = threading.Thread(target=slow)
    th.start()
    try:
        opid = None
        for _ in range(50):
            ops = ok(call(T.current_ops))["operations"]
            mine = [o for o in ops if o["ns"] == f"{TEST_DB}.ko" and o["op"] in ("query", "getmore")]
            if mine:
                opid = mine[0]["opid"]
                break
            time.sleep(0.1)
        assert opid is not None
        ok(call(T.kill_op, opid=opid))
        th.join(timeout=20)
        assert not th.is_alive()
    finally:
        other.close_all()


def test_readonly_profile(ctx):
    ok(call(T.insert_documents, collection="ro", documents={"x": 1}))
    ctx["mgr"].add(MongoProfile(name="ro", uri=ctx["uri"], database=TEST_DB, readonly=True))
    try:
        assert ok(call(T.find, collection="ro", connection="ro"))["count"] == 1
        assert ok(call(T.aggregate, collection="ro", pipeline=[{"$match": {}}], connection="ro"))["count"] == 1
        assert ok(call(T.run_command, command={"dbStats": 1}, connection="ro"))["result"]["ok"] == 1
        for tool, kw in [
            (T.insert_documents, {"collection": "ro", "documents": {"x": 2}}),
            (T.update_documents, {"collection": "ro", "filter": {"x": 1}, "update": {"$set": {"y": 1}}}),
            (T.delete_documents, {"collection": "ro", "filter": {"x": 1}}),
            (T.create_index, {"collection": "ro", "keys": {"x": 1}}),
            (T.drop_collection, {"collection": "ro", "confirm_name": "ro"}),
            (T.run_command, {"command": {"drop": "ro"}}),
            (T.aggregate, {"collection": "ro", "pipeline": [{"$out": "copy"}]}),
            (T.run_transaction, {"operations": [{"op": "insert", "collection": "ro", "document": {}}]}),
            (T.kill_op, {"opid": 1}),
        ]:
            e = err(call(tool, connection="ro", **kw))
            assert e["type"] == "ReadOnlyViolation", tool.__name__
        assert ok(call(T.count_documents, collection="ro"))["count"] == 1
    finally:
        ctx["mgr"].remove("ro")


def test_transaction_on_standalone_is_clear(ctx):
    e = err(call(T.run_transaction, operations=[{"op": "insert", "collection": "t", "document": {"a": 1}}]))
    assert "replica set" in e["message"].lower() or e.get("code") == 20
    assert e["failed_step"]["index"] == 1


def test_concurrent_calls(ctx):
    ok(call(T.insert_documents, collection="conc", documents=[{"i": i} for i in range(20)]))

    async def many():
        return await asyncio.gather(*[T.count_documents(collection="conc", filter={"i": {"$lt": i}})
                                      for i in range(20)])
    counts = [json.loads(r)["count"] for r in asyncio.run(many())]
    assert counts == list(range(20))


# ====================================================================== transactions (replica set)

def test_transaction_commit_and_rollback(rs):
    ok(call(T.insert_documents, collection="stock", documents={"_id": "sku1", "qty": 5}))
    ok(call(T.create_index, collection="orders", keys={"no": 1}, unique=True))
    res = ok(call(T.run_transaction, operations=[
        {"op": "insert", "collection": "orders", "document": {"no": 1, "sku": "sku1"}},
        {"op": "update", "collection": "stock", "filter": {"_id": "sku1"}, "update": {"$inc": {"qty": -1}}},
        {"op": "insert_many", "collection": "log", "documents": [{"e": "a"}, {"e": "b"}]},
    ]))
    assert res["committed"] is True and [r["op"] for r in res["results"]] == ["insert", "update", "insert_many"]
    assert docs(call(T.find, collection="stock"))[0]["qty"] == 4

    res = call(T.run_transaction, operations=[
        {"op": "update", "collection": "stock", "filter": {"_id": "sku1"}, "update": {"$inc": {"qty": -1}}},
        {"op": "insert", "collection": "orders", "document": {"no": 1, "sku": "sku1"}},  # duplicate -> abort
    ])
    e = err(res)
    assert res["committed"] is False and e["failed_step"] == {"index": 2, "op": "insert", "collection": "orders"}
    assert e["code"] == 11000
    assert docs(call(T.find, collection="stock"))[0]["qty"] == 4  # first update was rolled back
    assert ok(call(T.count_documents, collection="orders"))["count"] == 1

    e = err(call(T.run_transaction, operations=[{"op": "delete_many", "collection": "log", "filter": {}}]))
    assert "confirm_all" in e["message"]
    res = ok(call(T.run_transaction, operations=[
        {"op": "delete_many", "collection": "log", "filter": {}, "confirm_all": True},
        {"op": "replace", "collection": "stock", "filter": {"_id": "sku1"}, "replacement": {"qty": 100}}]))
    assert res["results"][0]["deleted_count"] == 2
    assert "must be one of" in err(call(T.run_transaction, operations=[{"op": "drop", "collection": "x"}]))["message"]
    info = ok(call(T.test_connection))["server"]
    assert info["transactions_supported"] is True and info["topology"].startswith("replica set '")


# ====================================================================== replica set with unreachable advertised hosts

BAD_RS = os.environ.get("MONGO_MCP_TEST_BAD_RS")  # rs whose config advertises a host the client cannot reach


@pytest.fixture
def bad_rs():
    if not BAD_RS:
        pytest.skip("MONGO_MCP_TEST_BAD_RS not set")
    previous = T._manager
    yield
    if T._manager is not None and T._manager is not previous:
        T._manager.close_all()
    T.set_manager(previous)


def _bad_manager(**overrides):
    prof = MongoProfile(name="bad", uri=BAD_RS, database=TEST_DB, server_selection_timeout_ms=3000, **overrides)
    mgr = ClientManager({"bad": prof}, "bad")
    T.set_manager(mgr)
    return mgr


def test_direct_fallback_when_replica_set_advertises_unreachable_hosts(bad_rs):
    _bad_manager()
    info = ok(call(T.test_connection))["server"]
    host = BAD_RS.split("://")[1].split("/")[0].split("?")[0].split(",")[-1]
    assert info["direct_fallback"]["address"] == host and info["direct_fallback"]["role"] == "primary"
    assert info["topology"].startswith(f"direct connection to {host}") and "warning" not in info
    ok(call(T.insert_documents, collection="fb", documents={"a": 1}))
    assert ok(call(T.count_documents, collection="fb"))["count"] == 1
    conns = ok(call(T.list_connections))["connections"]
    assert conns[0]["direct_fallback"]["address"] == host
    ok(call(T.drop_collection, collection="fb", confirm_name="fb"))


def test_first_seed_down_second_used(bad_rs):
    scheme, rest = BAD_RS.split("://", 1)
    _bad_manager()
    T._manager.add(MongoProfile(name="two", uri=f"{scheme}://127.0.0.1:1,{rest}", database=TEST_DB,
                                server_selection_timeout_ms=3000), make_default=True)
    info = ok(call(T.test_connection, connection="two"))["server"]
    assert info["direct_fallback"]["address"] != "127.0.0.1:1"


def test_fallback_disabled_gives_clear_error(bad_rs):
    _bad_manager(fallback_direct=False)
    e = err(call(T.test_connection))
    assert "Could not reach" in e["message"] and e["servers"]
    assert T._manager.existing_client("bad") is None  # reset so the next call reconnects
